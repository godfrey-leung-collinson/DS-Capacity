import math

import matplotlib.pyplot as plt


import pandas as pd
import plotly.graph_objects as go


def plot_heatmap_weekday_hour_utilisation_rate(
    utilisation_summary_df: pd.DataFrame,
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
            if day not in DAY_TO_DOW:
                continue
            if open_s == "—":
                open_s = "00:00"
            if close_s == "—":
                close_s = "24:00"
            try:
                oh = int(open_s.split(":")[0])
                ch = int(close_s.split(":")[0])
                ch_minute = int(close_s.split(":")[1])
                if ch == 0:
                    ch = 24
                if ch_minute != 0:
                    ch = ch + 1
                ch = min(ch, 24)
            except (ValueError, IndexError):
                continue
            for h in range(oh, ch):
                open_cells.add((day, h))

    # Compute peak utilisation per (weekday_abbr, hour_of_day)
    peak_df = utilisation_summary_df.copy()
    # df["DAY_ABBR"]   = df["VISIT_SLOT"].dt.day_of_week.map(
    #     {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    # )
    # df["HOUR_OF_DAY"] = df["VISIT_SLOT"].dt.hour
    #
    # peak = (
    #     df.groupby(["DAY_ABBR", "HOUR_OF_DAY"])["UTILISATION"]
    #     .max()
    #     .reset_index()
    # )

    # Pivot to (weekday × hour) matrix
    hours = list(range(24))
    # hours = [f"{h:02d}:00" for h in hours]

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
                hour_str = f"{h:02d}:00"
                match = peak_df[(peak_df["weekday"] == day) & (peak_df["hour"] == hour_str)]
                val = match["peak_utilisation_rate"].values[0] if not match.empty else float("nan")
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