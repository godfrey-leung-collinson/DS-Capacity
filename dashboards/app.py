"""
DS-Capacity: Outlet Hourly Utilisation Rate Dashboard

Utilisation rate = estimated_occupancy / total_seating_capacity

Estimated occupancy at hour H is computed as the rolling sum of visits
over the past `dwell_time_hours` hours (including hour H), approximating
how many guests are concurrently present assuming they stay for `dwell_time_hours`.

Usage:
    streamlit run dashboards/app.py
"""

import datetime as dt
import logging
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import snowflake.connector
import streamlit as st
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
SQL_TEMPLATES_DIR = PROJECT_ROOT / "SQL_templates"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Outlet Hourly Utilisation Dashboard",
    page_icon="🛋️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .main-header { font-size: 2rem; font-weight: bold; color: #ce0058; margin-bottom: 1rem; }
    .sub-header  { font-size: 1.2rem; color: #003865; margin-bottom: 0.5rem; }
</style>
""", unsafe_allow_html=True)

COLORS = {
    "primary":    "#ce0058",
    "secondary":  "#003865",
    "aqua":       "#7ce0d3",
    "light_blue": "#007dba",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent / "config" / "dashboard_config.yaml"
    if not config_path.exists():
        st.error(f"Config not found: {config_path}")
        st.stop()
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_sql(template_name: str, **params) -> str:
    path = SQL_TEMPLATES_DIR / template_name
    if not path.exists():
        raise FileNotFoundError(f"SQL template not found: {path}")
    sql = path.read_text()
    return sql.format(**params) if params else sql


@st.cache_resource
def get_snowflake_connection(config: dict):
    sf = config.get("snowflake", {})
    try:
        conn = snowflake.connector.connect(
            user=sf.get("user", os.environ.get("SNOWFLAKE_USER", "")),
            account=sf.get("account", ""),
            authenticator="externalbrowser",
            warehouse=sf.get("warehouse", os.environ.get("WAREHOUSE", "")),
            database=sf.get("database", "STAGING_QUALITY"),
            session_parameters=config.get("query_tags", {}),
        )
        logger.info("Snowflake connection established.")
        return conn
    except Exception as e:
        logger.error(f"Snowflake connection failed: {e}")
        return None


@st.cache_data(ttl=3600)
def fetch_lounge_info(_cursor, lounge_codes: list, sql_template: str) -> pd.DataFrame:
    codes_str = ", ".join(f"'{c}'" for c in lounge_codes)
    sql = load_sql(sql_template, lounge_codes=codes_str)
    _cursor.execute(sql)
    return pd.DataFrame.from_records(
        _cursor.fetchall(), columns=[d[0] for d in _cursor.description]
    )


@st.cache_data(ttl=600)
def fetch_hourly_visits(_cursor, lounge_code: str, start_dt: str, end_dt: str, sql_template: str) -> pd.DataFrame:
    sql = load_sql(
        sql_template,
        selected_lounge=lounge_code,
        start_datetime=start_dt,
        end_datetime=end_dt,
    )
    _cursor.execute(sql)
    df = pd.DataFrame.from_records(
        _cursor.fetchall(), columns=[d[0] for d in _cursor.description]
    )
    df["VISIT_SLOT"] = pd.to_datetime(df["VISIT_SLOT"])
    df = df.sort_values("VISIT_SLOT").reset_index(drop=True)
    return df


@st.cache_data(ttl=600)
def fetch_historical_flights(
    _cursor,
    airport_code: str,
    start_dt: str,
    end_dt: str,
    flight_service_type: str,
    sql_template: str,
) -> pd.DataFrame:
    """Fetch actual departure flight counts from OAG actuals table.

    The historical SQL returns one row per (slot, terminal, airport) that had
    at least one flight.  Slots with no flights are absent (LEFT JOIN gaps).
    We re-index against a full 15-min spine and fill missing counts with 0.
    """
    sql = load_sql(
        sql_template,
        selected_airport=airport_code,
        start_datetime=start_dt,
        end_datetime=end_dt,
        flight_service_type=flight_service_type,
        is_cancelled="false",
    )
    _cursor.execute(sql)
    df = pd.DataFrame.from_records(
        _cursor.fetchall(), columns=[d[0] for d in _cursor.description]
    )
    if df.empty:
        return df

    # Normalise column names from the historical SQL output
    df = df.rename(columns={
        "VISIT_DATE":   "SLOT_START",
        "dep_airport":  "DEP_AIRPORT",
        "DEPTERMINAL":  "DEP_TERMINAL",
    })
    df["SLOT_START"] = pd.to_datetime(df["SLOT_START"])

    # Build a complete 15-min spine for the requested window and fill zeros
    spine = pd.DataFrame({
        "SLOT_START": pd.date_range(start_dt, end_dt, freq="15min", inclusive="left"),
    })
    df = (
        spine
        .merge(df, on="SLOT_START", how="left")
        .fillna({"DEP_FLIGHT_COUNT": 0})
    )
    df["DEP_FLIGHT_COUNT"] = df["DEP_FLIGHT_COUNT"].astype(int)
    df = df.sort_values("SLOT_START").reset_index(drop=True)
    return df


@st.cache_data(ttl=600)
def fetch_scheduled_seats(
    _cursor,
    airport_code: str,
    start_dt: str,
    end_dt: str,
    flight_service_type: str,
    sql_template: str,
) -> pd.DataFrame:
    """Fetch scheduled seat counts by cabin class from OAG schedule snapshots.

    Uses the last-Monday-of-previous-month snapshot per calendar month.
    The SQL already uses a LEFT JOIN against the 15-min time spine, so all
    slots are present; NaN seat values (no flight) are filled with 0.
    """
    sql = load_sql(
        sql_template,
        selected_airport=airport_code,
        start_datetime=start_dt,
        end_datetime=end_dt,
        flight_service_type=flight_service_type,
    )
    _cursor.execute(sql)
    df = pd.DataFrame.from_records(
        _cursor.fetchall(), columns=[d[0] for d in _cursor.description]
    )
    if df.empty:
        return df

    # Normalise column names to upper-case (SQL returns lower-case aliases)
    df.columns = [c.upper() for c in df.columns]
    df = df.rename(columns={
        "SLOT_START":   "SLOT_START",
        "DEP_AIRPORT":  "DEP_AIRPORT",
        "DEP_TERMINAL": "DEP_TERMINAL",
    })
    df["SLOT_START"] = pd.to_datetime(df["SLOT_START"])

    seat_cols = [
        "TOTAL_SEATS", "FIRST_CLASS_SEATS", "BUSINESS_CLASS_SEATS",
        "PREMIUM_ECONOMY_SEATS", "ECONOMY_PLUS_SEATS", "ECONOMY_CLASS_SEATS",
    ]
    for col in seat_cols:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(int)

    df = df.sort_values("SLOT_START").reset_index(drop=True)
    return df


CABIN_COLS = {
    "Total":            "TOTAL_SEATS",
    "First Class":      "FIRST_CLASS_SEATS",
    "Business Class":   "BUSINESS_CLASS_SEATS",
    "Premium Economy":  "PREMIUM_ECONOMY_SEATS",
    "Economy Plus":     "ECONOMY_PLUS_SEATS",
    "Economy":          "ECONOMY_CLASS_SEATS",
}


# ---------------------------------------------------------------------------
# Occupancy & utilisation calculation
# ---------------------------------------------------------------------------

SLOT_MINS = 15  # granularity of the raw data


def compute_utilisation(df: pd.DataFrame, dwell_time_mins: int, capacity: int) -> pd.DataFrame:
    """
    Compute estimated occupancy from 15-min slot visit counts.

    A visitor arriving in slot S is assumed to remain for `dwell_time_mins`
    minutes, so they occupy the lounge for the next N slots where
    N = ceil(dwell_time_mins / 15).  Occupancy at slot S is therefore the
    rolling sum of arrivals over the preceding N slots (inclusive).

    Utilisation = estimated_occupancy / capacity.
    """
    df = df.copy()
    rolling_slots = max(1, math.ceil(dwell_time_mins / SLOT_MINS))
    df["EST_OCCUPANCY"] = (
        df["TOTAL_VISITS"]
        .rolling(window=rolling_slots, min_periods=1)
        .sum()
    )
    df["UTILISATION"]  = df["EST_OCCUPANCY"] / capacity
    df["HOUR_OF_DAY"]  = df["VISIT_SLOT"].dt.hour
    return df


def filter_to_opening_hours(df: pd.DataFrame, opening_hours_rows: list[dict]) -> pd.DataFrame:
    """
    Return only rows whose VISIT_SLOT falls within the lounge's opening hours.

    opening_hours_rows : list of {'Day': 'Mon', 'Open': 'HH:MM', 'Close': 'HH:MM'}
    Rows with '—' for Open/Close are treated as closed all day.
    """
    if not opening_hours_rows:
        return df

    day_abbr_to_dow = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    schedule: dict[int, tuple[int, int]] = {}

    for row in opening_hours_rows:
        dow = day_abbr_to_dow.get(row.get("Day", ""), -1)
        if dow == -1:
            continue
        open_str  = row.get("Open",  "—")
        close_str = row.get("Close", "—")
        if open_str == "—" or close_str == "—":
            continue
        try:
            open_h  = int(open_str.split(":")[0])
            close_h = int(close_str.split(":")[0])
            if close_h == 0:
                close_h = 24
        except (ValueError, IndexError):
            continue
        schedule[dow] = (open_h, close_h)

    if not schedule:
        return df

    dow_series  = df["VISIT_SLOT"].dt.dayofweek
    hour_series = df["VISIT_SLOT"].dt.hour

    mask = pd.Series(False, index=df.index)
    for dow, (open_h, close_h) in schedule.items():
        mask |= (dow_series == dow) & (hour_series >= open_h) & (hour_series < close_h)

    return df[mask]


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_timeseries(df: pd.DataFrame, capacity: int, title: str) -> go.Figure:
    """
    Dual-axis time-series plot with a single line.

    One line is drawn (utilisation rate on the left axis).
    The right axis is a mirror scale showing the equivalent estimated
    occupancy headcount — since utilisation = occupancy / capacity,
    the two axes are always proportional and the line shape is identical.
    Reference lines are drawn at 70% and 100% utilisation (left axis).
    """
    fig = go.Figure()

    util_max = max(df["UTILISATION"].max() * 1.1, 1.05)  # headroom above 100%
    occupancy_max = capacity * util_max

    fig.add_trace(go.Scatter(
        x=df["VISIT_SLOT"],
        y=df["UTILISATION"],
        mode="lines",
        name="Utilisation Rate / Est. Occupancy",
        line=dict(color=COLORS["primary"], width=2),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Utilisation: %{y:.1%}<br>"
            "Est. Occupancy: %{customdata:.0f} visitors<extra></extra>"
        ),
        customdata=df["EST_OCCUPANCY"],
        yaxis="y1",
    ))

    # Invisible trace on y2 – forces Plotly to render the right axis
    # with the mirrored occupancy scale; no second line is visible.
    fig.add_trace(go.Scatter(
        x=df["VISIT_SLOT"],
        y=df["EST_OCCUPANCY"],
        mode="lines",
        line=dict(color="rgba(0,0,0,0)", width=0),
        hoverinfo="skip",
        showlegend=False,
        yaxis="y2",
    ))

    # Reference lines on left axis
    fig.add_hline(
        y=1.0, line_dash="dot",
        line=dict(color="red", width=1.5),
        annotation_text=f"100% capacity ({capacity} seats)",
        annotation_position="top left",
        annotation_font=dict(color="red"),
    )
    fig.add_hline(
        y=0.7, line_dash="dot",
        line=dict(color="orange", width=1.5),
        annotation_text=f"70% threshold ({int(capacity * 0.7)} seats)",
        annotation_position="bottom right",
        annotation_font=dict(color="orange"),
    )

    fig.update_layout(
        title=title,
        xaxis=dict(title="Date / Hour"),
        yaxis=dict(
            title="Utilisation Rate",
            tickformat=".0%",
            side="left",
            range=[0, util_max],
            showgrid=True,
            gridcolor="rgba(200,200,200,0.3)",
        ),
        yaxis2=dict(
            title="Est. Occupancy (visitors)",
            side="right",
            overlaying="y",
            range=[0, occupancy_max],
            rangemode="tozero",
            showgrid=False,
            tickformat=".0f",
        ),
        hovermode="x unified",
        height=480,
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="left", x=0,
        ),
    )
    return fig


def plot_distribution_by_hour(df: pd.DataFrame) -> go.Figure:
    """Box plot of utilisation rate distribution, one box per hour of day."""
    fig = go.Figure()
    for hour in range(24):
        subset = df[df["HOUR_OF_DAY"] == hour]["UTILISATION"].dropna()
        if subset.empty:
            continue
        fig.add_trace(go.Box(
            y=subset,
            name=f"{hour:02d}:00",
            marker=dict(color=COLORS["secondary"]),
            showlegend=False,
        ))

    fig.update_layout(
        title="Utilisation Rate Distribution by Hour of Day",
        xaxis_title="Hour of Day",
        yaxis_title="Utilisation Rate",
        yaxis_tickformat=".0%",
        height=420,
    )
    fig.add_hline(y=1.0, line_dash="dot", line=dict(color="red", width=1.5))
    fig.add_hline(y=0.7, line_dash="dot", line=dict(color="orange", width=1.5))
    return fig


def plot_peak_by_hour(df: pd.DataFrame) -> go.Figure:
    """Bar chart of peak (max) utilisation per hour of day."""
    peak = df.groupby("HOUR_OF_DAY")["UTILISATION"].max().reset_index()
    peak.columns = ["hour_of_day", "peak_utilisation"]

    colors_list = [
        COLORS["primary"] if v >= 1.0 else
        "orange" if v >= 0.7 else
        COLORS["aqua"]
        for v in peak["peak_utilisation"]
    ]

    fig = go.Figure(go.Bar(
        x=[f"{h:02d}:00" for h in peak["hour_of_day"]],
        y=peak["peak_utilisation"],
        marker=dict(color=colors_list),
        name="Peak Utilisation",
    ))

    fig.add_hline(y=1.0, line_dash="dot", line=dict(color="red", width=1.5),
                  annotation_text="100% capacity", annotation_position="top left")
    fig.add_hline(y=0.7, line_dash="dot", line=dict(color="orange", width=1.5),
                  annotation_text="70% threshold", annotation_position="top left")

    fig.update_layout(
        title="Peak Utilisation Rate per Hour of Day",
        xaxis_title="Hour of Day",
        yaxis_title="Peak Utilisation Rate",
        yaxis_tickformat=".0%",
        height=380,
    )
    return fig


def plot_heatmap_weekday_hour(
    df: pd.DataFrame,
    opening_hours_rows: list[dict],
) -> go.Figure:
    """
    Heatmap of peak utilisation rate by weekday (rows) and hour of day (columns).

    Non-opening-hour cells are masked (shown as grey / no colour) so only
    trading hours are visible.  Weekdays are ordered Mon → Sun.
    """
    DAY_ORDER  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    DAY_TO_DOW = {d: i for i, d in enumerate(DAY_ORDER)}

    # Build opening-hours mask: set of (dow_abbr, hour) that are open
    open_cells: set[tuple[str, int]] = set()
    if opening_hours_rows:
        for row in opening_hours_rows:
            day   = row.get("Day", "")
            open_s  = row.get("Open",  "—")
            close_s = row.get("Close", "—")
            if day not in DAY_TO_DOW or open_s == "—" or close_s == "—":
                continue
            try:
                oh = int(open_s.split(":")[0])
                ch = int(close_s.split(":")[0])
                if ch == 0:
                    ch = 24
            except (ValueError, IndexError):
                continue
            for h in range(oh, ch):
                open_cells.add((day, h))

    # Compute peak utilisation per (weekday_abbr, hour_of_day)
    df = df.copy()
    df["DAY_ABBR"]   = df["VISIT_SLOT"].dt.day_of_week.map(
        {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    )
    df["HOUR_OF_DAY"] = df["VISIT_SLOT"].dt.hour

    peak = (
        df.groupby(["DAY_ABBR", "HOUR_OF_DAY"])["UTILISATION"]
        .max()
        .reset_index()
    )

    # Pivot to (weekday × hour) matrix
    hours   = list(range(24))
    z       = []        # peak utilisation values (NaN = closed)
    z_text  = []        # annotation text

    for day in DAY_ORDER:
        row_vals = []
        row_text = []
        for h in hours:
            if opening_hours_rows and (day, h) not in open_cells:
                row_vals.append(float("nan"))
                row_text.append("")
            else:
                match = peak[(peak["DAY_ABBR"] == day) & (peak["HOUR_OF_DAY"] == h)]
                val = match["UTILISATION"].values[0] if not match.empty else float("nan")
                row_vals.append(val)
                row_text.append(f"{val:.0%}" if not math.isnan(val) else "")
        z.append(row_vals)
        z_text.append(row_text)

    fig = go.Figure(go.Heatmap(
        z=z,
        x=[f"{h:02d}:00" for h in hours],
        y=DAY_ORDER,
        text=z_text,
        texttemplate="%{text}",
        textfont=dict(size=10),
        colorscale="YlOrRd",
        zmin=0,
        zmax=1,
        colorbar=dict(
            title="Peak Utilisation",
            tickformat=".0%",
        ),
        hoverongaps=False,
        hovertemplate="<b>%{y} %{x}</b><br>Peak Utilisation: %{z:.1%}<extra></extra>",
    ))

    fig.update_layout(
        title="Peak Utilisation Rate by Weekday & Hour (opening hours only)",
        xaxis_title="Hour of Day",
        yaxis_title="Weekday",
        yaxis=dict(autorange="reversed"),   # Mon at top
        height=360,
    )
    return fig


def build_demo_flights(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate synthetic 15-min slot data for demo mode.

    Returns
    -------
    df_hist : pd.DataFrame
        Historical flight counts — mirrors counts_interval_historical_flights SQL.
    df_sched : pd.DataFrame
        Scheduled seat counts — mirrors counts_interval_schedule_flights SQL.
    """
    np.random.seed(99)
    slots = pd.date_range(start_dt, end_dt, freq="15min", inclusive="left")
    hod   = np.array(slots.hour)
    peak_mask = ((hod >= 6) & (hod < 10)) | ((hod >= 14) & (hod < 19))
    base_flights = np.random.poisson(lam=1, size=len(slots)) + peak_mask.astype(int) * 2

    df_hist = pd.DataFrame({
        "SLOT_START":       slots,
        "DEP_AIRPORT":      "DEMO",
        "DEP_TERMINAL":     "T1",
        "DEP_FLIGHT_COUNT": base_flights.astype(int),
    })

    total_seats = base_flights * np.random.randint(150, 220, size=len(slots))
    df_sched = pd.DataFrame({
        "SLOT_START":             slots,
        "DEP_AIRPORT":            "DEMO",
        "DEP_TERMINAL":           "T1",
        "TOTAL_SEATS":            total_seats.astype(int),
        "FIRST_CLASS_SEATS":      (total_seats * 0.05).astype(int),
        "BUSINESS_CLASS_SEATS":   (total_seats * 0.15).astype(int),
        "PREMIUM_ECONOMY_SEATS":  (total_seats * 0.10).astype(int),
        "ECONOMY_PLUS_SEATS":     (total_seats * 0.05).astype(int),
        "ECONOMY_CLASS_SEATS":    (total_seats * 0.65).astype(int),
    })

    return df_hist, df_sched


def plot_flight_timeseries(
    df_hist: pd.DataFrame,
    df_sched: pd.DataFrame | None,
    seat_col: str,
    cabin_label: str,
    title: str,
    upcoming_hour_for_count: int = 3,
) -> go.Figure:
    """
    Dual-axis time-series for departure flights.

    Left  axis : actual departure flight count (bars) from OAG actuals.
    Right axis : scheduled seat count for the selected cabin class (line)
                 from OAG schedule snapshots.

    Both datasets are aggregated to hourly buckets.
    Missing hours are imputed with 0 so the x-axis is continuous.
    """
    def _to_hourly_flights(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["HOUR"] = df["SLOT_START"].dt.floor("h")
        hourly = df.groupby("HOUR", as_index=False).agg(
            DEP_FLIGHT_COUNT=("DEP_FLIGHT_COUNT", "sum")
        )

        if not hourly.empty:
            full = pd.DataFrame({
                "HOUR": pd.date_range(hourly["HOUR"].min(), hourly["HOUR"].max(), freq="h")
            })
            hourly = full.merge(hourly, on="HOUR", how="left").fillna({"DEP_FLIGHT_COUNT": 0})
            hourly["DEP_FLIGHT_COUNT"] = hourly["DEP_FLIGHT_COUNT"].astype(int)

        upcoming_hourly = hourly.copy()
        upcoming_hourly["DEP_FLIGHT_COUNT"] = upcoming_hourly["DEP_FLIGHT_COUNT"].rolling(
            window=upcoming_hour_for_count, min_periods=1
        ).sum()

        return upcoming_hourly

    def _to_hourly_seats(df: pd.DataFrame, col_name: str) -> pd.DataFrame:
        df = df.copy()
        df["HOUR"] = df["SLOT_START"].dt.floor("h")

        hourly = df.groupby("HOUR", as_index=False)[col_name].sum()

        if not hourly.empty:
            full = pd.DataFrame({
                "HOUR": pd.date_range(hourly["HOUR"].min(), hourly["HOUR"].max(), freq="h")
            })
            hourly = full.merge(hourly, on="HOUR", how="left").fillna({col_name: 0})
            hourly[col_name] = hourly[col_name].astype(int)

        upcoming_hourly = hourly.copy()
        upcoming_hourly[col_name] = upcoming_hourly[col_name].rolling(
            window=upcoming_hour_for_count, min_periods=1
        ).sum()

        return upcoming_hourly

    hourly_hist_flight = _to_hourly_flights(df_hist)
    hourly_sched_seat = _to_hourly_seats(df_sched, seat_col)

    fig = go.Figure()

    # Left axis – actual flight count (bars)
    fig.add_trace(go.Bar(
        x=hourly_hist_flight["HOUR"],
        y=hourly_hist_flight["DEP_FLIGHT_COUNT"],
        name="Actual Departures (OAG actuals)",
        marker=dict(color=COLORS["secondary"], opacity=0.65),
        yaxis="y1",
        hovertemplate="<b>%{x}</b><br>Actual Flights: %{y}<extra></extra>",
    ))

    # Right axis – scheduled seat count (line), if schedule data is available
    if df_sched is not None and not df_sched.empty and seat_col in df_sched.columns:
        hourly_sched = _to_hourly_seats(df_sched, seat_col)
        fig.add_trace(go.Scatter(
            x=hourly_sched_seat["HOUR"],
            y=hourly_sched_seat[seat_col],
            mode="lines",
            name=f"Scheduled {cabin_label} Seats (OAG schedule)",
            line=dict(color=COLORS["primary"], width=2),
            yaxis="y2",
            hovertemplate=(
                "<b>%{x}</b><br>Scheduled " + cabin_label + " Seats: %{y:,}<extra></extra>"
            ),
        ))

    fig.update_layout(
        title=title,
        xaxis=dict(title="Date / Hour"),
        yaxis=dict(
            title="Actual Departure Flight Count",
            side="left",
            rangemode="tozero",
            showgrid=True,
            gridcolor="rgba(200,200,200,0.3)",
        ),
        yaxis2=dict(
            title=f"Scheduled {cabin_label} Seats",
            side="right",
            overlaying="y",
            rangemode="tozero",
            showgrid=False,
            tickformat=",",
        ),
        barmode="overlay",
        hovermode="x unified",
        height=480,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig


def _hourly_mean_utilisation(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 15-min utilisation to hourly mean; impute missing hours with 0."""
    d = df.copy()
    d["HOUR"] = d["VISIT_SLOT"].dt.floor("h")
    hourly = d.groupby("HOUR", as_index=False)["UTILISATION"].mean()
    if hourly.empty:
        return hourly
    full = pd.DataFrame({
        "HOUR": pd.date_range(hourly["HOUR"].min(), hourly["HOUR"].max(), freq="h"),
    })
    hourly = full.merge(hourly, on="HOUR", how="left").fillna({"UTILISATION": 0})
    return hourly


def _hourly_scheduled_seats_with_rolling(
    df_sched: pd.DataFrame,
    seat_col: str,
    upcoming_hour_for_count: int,
) -> pd.DataFrame:
    """Match flight tab: hourly sum of seats per cabin, then trailing rolling sum over N hours."""
    df = df_sched.copy()
    df["HOUR"] = df["SLOT_START"].dt.floor("h")
    hourly = df.groupby("HOUR", as_index=False)[seat_col].sum()
    if hourly.empty:
        return hourly
    full = pd.DataFrame({
        "HOUR": pd.date_range(hourly["HOUR"].min(), hourly["HOUR"].max(), freq="h"),
    })
    hourly = full.merge(hourly, on="HOUR", how="left").fillna({seat_col: 0})
    hourly[seat_col] = hourly[seat_col].astype(int)
    hourly = hourly.copy()
    hourly[seat_col] = hourly[seat_col].rolling(
        window=upcoming_hour_for_count, min_periods=1
    ).sum()
    return hourly


def plot_combined_utilisation_vs_scheduled_seats(
    df_util: pd.DataFrame,
    df_sched: pd.DataFrame | None,
    seat_col: str,
    cabin_label: str,
    capacity: int,
    title: str,
    upcoming_hour_for_count: int = 3,
) -> go.Figure:
    """
    Single chart: outlet utilisation rate (left axis) vs scheduled departure seats
    (right axis), both as hourly time series. Scheduled seats use the same trailing
    rolling-hour aggregation as the Departure Flights tab.
    """
    hourly_util = _hourly_mean_utilisation(df_util)
    fig = go.Figure()

    if not hourly_util.empty:
        fig.add_trace(go.Scatter(
            x=hourly_util["HOUR"],
            y=hourly_util["UTILISATION"],
            mode="lines",
            name="Outlet utilisation rate",
            line=dict(color=COLORS["primary"], width=2),
            yaxis="y1",
            hovertemplate="<b>%{x}</b><br>Utilisation: %{y:.1%}<extra></extra>",
        ))

    has_sched = (
        df_sched is not None
        and not df_sched.empty
        and seat_col in df_sched.columns
    )
    if has_sched:
        hourly_seats = _hourly_scheduled_seats_with_rolling(
            df_sched, seat_col, upcoming_hour_for_count
        )
        if not hourly_seats.empty:
            fig.add_trace(go.Scatter(
                x=hourly_seats["HOUR"],
                y=hourly_seats[seat_col],
                mode="lines",
                name=f"Scheduled {cabin_label} seats ({upcoming_hour_for_count}h roll-up)",
                line=dict(color=COLORS["secondary"], width=2, dash="dash"),
                yaxis="y2",
                hovertemplate=(
                    "<b>%{x}</b><br>Scheduled " + cabin_label + " seats: %{y:,}<extra></extra>"
                ),
            ))

    util_max_axis = (
        max(hourly_util["UTILISATION"].max() * 1.1, 1.05)
        if not hourly_util.empty else 1.05
    )

    fig.update_layout(
        title=title,
        xaxis=dict(title="Date / Hour"),
        yaxis=dict(
            title="Utilisation rate",
            tickformat=".0%",
            side="left",
            range=[0, util_max_axis],
            showgrid=True,
            gridcolor="rgba(200,200,200,0.3)",
        ),
        yaxis2=dict(
            title=f"Scheduled {cabin_label} seats (rolling)",
            side="right",
            overlaying="y",
            rangemode="tozero",
            showgrid=False,
            tickformat=",",
        ),
        hovermode="x unified",
        height=520,
        legend=dict(orientation="h", yanchor="bottom", y=1.06, xanchor="left", x=0),
    )

    if not hourly_util.empty:
        fig.add_hline(
            y=1.0, line_dash="dot",
            line=dict(color="red", width=1.5),
            annotation_text=f"100% capacity ({capacity} seats)",
            annotation_position="top left",
            annotation_font=dict(color="red"),
        )
        fig.add_hline(
            y=0.7, line_dash="dot",
            line=dict(color="orange", width=1.5),
            annotation_text=f"70% threshold ({int(capacity * 0.7)} seats)",
            annotation_position="bottom right",
            annotation_font=dict(color="orange"),
        )

    if hourly_util.empty and not has_sched:
        fig.update_layout(
            annotations=[dict(
                text="No utilisation or schedule data to plot",
                xref="paper", yref="paper",
                x=0.5, y=0.5, showarrow=False,
                font=dict(size=14),
            )],
        )
    return fig


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    st.markdown('<div class="main-header">🛋️ Outlet Hourly Utilisation Dashboard</div>', unsafe_allow_html=True)
    st.markdown("Utilisation Rate = Estimated Occupancy ÷ Total Seating Capacity")
    st.markdown("---")

    # ---- Sidebar --------------------------------------------------------
    st.sidebar.header("⚙️ Configuration")

    all_lounge_codes = config.get("target_lounge_codes", [])
    sql_templates = config.get("sql_templates", {})
    sql_lounge_info        = sql_templates.get("lounge_info",          "get_lounge_info.sql")
    sql_hourly_visits      = sql_templates.get("hourly_visits",        "visits/get_interval_visits_3months.sql")
    sql_historical_flights = sql_templates.get("historical_flights",   "flights/counts_interval_historical_flights_selected_terminal_by_hour.sql")
    sql_scheduled_seats    = sql_templates.get("scheduled_seats",      "flights/counts_interval_schedule_flights_selected_terminal_by_hour.sql")

    # Demo toggle – OFF by default (uses Snowflake for all data)
    # When enabled, only visit and flight data are replaced with synthetic
    # values; lounge metadata is always fetched from Snowflake so that the
    # airport → outlet mapping and opening hours are always accurate.
    use_demo = st.sidebar.checkbox("Use demo data (visits & flights only)", value=False)

    # ---- Snowflake: always connect to fetch lounge metadata -------------
    # Lounge info drives the airport filter, outlet list, capacity, and
    # opening hours — it must be fetched from Snowflake unconditionally.
    conn = get_snowflake_connection(config)
    if conn is None:
        st.error("❌ Could not connect to Snowflake. Check config.")
        st.stop()
    cursor = conn.cursor()
    flight_service_type = config.get("defaults", {}).get("flight_service_type", "J")

    with st.spinner("Loading outlet info from Snowflake…"):
        df_info = fetch_lounge_info(cursor, all_lounge_codes, sql_lounge_info)

    if df_info.empty:
        st.error("No lounge information returned from Snowflake. Check target_lounge_codes in config.")
        st.stop()

    # ---- Build airport → lounge mapping from get_lounge_info.sql -------
    # Derived entirely from the AIRPORT_CODE column in the SQL result —
    # no hardcoded mapping required in the config.
    airport_lounge_map: dict[str, list[str]] = {}
    for _, row in df_info.iterrows():
        apt  = str(row.get("AIRPORT_CODE") or "").strip().upper()
        code = str(row.get("CODE")         or "").strip()
        if apt and code:
            airport_lounge_map.setdefault(apt, [])
            if code not in airport_lounge_map[apt]:
                airport_lounge_map[apt].append(code)

    # ---- Airport filter selectbox ---------------------------------------
    sorted_airports = sorted(airport_lounge_map.keys())
    if not sorted_airports:
        sorted_airports = ["ALL"]
        airport_lounge_map["ALL"] = all_lounge_codes

    selected_airport = st.sidebar.selectbox(
        "Airport",
        options=sorted_airports,
        help="Filter outlets by airport. Only outlets at the selected airport are shown below.",
    )

    # ---- Outlet selectbox (filtered by selected airport) ----------------
    lounge_codes_at_airport = airport_lounge_map.get(selected_airport, all_lounge_codes)
    # Preserve only codes that are in the master list
    lounge_codes_at_airport = [c for c in all_lounge_codes if c in lounge_codes_at_airport]
    if not lounge_codes_at_airport:
        lounge_codes_at_airport = all_lounge_codes

    selected_lounge = st.sidebar.selectbox("Outlet", lounge_codes_at_airport)

    # ---- Extract per-lounge metadata from df_info -----------------------
    sf_capacity: int | None = None
    opening_hours_rows: list[dict] = []
    airport_code: str | None = None
    # terminal: str | None = None

    lounge_row = df_info[df_info["CODE"] == selected_lounge]
    if not lounge_row.empty:
        seats_val = lounge_row.iloc[0].get("NUMBER_OF_SEATS")
        if seats_val is not None and not (isinstance(seats_val, float) and math.isnan(seats_val)):
            sf_capacity = int(seats_val)
        airport_code = lounge_row.iloc[0].get("AIRPORT_CODE") or None
        terminal     = lounge_row.iloc[0].get("TERMINAL")     or None

        row = lounge_row.iloc[0]
        day_map = [
            ("Mon", "MONDAY"),   ("Tue", "TUESDAY"),  ("Wed", "WEDNESDAY"),
            ("Thu", "THURSDAY"), ("Fri", "FRIDAY"),   ("Sat", "SATURDAY"),
            ("Sun", "SUNDAY"),
        ]
        for short, col in day_map:
            open_t  = row.get(f"{col}_OPEN_TIME",  None)
            close_t = row.get(f"{col}_CLOSE_TIME", None)
            opening_hours_rows.append({
                "Day":   short,
                "Open":  str(open_t)[:5]  if open_t  else "—",
                "Close": str(close_t)[:5] if close_t else "—",
            })

    # ---- Capacity input – seeded from Snowflake NUMBER_OF_SEATS ---------
    capacity_overrides = config.get("capacity_overrides", {})
    default_cap = sf_capacity or capacity_overrides.get(selected_lounge) or 100
    capacity_help = (
        f"Auto-populated from SF_PARTNERSHIP (NUMBER_OF_SEATS = {sf_capacity})."
        if sf_capacity else "Manually set – Snowflake value not available."
    )
    capacity = st.sidebar.number_input(
        "Total Seating Capacity",
        min_value=1, max_value=2000,
        value=default_cap, step=1,
        help=capacity_help,
    )

    # ---- Dwell time in minutes (15-min step) ----------------------------
    dwell_time_mins = st.sidebar.slider(
        "Dwell Time (minutes)",
        min_value=15, max_value=360,
        value=config.get("defaults", {}).get("dwell_time_mins", 60),
        step=15,
        help=(
            "Assumed average time a guest stays in the lounge. "
            "Occupancy at each 15-min slot = rolling sum of arrivals "
            "over the preceding N slots, where N = dwell_time / 15."
        ),
    )
    rolling_slots = max(1, math.ceil(dwell_time_mins / SLOT_MINS))

    # ---- Lookback period ------------------------------------------------
    lookback_months = st.sidebar.slider(
        "Lookback Period (months)",
        min_value=1, max_value=6,
        value=config.get("defaults", {}).get("lookback_months", 3),
    )
    if opening_hours_rows:
        st.sidebar.markdown("---")
        st.sidebar.markdown("**🕐 Opening Hours**")
        st.sidebar.dataframe(
            pd.DataFrame(opening_hours_rows),
            hide_index=True,
            use_container_width=True,
        )

    # ---- Data loading ---------------------------------------------------
    df_visits: pd.DataFrame | None = None
    df_flights: pd.DataFrame | None = None
    df_scheduled: pd.DataFrame | None = None

    if use_demo:
        np.random.seed(42)
        end_dt   = pd.Timestamp.now().normalize()
        start_dt = end_dt - pd.DateOffset(months=lookback_months)
        slots    = pd.date_range(start_dt, end_dt, freq="15min")
        base     = np.random.poisson(lam=8, size=len(slots))
        hod      = np.array(slots.hour)
        slot_effect = np.sin((hod - 6) * np.pi / 12).clip(0) * 10
        visits   = (base + slot_effect).astype(int).clip(0)
        df_visits  = pd.DataFrame({"VISIT_SLOT": slots, "TOTAL_VISITS": visits})
        df_flights, df_scheduled = build_demo_flights(start_dt, end_dt)
        st.sidebar.success("✅ Demo visits & flights loaded (outlet metadata from Snowflake).")
    else:
        lag_days = config.get("defaults", {}).get("visit_data_lag_days", 5)
        end_dt   = dt.datetime.now() - dt.timedelta(days=lag_days)
        start_dt = end_dt - dt.timedelta(days=30 * lookback_months)
        start_str = start_dt.date().strftime("%Y-%m-%d %H:%M:%S")
        end_str   = end_dt.date().strftime("%Y-%m-%d %H:%M:%S")

        with st.spinner("Fetching visit data from Snowflake…"):
            df_visits = fetch_hourly_visits(
                cursor, selected_lounge, start_str, end_str, sql_hourly_visits,
            )
        if df_visits.empty:
            st.warning(f"No visit data found for outlet **{selected_lounge}**.")
            st.stop()
        st.sidebar.success(f"✅ {len(df_visits):,} 15-min slots loaded.")

        if airport_code:
            with st.spinner("Fetching historical flight data from Snowflake…"):
                df_flights = fetch_historical_flights(
                    cursor, airport_code, start_str, end_str,
                    flight_service_type, sql_historical_flights,
                )
            with st.spinner("Fetching scheduled seat data from Snowflake…"):
                df_scheduled = fetch_scheduled_seats(
                    cursor, airport_code, start_str, end_str,
                    flight_service_type, sql_scheduled_seats,
                )
        else:
            st.sidebar.warning("⚠️ Airport code not found for this outlet – flight data unavailable.")

    # ---- Compute utilisation -------------------------------------------
    df = compute_utilisation(df_visits, dwell_time_mins, capacity)

    df_open  = filter_to_opening_hours(df, opening_hours_rows)
    avg_util = df_open["UTILISATION"].mean() if not df_open.empty else df["UTILISATION"].mean()
    peak_util = df["UTILISATION"].max()
    open_hours_available = bool(opening_hours_rows)

    # ---- Top KPI row (shared across tabs) ------------------------------
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Outlet", selected_lounge)
    col2.metric("Capacity (seats)", f"{capacity:,}")
    col3.metric("Dwell Time", f"{dwell_time_mins} min")
    col4.metric(
        "Avg Utilisation" + (" (opening hrs)" if open_hours_available else ""),
        f"{avg_util:.1%}",
        delta=f"Peak {peak_util:.1%}",
    )

    st.markdown("---")

    # ---- Tabs ----------------------------------------------------------
    tab_util, tab_flights, tab_combined = st.tabs([
        "📊 Utilisation",
        "✈️ Departure Flights",
        "🔗 Combined view",
    ])

    # ================================================================
    # TAB 1 – Utilisation
    # ================================================================
    with tab_util:
        st.markdown('<div class="sub-header">📈 Hourly Utilisation Rate – Time Series</div>',
                    unsafe_allow_html=True)
        fig_ts = plot_timeseries(
            df, capacity,
            title=f"{selected_lounge} — Hourly Utilisation  |  dwell={dwell_time_mins} min  |  capacity={capacity} seats",
        )
        st.plotly_chart(fig_ts, use_container_width=True)

        st.markdown("---")

        col_left, col_right = st.columns(2)
        with col_left:
            st.markdown('<div class="sub-header">📊 Distribution by Hour of Day</div>', unsafe_allow_html=True)
            st.caption("Each box shows the spread of hourly utilisation rates across all days in the period.")
            st.plotly_chart(plot_distribution_by_hour(df), use_container_width=True)

        with col_right:
            st.markdown('<div class="sub-header">🔝 Peak Utilisation per Hour of Day</div>', unsafe_allow_html=True)
            st.caption("The highest utilisation rate ever observed for each hour. "
                       "Red = over capacity, Orange = ≥70%, Teal = <70%.")
            st.plotly_chart(plot_peak_by_hour(df), use_container_width=True)

        st.markdown("---")

        st.markdown('<div class="sub-header">🗓️ Peak Utilisation Heatmap – Weekday × Hour</div>',
                    unsafe_allow_html=True)
        st.caption(
            "Peak (maximum) utilisation rate observed for each weekday–hour combination "
            "over the lookback period. Non-opening-hour cells are hidden."
        )
        st.plotly_chart(plot_heatmap_weekday_hour(df, opening_hours_rows), use_container_width=True)

        st.markdown("---")

        with st.expander("🗂️ Raw Utilisation Data"):
            st.dataframe(
                df[["VISIT_SLOT", "TOTAL_VISITS", "EST_OCCUPANCY", "UTILISATION", "HOUR_OF_DAY"]]
                .rename(columns={
                    "VISIT_SLOT":    "15-min Slot",
                    "TOTAL_VISITS":  "Arrivals",
                    "EST_OCCUPANCY": "Est. Occupancy",
                    "UTILISATION":   "Utilisation Rate",
                    "HOUR_OF_DAY":   "Hour of Day",
                })
                .style.format({"Utilisation Rate": "{:.1%}", "Est. Occupancy": "{:.0f}"}),
                use_container_width=True,
            )

    # ================================================================
    # TAB 2 – Departure Flights
    # ================================================================
    with tab_flights:
        if df_flights is None or df_flights.empty:
            st.info("No flight data available for this outlet.")
        else:
            total_flights = int(df_flights["DEP_FLIGHT_COUNT"].sum())
            c1, c2, c3, c4 = st.columns(4)

            with c1:
                selected_cabin = st.selectbox(
                    "Cabin Class", config.get("cabin_classes", ["Total"])
                )
                selected_cabin_col = selected_cabin.upper()
                selected_cabin_col = (
                    f"{selected_cabin_col}_CLASS" if selected_cabin_col != "TOTAL" else selected_cabin_col
                )
                selected_cabin_col = f"{selected_cabin_col}_SEATS"

            with c2:
                upcoming_hour_for_flight_count = st.slider(
                    "Number of upcoming hours for flight/seat count",
                    min_value=1, max_value=5,
                    value=config.get("upcoming_hour_for_flight_count"),
                )


            c3.metric("Airport", airport_code or "DEMO")
            c4.metric("Total Departing Flights", f"{total_flights:,}")

            st.markdown("---")

            st.markdown('<div class="sub-header">✈️ Departure Flight Count – Time Series</div>',
                        unsafe_allow_html=True)
            st.caption(
                "Hourly departure flight count sourced from OAG actuals "
                "(SQ_OAG__ACTUALS). Hours with no recorded departures are shown as 0."
            )
            fig_fl = plot_flight_timeseries(
                df_flights,
                df_scheduled,
                seat_col=selected_cabin_col,
                cabin_label=selected_cabin,
                title=f"{airport_code or 'Demo'} — Departures  |  service type: {flight_service_type}",
                upcoming_hour_for_count=upcoming_hour_for_flight_count,
            )
            st.plotly_chart(fig_fl, use_container_width=True)

            st.markdown("---")

            with st.expander("🗂️ Raw Flight Data"):
                display_cols = ["SLOT_START", "DEP_AIRPORT", "DEP_TERMINAL", "DEP_FLIGHT_COUNT"]
                available = [c for c in display_cols if c in df_flights.columns]
                st.dataframe(df_flights[available], use_container_width=True)

    # ================================================================
    # TAB 3 – Combined: utilisation vs scheduled seats
    # ================================================================
    with tab_combined:
        st.markdown(
            '<div class="sub-header">🔗 Outlet utilisation vs scheduled departure seats</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Hourly mean lounge utilisation (left axis) compared with scheduled seat volume "
            "from OAG schedule snapshots (right axis), using the same trailing rolling window "
            "as the Departure Flights tab."
        )

        if df_scheduled is None or df_scheduled.empty:
            st.warning(
                "No scheduled seat data for this outlet or airport — only utilisation is available below."
            )

        cabin_opts = config.get("cabin_classes", ["Total"])
        cc1, cc2 = st.columns(2)
        with cc1:
            comb_cabin = st.selectbox("Cabin class (scheduled seats)", cabin_opts, key="comb_cabin")
        comb_cabin_col = comb_cabin.upper()
        comb_cabin_col = (
            f"{comb_cabin_col}_CLASS" if comb_cabin_col != "TOTAL" else comb_cabin_col
        )
        comb_cabin_col = f"{comb_cabin_col}_SEATS"
        with cc2:
            comb_upcoming_h = st.slider(
                "Rolling hours (scheduled seats)",
                min_value=1,
                max_value=5,
                value=config.get("upcoming_hour_for_flight_count", 3),
                key="comb_upcoming_h",
                help="Trailing sum of scheduled seats over this many hours (matches Departure Flights).",
            )

        fig_comb = plot_combined_utilisation_vs_scheduled_seats(
            df,
            df_scheduled,
            seat_col=comb_cabin_col,
            cabin_label=comb_cabin,
            capacity=capacity,
            title=(
                f"{selected_lounge} @ {airport_code or '—'} — Utilisation vs scheduled {comb_cabin} seats "
                f" | dwell={dwell_time_mins} min | roll-up={comb_upcoming_h} h"
            ),
            upcoming_hour_for_count=comb_upcoming_h,
        )
        st.plotly_chart(fig_comb, use_container_width=True)


if __name__ == "__main__":
    main()


