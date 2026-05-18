import datetime as dt
import glob
import logging
import os
from pathlib import Path
import time
from typing import List
import warnings

import matplotlib.pyplot as plt
import pandas as pd
import yaml

from compute import (
    compute_occupancy_from_visit,
    compute_weekday_and_hour_summary,
    compute_upcoming_air_traffic_from_flights,
    compute_utilisation_rate,
    get_hourly_max,
    impute_zero_visits,
    impute_zero_flights,
)
from plot import plot_heatmap_weekday_hour_utilisation_rate
from utility import get_opening_hours_list, get_weekday_hour_is_open_flag


# only for local run when storing the extracted data to local
code_dir = Path(__file__).parent.parent.parent
directory = code_dir.parent

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")


if __name__ == "__main__":
    start_time = time.time()

    is_to_export = False
    is_run_part_one = True

    try:

        # Load parent main config
        with open(code_dir / "config/compute_utilisation_and_airport_traffic.yaml", "r") as f:
            config = yaml.safe_load(f)

        path_config = config["paths"]
        data_filepaths = path_config["data"]

        logger.info(
            "Loading outlet information data from local ..."
        )

        outlet_info_filepath = directory / data_filepaths["outlet_info"]
        outlet_info_df = pd.read_csv(outlet_info_filepath)
        outlet_info_df.columns = [col.lower() for col in outlet_info_df.columns]

        if is_run_part_one:

            logger.info(
                "Part I) Computing outlet hourly utilisation rate ..."
            )

            visit_filepath_dir = directory / data_filepaths["visit"]

            logger.info(
                "Loading fetched 15-min visit data from local ..."
            )

            visit_filepaths = glob.glob(str(visit_filepath_dir / "*.csv"))

            visit_df = pd.DataFrame()
            for filepath in visit_filepaths:
                temp_df = pd.read_csv(filepath)
                visit_df = pd.concat([visit_df, temp_df])

            visit_df.columns = [col.lower() for col in visit_df.columns]

            visit_df.reset_index(drop=True, inplace=True)

            outlet_codes = visit_df["outlet_code"].unique().tolist()
            outlet_codes = sorted(outlet_codes)

            hourly_utilisation_all_df = pd.DataFrame()
            utilisation_rate_summary_all_df = pd.DataFrame()
            occupancy_summary_all_df = pd.DataFrame()

            no_capacity_outlets = []

            for outlet_code in outlet_codes:

                logger.info(f"Processing outlet {outlet_code} ...")

                outlet_visit_df = visit_df[visit_df["outlet_code"] == outlet_code].copy()

                logger.info(
                    "Imputing zero visit period ..."
                )

                outlet_visit_df = impute_zero_visits(outlet_visit_df)

                logger.info(
                    "Computing estimate occupancy from visit assuming a fixed dwell time ..."
                )

                dwell_time_estimate = config["dwell_time"]

                occupancy_df = compute_occupancy_from_visit(
                    outlet_visit_df,
                    dwell_time_minutes=dwell_time_estimate,
                    visit_interval=config["visit_interval"]
                )

                logger.info(
                    "Computing hourly estimate occupancy ..."
                )

                hourly_occupancy_df = get_hourly_max(occupancy_df, "estimate_occupancy")

                occupancy_summary_df = compute_weekday_and_hour_summary(
                    hourly_occupancy_df, "estimate_occupancy"
                )

                outlet_info = outlet_info_df[outlet_info_df["outlet_code"] == outlet_code]
                outlet_info_row = outlet_info.iloc[0]

                # add is_open hour flag for each weekday and hour based on the opening hours info in the outlet info dataframe
                opening_hours_list = get_opening_hours_list(outlet_info_row)
                is_open_flag_by_weekday_and_hour = get_weekday_hour_is_open_flag(opening_hours_list)
                occupancy_summary_df["is_open"] = occupancy_summary_df.apply(
                    lambda row: is_open_flag_by_weekday_and_hour.get(row["weekday"], {}).get(row["hour"], False),
                    axis=1
                )

                occupancy_summary_all_df = pd.concat([
                    occupancy_summary_all_df, occupancy_summary_df.assign(outlet_code=outlet_code)
                ])

                logger.info(
                    "Computing hourly utilisation rate ..."
                )

                total_seating_capacity = outlet_info["number_of_seats"].values[0]

                if total_seating_capacity == 0 or pd.isna(total_seating_capacity):
                    logger.warning(
                        f"Outlet {outlet_code} has invalid or missing seating capacity ({total_seating_capacity}). Skipping utilisation rate computation."
                    )

                    no_capacity_outlets.append(outlet_code)
                    continue

                hourly_utilisation_rate_df = compute_utilisation_rate(
                    hourly_occupancy_df, total_seating_capacity
                )

                utilisation_rate_summary_df = compute_weekday_and_hour_summary(
                    hourly_utilisation_rate_df, "utilisation_rate"
                )
                utilisation_rate_summary_df["is_open"] = utilisation_rate_summary_df.apply(
                    lambda row: is_open_flag_by_weekday_and_hour.get(row["weekday"], {}).get(row["hour"], False),
                    axis=1
                )

                hourly_utilisation_all_df = pd.concat(
                    [hourly_utilisation_all_df, hourly_utilisation_rate_df.assign(outlet_code=outlet_code)]
                )
                utilisation_rate_summary_all_df = pd.concat(
                    [utilisation_rate_summary_all_df, utilisation_rate_summary_df.assign(outlet_code=outlet_code)]
                )

            # fig = plot_heatmap_weekday_hour_utilisation_rate(
            #     utilisation_rate_summary_df,
            #     opening_hours_list,
            # )
            # # fig.show(renderer="png", width=800, height=300)
            #
            # fig.write_image(
            #     directory / f"data/images/utilisation_heatmap_test_{outlet_code}.png",
            #     width=800,
            # )

            print("Number of outlets with missing or zero seating capacity: {}".format(len(no_capacity_outlets)))

            percent_of_no_capacity = (len(no_capacity_outlets) / len(outlet_codes)) * 100
            print("% of outlets with missing or zero seating capacity: {}%".format(round(percent_of_no_capacity, 1)))

            if is_to_export:
                logger.info(
                    "Export the hourly estimate occupancy, utilisation rate weekday x hour summary to local ..."
                )

                filename_config = config["filename_base"]
                export_dir = directory / path_config["export_directory"]

                min_date = hourly_utilisation_all_df["visit_hour"].dt.date.min()
                max_date = hourly_utilisation_all_df["visit_hour"].dt.date.max()

                # Creating Excel Writer Object to export the processed results
                excel_filepath = export_dir / f"{filename_config['excel']}_{min_date}_to_{max_date}.xlsx"

                with pd.ExcelWriter(excel_filepath, engine="xlsxwriter") as writer:
                    occupancy_summary_all_df.to_excel(writer, sheet_name="occupancy_summary", index=False)
                    utilisation_rate_summary_all_df.to_excel(writer, sheet_name="utilisation_summary", index=False)

                    outlet_info_to_export = outlet_info_df[
                        [
                            "outlet_code", "outlet_name", "airport_code", "terminal", "number_of_seats",
                            "iso_country_code", "outlet_type", "account_status", "airside_landside"
                        ]
                    ]
                    outlet_info_to_export = outlet_info_to_export[
                        outlet_info_to_export["outlet_code"].isin(outlet_codes)
                    ]

                    outlet_info_to_export.to_excel(writer, sheet_name="outlet_info", index=False)

        logger.info(
            "Part II) Computing airport traffic and pressure index ..."
        )

        upcoming_hours_to_consider = config["relevant_upcoming_hours"]

        logger.info(
            "Loading fetched 15-min historical flight data from local ..."
        )

        historical_flights_filepath_dir = directory / data_filepaths["historical_flights"]
        historical_flights_filepaths = glob.glob(str(historical_flights_filepath_dir / "*.csv"))

        hist_flight_df = pd.DataFrame()
        for filepath in historical_flights_filepaths:
            temp_df = pd.read_csv(filepath)
            hist_flight_df = pd.concat([hist_flight_df, temp_df])

        hist_flight_df.columns = [col.lower() for col in hist_flight_df.columns]
        hist_flight_df.reset_index(drop=True, inplace=True)

        logger.info(
            "Loading fetched scheduled flight seat count data from local ..."
        )

        sched_flights_filepath_dir = directory / data_filepaths["scheduled_flights"]
        sched_flights_filepaths = glob.glob(str(sched_flights_filepath_dir / "*.csv"))

        scheduled_flight_df = pd.DataFrame()
        for filepath in sched_flights_filepaths:
            temp_df = pd.read_csv(filepath)
            scheduled_flight_df = pd.concat([scheduled_flight_df, temp_df])

        scheduled_flight_df.columns = [col.lower() for col in scheduled_flight_df.columns]
        scheduled_flight_df.reset_index(drop=True, inplace=True)

        scheduled_flight_df["visit_slot"] = pd.to_datetime(scheduled_flight_df["visit_slot"])

        scheduled_airport_codes = set(scheduled_flight_df["airport_code"].unique().tolist())
        historical_airport_codes = set(hist_flight_df["airport_code"].unique().tolist())

        airport_codes = scheduled_airport_codes.union(historical_airport_codes)
        airport_codes = sorted(airport_codes)

        logger.info(f"Number of airports to be processed: {len(airport_codes)}.")

        flight_count_summary_all_df = pd.DataFrame()
        seat_count_summary_all_df = pd.DataFrame()

        for airport_code in airport_codes:

            logger.info(f"Processing airport {airport_code} ...")

            ## TODO: add aggregation/impute by terminal as well
            logger.info("Aggregating the historical and scheduled flight data to airport level ...")

            historical_df = hist_flight_df[hist_flight_df["airport_code"] == airport_code].copy()
            if historical_df.empty:
                logger.warning(f"No historical flight data for airport {airport_code}. Skipping.")
            else:
                historical_df = historical_df.groupby(
                    ["visit_slot", "airport_code"]
                )["departure_flight_count"].sum().reset_index()

                logger.info("Imputing historical flight to a continuous time-series ...")
                historical_df = impute_zero_flights(historical_df)

                logger.info("Computing upcoming relevant historical flight ...")
                upcoming_hist_df = compute_upcoming_air_traffic_from_flights(
                    historical_df, upcoming_hours_to_consider
                )

                logger.info(
                    "Computing hourly upcoming historical flight counts ..."
                )
                hourly_upcoming_hist_df = get_hourly_max(upcoming_hist_df, "upcoming_flight_count")

                ## TODO: fix boundary issue at the start of the extraction period where the upcoming flight count
                # may be underestimated due to lack of historical data in the lookback window
                ## HOTFIX: remove the first x hours
                hourly_upcoming_hist_df = hourly_upcoming_hist_df.iloc[upcoming_hours_to_consider:, :]

                logger.info(
                    "Computing weekday and hour upcoming historical flight counts summary ..."
                )
                hourly_upcoming_hist_summary_df = compute_weekday_and_hour_summary(
                    hourly_upcoming_hist_df, "upcoming_flight_count"
                )

                flight_count_summary_all_df = pd.concat(
                    [flight_count_summary_all_df, hourly_upcoming_hist_summary_df.assign(airport_code=airport_code)]
                )

            scheduled_df = scheduled_flight_df[scheduled_flight_df["airport_code"] == airport_code].copy()
            if scheduled_df.empty:
                logger.warning(f"No scheduled flight data for airport {airport_code}. Skipping.")
            else:
                scheduled_df = scheduled_df.groupby(["visit_slot", "airport_code"])[
                    [
                        "departure_flight_count",
                        "total_seats", "first_class_seats", "business_class_seats",
                        "premium_economy_seats", "economy_plus_seats", "economy_class_seats"
                    ]
                ].sum().reset_index()

                logger.info("Imputing scheduled flight to a continuous time-series ...")
                scheduled_df = impute_zero_flights(scheduled_df)

                upcoming_sched_df = compute_upcoming_air_traffic_from_flights(scheduled_df, upcoming_hours_to_consider)

                logger.info(
                    "Computing hourly upcoming scheduled seat counts ..."
                )
                hourly_upcoming_sched_df = get_hourly_max(upcoming_sched_df, "total_seats")

                ## TODO: fix boundary issue at the start of the extraction period where the upcoming flight count
                # may be underestimated due to lack of historical data in the lookback window
                ## HOTFIX: remove the first x hours
                hourly_upcoming_sched_df = hourly_upcoming_sched_df.iloc[upcoming_hours_to_consider:, :]

                logger.info(
                    "Computing weekday and hour upcoming scheduled seat counts summary ..."
                )
                hourly_upcoming_sched_summary_df = compute_weekday_and_hour_summary(
                    hourly_upcoming_sched_df, "total_seats"
                )

                seat_count_summary_all_df = pd.concat(
                    [seat_count_summary_all_df, hourly_upcoming_sched_summary_df.assign(airport_code=airport_code)]
                )

        if is_to_export:

            logger.info(
                "Export the hourly airport traffic (dep flight and seat counts) weekday x hour summary to local ..."
            )

            filename_config = config["filename_base"]
            export_dir = directory / path_config["export_directory"]

            min_date = scheduled_flight_df["visit_slot"].dt.date.min()
            max_date = scheduled_flight_df["visit_slot"].dt.date.max()

            # Creating Excel Writer Object to export the processed results
            excel_filepath = export_dir / f"{filename_config['excel']}_{min_date}_to_{max_date}.xlsx"

            with pd.ExcelWriter(excel_filepath, engine="xlsxwriter") as writer:
                flight_count_summary_all_df.to_excel(writer, sheet_name="dep_flight_summary", index=False)
                seat_count_summary_all_df.to_excel(writer, sheet_name="dep_seat_summary", index=False)

                pp_airport_info_to_export = outlet_info_df[
                    [
                        "airport_code", "terminal"
                    ]
                ].drop_duplicates()

                pp_airport_info_to_export = pp_airport_info_to_export[
                    pp_airport_info_to_export["airport_code"].isin(airport_codes)
                ]
                pp_airport_info_to_export.to_excel(writer, sheet_name="pp_airport_info", index=False)

                oag_sched_airport_info = scheduled_flight_df[["airport_code", "terminal"]].drop_duplicates()
                oag_sched_airport_info.to_excel(writer, sheet_name="oag_sched_airport_info", index=False)

    except Exception as e:
        logger.exception(f"An error {e} occurred during analysis.")

    end_time = time.time()

    logger.info(
        "Finished analysis."
    )
    logger.info(f"Time used = {end_time - start_time} seconds.")