import datetime as dt
import logging
import os
from pathlib import Path
import time
from typing import List
import warnings

import pandas as pd

import snowflake.connector  # For local run only
import yaml

# (TODO) NOTE: need to set python path environment in order to import from parent directory
# from exc import InvalidParameters
# from helpers import SnowflakeManager


logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")


class DataIssue(Exception):
    """
    Custom exception to indicate issues with the input data, such as missing columns, empty DataFrame, or invalid data types.
    """
    pass


def impute_zero_visits(visit_df: pd.DataFrame) -> pd.DataFrame:
    """
    Impute zero visits for missing time slots in the visit_df
    (single outlet's 15-min visit data) to create a continuous time series of visits.

    Parameters
    ----------
    visit_df
        DataFrame with columns: visit_slot (datetime), total_visits (int)

    Returns
    -------
    pd.DataFrame
        DataFrame with continuous time slots and zero visits imputed for missing slots.
    """

    if visit_df is None or visit_df.empty:
        raise DataIssue(
            "visit_df is empty or None. Please provide a valid non-empty DataFrame with visit data."
        )

    # Validate required columns
    required_cols = {"visit_slot", "total_visits"}
    missing = required_cols - set(visit_df.columns)
    if missing:
        raise ValueError(
            f"visit_df is missing required columns: {missing}. "
            f"Available columns: {list(visit_df.columns)}"
        )

    visit_df["visit_slot"] = pd.to_datetime(visit_df["visit_slot"])
    visit_df = visit_df.sort_values("visit_slot").reset_index(drop=True)

    start_date = visit_df["visit_slot"].min().floor("D")
    end_date = visit_df["visit_slot"].max().ceil("D")
    end_date = end_date - pd.Timedelta(minutes=15)  # Include the last day up to 23:45

    # Create a complete time range based on the min and max visit_slot
    full_time_range = pd.date_range(
        start=start_date,
        end=end_date,
        freq="15T"  # Assuming 15-minute intervals
    )

    # Reindex the DataFrame to include all time slots, filling missing total_visits with 0
    imputed_visit_df = (
        visit_df.set_index("visit_slot")
        .reindex(full_time_range, fill_value=0)
        .rename_axis("visit_slot")
        .reset_index()
    )

    # NOTE: only work for single outlet's data, need to add correct outlet_code back after imputation
    outlet_code = visit_df["outlet_code"].iloc[0]
    imputed_visit_df["outlet_code"] = outlet_code

    return imputed_visit_df


def compute_occupancy_from_visit(
    visit_df: pd.DataFrame,
    dwell_time_minutes: float,
    visit_interval: int = 15
) -> pd.DataFrame:
    """
    Compute estimate_occupancy from total_visits using a rolling window.

    Parameters
    ---------
    visit_df
        DataFrame with columns: visit_slot (datetime), total_visits (int)
    dwell_time_minutes
        expected dwell time of a visitor in the outlet, in minutes (e.g. 60)
    visit_interval
        length of each time window in minutes (e.g. 15) in the visit count data (visit_df).
        This is used to determine how many subsequent windows a visit contributes to based on the dwell time.

    """

    if visit_df is None or visit_df.empty:
        raise DataIssue(
            "visit_df is empty or None. Please provide a valid non-empty DataFrame with visit data."
        )

    # Validate required columns
    required_cols = {"visit_slot", "total_visits"}
    missing = required_cols - set(visit_df.columns)
    if missing:
        raise ValueError(
            f"visit_df is missing required columns: {missing}. "
            f"Available columns: {list(visit_df.columns)}"
        )

    occupancy_df = visit_df.copy()
    occupancy_df["visit_slot"] = pd.to_datetime(occupancy_df["visit_slot"])
    occupancy_df = occupancy_df.sort_values("visit_slot").reset_index(drop=True)

    # Number of 15-min windows a visitor occupies based on dwell time
    # e.g. dwell_time=60, interval=15 → windows=4
    windows = max(1, int(dwell_time_minutes // visit_interval))   # TODO: use math.ceil if not divisible

    # logger.info(
    #     f"Computing occupancy with dwell_time={dwell_time_minutes} mins, "
    #     f"count_interval={visit_interval} mins, "
    #     f"rolling_windows={windows}"
    # )

    # Rolling sum: each window's occupancy = sum of visits
    # across the current and previous (windows-1) intervals
    occupancy_df["estimate_occupancy"] = (
        occupancy_df["total_visits"]
        .rolling(window=windows, min_periods=1)
        .sum()
        .astype(int)
    )

    # Sanity check: occupancy should not be less than total visits in the same slot
    assert (occupancy_df["estimate_occupancy"] >= occupancy_df["total_visits"]).all(), (
        "Estimated occupancy should be greater than or equal to total visits in the same slot. "
        "Please check the input data and parameters."
    )

    return occupancy_df


def get_hourly_max(
    df: pd.DataFrame,
    agg_col: str = "estimate_occupancy"
) -> pd.DataFrame:
    """
    Get hourly max from the 15-min interval agg_col data

    Parameters
    ----------
    df
        DataFrame with columns: visit_slot (datetime)

    Returns
    -------
    pd.DataFrame
        DataFrame with columns: hour_slot (datetime)
    """

    if df is None or df.empty:
        raise DataIssue(
            "occupancy_df is empty or None. Please provide a valid non-empty DataFrame with occupancy data."
        )

    # Validate required columns
    required_cols = {"visit_slot", agg_col}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"df is missing required columns: {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    hourly_max_df = (
        df.groupby(df["visit_slot"].dt.floor("H"))[agg_col]
        .max()
        .reset_index()
        .rename(columns={"visit_slot": "visit_hour"})
    )

    return hourly_max_df



def compute_utilisation_rate(
    occupancy_df: pd.DataFrame,
    capacity: int
) -> pd.DataFrame:
    """
    Compute utilisation rate based on occupancy and capacity

    Parameters
    ----------
    occupancy_df
        DataFrame with columns: visit_slot (datetime), estimate_occupancy (int)
    capacity
        maximum capacity of the outlet (int)

    Returns
    -------
    pd.DataFrame
        DataFrame with columns: visit_slot (datetime), estimate_occupancy (int), utilisation_rate (float)
    """

    if occupancy_df is None or occupancy_df.empty:
        raise DataIssue(
            "occupancy_df is empty or None. Please provide a valid non-empty DataFrame with occupancy data."
        )

    # Validate required columns
    required_cols = {"visit_hour", "estimate_occupancy"}
    missing = required_cols - set(occupancy_df.columns)
    if missing:
        raise ValueError(
            f"occupancy_df is missing required columns: {missing}. "
            f"Available columns: {list(occupancy_df.columns)}"
        )

    if capacity <= 0:
        raise ValueError("Capacity must be a positive integer.")

    occupancy_and_utilisation_df = occupancy_df.copy()
    occupancy_and_utilisation_df["utilisation_rate"] = occupancy_and_utilisation_df["estimate_occupancy"] / capacity

    return occupancy_and_utilisation_df

def compute_weekday_and_hour_summary(
    df: pd.DataFrame,
    agg_col: str = "utilisation_rate"
) -> pd.DataFrame:
    """
    Compute average and peak utilisation rate/estimate occupancy by weekday and hour

    Parameters
    ----------
    df
        DataFrame with columns: visit_hour (datetime), and the column to aggregate

    Returns
    -------
    pd.DataFrame
        DataFrame with columns: weekday, hour (int),
        [avg_utilisation_rate (float), peak_utilisation_rate (float)]
        or [avg_estimate_occupancy (float), peak_estimate_occupancy (float)]

    """

    if df is None or df.empty:
        raise DataIssue(
            "df is empty or None. Please provide a valid non-empty DataFrame with data."
        )

    # Validate required columns
    required_cols = {"visit_hour", agg_col}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"Given dataframe is missing required columns: {missing}. "
            f"Available columns: {list(df.columns)}"
        )

    df["weekday_numeric"] = df["visit_hour"].dt.weekday
    df["weekday"] = df["visit_hour"].apply(lambda x: x.strftime("%a"))
    df["hour"] = df["visit_hour"].apply(lambda x: f"{x.hour:02d}:00")

    avg_weekday_hour_df = (
        df.groupby(["weekday", "weekday_numeric", "hour"])[agg_col]
        .mean()
        .reset_index()
        .rename(columns={agg_col: f"avg_{agg_col}"})
    )
    avg_weekday_hour_df[f"avg_{agg_col}"] = avg_weekday_hour_df[f"avg_{agg_col}"].round(2)
    peak_weekday_hour_df = (
        df.groupby(["weekday", "weekday_numeric", "hour"])[agg_col]
        .max()
        .reset_index()
        .rename(columns={agg_col: f"peak_{agg_col}"})
    )

    weekday_hour_summary_df = pd.merge(
        avg_weekday_hour_df,
        peak_weekday_hour_df,
        on=["weekday", "weekday_numeric", "hour"]
    )
    weekday_hour_summary_df.sort_values(by=["weekday_numeric", "hour"], inplace=True)
    weekday_hour_summary_df.drop(columns=["weekday_numeric"], inplace=True)

    return weekday_hour_summary_df

def impute_zero_flights(flight_df: pd.DataFrame) -> pd.DataFrame:
    """
    Impute zero flights for missing time slots in the flight_df
    (15-min flight count data) to create a continuous time series of flights.

    Parameters
    ---------
    flight_df
        DataFrame with columns: flight_time (datetime), flight_count (int)
        [and seat count columns if available]

    """

    if flight_df is None or flight_df.empty:
        raise DataIssue(
            "flight_df is empty or None. Please provide a valid non-empty DataFrame with flight data."
        )

    # Validate required columns
    required_cols = {"visit_slot", "departure_flight_count"}
    missing = required_cols - set(flight_df.columns)
    if missing:
        raise ValueError(
            f"flight_df is missing required columns: {missing}. "
            f"Available columns: {list(flight_df.columns)}"
        )

    flight_df["visit_slot"] = pd.to_datetime(flight_df["visit_slot"])
    flight_df = flight_df.sort_values("visit_slot").reset_index(drop=True)

    start_date = flight_df["visit_slot"].min().floor("D")
    end_date = flight_df["visit_slot"].max().ceil("D")
    end_date = end_date - pd.Timedelta(minutes=15)  # Include the last day up to 23:45

    ## TODO: add aggregation/impute by terminal as well

    # Create a complete time range based on the min and max visit_slot
    full_time_range = pd.date_range(
        start=start_date,
        end=end_date,
        freq="15T"  # Assuming 15-minute intervals
    )

    # Reindex the DataFrame to include all time slots, filling missing flight_count with 0
    imputed_flight_df = (
        flight_df.set_index("visit_slot")
        .reindex(full_time_range, fill_value=0)
        .rename_axis("visit_slot")
        .reset_index()
    )

    # NOTE: only work for single airport's data, need to add correct airport code back after imputation
    airport_code = flight_df["airport_code"].iloc[0]
    imputed_flight_df["airport_code"] = airport_code

    return imputed_flight_df



def compute_upcoming_air_traffic_from_flights(
    flight_df: pd.DataFrame,
    upcoming_hours: int,
    flight_interval: int = 15,

) -> pd.DataFrame:
    """
    Compute upcoming air traffic (number of flights) in the next N hours based on the flight schedule/historical data.

    """

    if flight_df is None or flight_df.empty:
        raise DataIssue(
            "flight_df is empty or None. Please provide a valid non-empty DataFrame with flight data."
        )

    # Validate required columns
    required_cols = {"visit_slot", "departure_flight_count"}
    missing = required_cols - set(flight_df.columns)
    if missing:
        raise ValueError(
            f"flight_df is missing required columns: {missing}. "
            f"Available columns: {list(flight_df.columns)}"
        )

    upcoming_flight_df = flight_df.copy()
    upcoming_flight_df["visit_slot"] = pd.to_datetime(upcoming_flight_df["visit_slot"])
    upcoming_flight_df = upcoming_flight_df.sort_values("visit_slot").reset_index(drop=True)

    # Number of 15-min windows a visitor occupies based on dwell time
    # e.g. dwell_time=60, interval=15 → windows=4
    windows = max(1, int(upcoming_hours * 60 // flight_interval))   # TODO: use math.ceil if not divisible

    # logger.info(
    #     f"Computing occupancy with dwell_time={dwell_time_minutes} mins, "
    #     f"count_interval={visit_interval} mins, "
    #     f"rolling_windows={windows}"
    # )

    # Rolling sum: each window's flight count = sum of upcoming flights
    # across the current and previous (windows-1) intervals
    upcoming_flight_df["upcoming_flight_count"] = (
        upcoming_flight_df["departure_flight_count"]
        .rolling(window=windows, min_periods=1)
        .sum()
        .astype(int)
    )

    is_seat_included = [x for x in flight_df.columns if "seat" in x]
    if is_seat_included:
        for col in is_seat_included:
            upcoming_flight_df[col] = (
                upcoming_flight_df[col]
                .rolling(window=windows, min_periods=1)
                .sum()
                .astype(int)
            )

    # Sanity check: upcoming x hour flight count should not be less than flight count in the same slot
    assert (upcoming_flight_df["upcoming_flight_count"] >= upcoming_flight_df["departure_flight_count"]).all(), (
        "Upcoming departure flight count should be greater than or equal to departure flight count in the same slot. "
        "Please check the input data and parameters."
    )

    return upcoming_flight_df