import datetime as dt
import logging
import os
from pathlib import Path
import time
import warnings

import pandas as pd

import snowflake.connector  # For local run only
import yaml

# (TODO) NOTE: need to set python path environment in order to import from parent directory
# from helpers import SnowflakeManager


# only for local run when storing the extracted data to local
directory = Path(__file__).parent.parent.parent.parent

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")


def update_sql_for_extracting_15min_schedule_flight(
    sql_filepath: str, start: str, end: str, service_type: str = "J"
) -> pd.core.frame.DataFrame:
    """
    Updating the SQL template for extracting 15-min interval scheduled departure
    flight and seat counts (non-zero only) per airport and terminal in the
    given period of interest

    Parameters
    ----------
    sql_filepath
        path to the SQL template
    start
        start datetime of the period of interest
    end
        end datetime of the period of interest
    service_type
        flight service type to consider.
        Default = "J", i.e. normal commercial passenger flight services

    Returns
    -------
        15-min interval scheduled flight and seat counts (non-null/zero only) per airport
        (and terminal) in the given period of interest

    """

    with open(sql_filepath) as f:
        sql_query = f.read()

    sql_query = sql_query.replace("{start_datetime}", start)
    sql_query = sql_query.replace("{end_datetime}", end)
    sql_query = sql_query.replace("{flight_service_type}", service_type)

    return sql_query


def fetch_15min_scheduled_flights(data_dir: Path, code_dir: Path) -> Path:
    """
    Extract

    Parameters
    ----------
    data_dir
        local (EC2) directory path to export the extracted data to
    code_dir
        local directory where the config files are stored

    """

    try:
        start_time = time.time()

        logger.info(
            "Starting the ETL job of extracting 15-min scheduled departure flights and seat counts per airport and terminal ..."
        )

        # Load project tags
        with open(directory / "config/common_config.yaml", "r") as f:
            common_config = yaml.safe_load(f)

        project_tags = common_config["project_tag"]

        # Load parent main config
        with open(code_dir / "config/get_schedule_seat_count.yaml", "r") as f:
            config = yaml.safe_load(f)

        path_config = config["paths"]
        sql_template = directory / path_config["sql_template"]["get_15min_flights"]

        start_date_str = config["start_date"]
        start_date = dt.date.fromisoformat(start_date_str)

        end_date_str = config["end_date"]
        end_date = dt.date.fromisoformat(end_date_str)

        service_type = config["flight_service"]

        logger.info(
            "Connecting to Snowflake and executing the queries for counting 15min scheduled departure flights and seat counts per airport and terminal ..."
        )

        # # For local run only. Comment this out and use the following connector if running on ML platform
        snow_conn = snowflake.connector.connect(
            user=os.environ["USER"],
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            authenticator="externalbrowser",
            warehouse=os.environ["WAREHOUSE"],
            database=os.environ["DATABASE"],
            session_parameters=project_tags,
        )
        cur = snow_conn.cursor()

        # For ML platform run
        # snowflake_credentials = json.loads(os.environ.get("SNOWFLAKE_CREDENTIALS"))
        # environment = os.environ.get("ENVIRONMENT")
        # project_name = os.environ.get("PROJECT_NAME")
        # stage = os.environ.get("STAGE")
        # snowflake_manager = SnowflakeManager(
        #     project_name=project_name,
        #     stage=stage,
        #     secret_name=snowflake_credentials[environment]["secret_name"],
        #     account=snowflake_credentials[environment]["account_name"],
        #     username_key=snowflake_credentials[environment]["secret_user_key"],
        #     # password_key=snowflake_credentials[environment]['secret_password_key'],
        #     privatekey_key=snowflake_credentials[environment]["secret_privatekey_key"],
        #     passphrase_key=snowflake_credentials[environment]["secret_passphrase_key"],
        #     warehouse_key=snowflake_credentials[environment]["secret_warehouse_key"],
        #     environment=environment,
        #     lambda_function_name=snowflake_credentials[environment].get(
        #         "snowflake_lambda_name", None
        #     ),
        #     aws_access_key_id=snowflake_credentials[environment].get(
        #         "AWS_ACCESS_KEY_ID", None
        #     ),
        #     aws_secret_access_key=snowflake_credentials[environment].get(
        #         "AWS_SECRET_ACCESS_KEY", None
        #     ),
        #     aws_session_token=snowflake_credentials[environment].get(
        #         "AWS_SESSION_TOKEN", None
        #     ),
        #     prod_lambda=snowflake_credentials[environment].get("prod_lambda", "False"),
        # )
        # cur = snowflake_manager.get_custom_cursor()

        filename_base = config["filename_base"]
        export_dir = path_config["export_directory"]
        output_parent_dir = data_dir / f"{export_dir}"

        intermediate_end_date = start_date
        months_to_run = (end_date.year - start_date.year) * 12 + (
            end_date.month - start_date.month
        )

        full_scheduled_flight_df = pd.DataFrame()
        for _ in range(months_to_run):
            from dateutil.relativedelta import relativedelta

            intermediate_end_date = intermediate_end_date + relativedelta(months=1)

            intermediate_end_date_str = intermediate_end_date.strftime("%Y-%m-%d")
            temp_end_date = intermediate_end_date_str

            logger.info(
                "Extracting 15-min scheduled departure flight and seat counts per airport and terminal from {} to {} ...".format(
                    start_date, temp_end_date
                )
            )

            final_sql_query = update_sql_for_extracting_15min_schedule_flight(
                str(sql_template), start_date_str, temp_end_date, service_type
            )

            cur.execute(final_sql_query)
            temp_flight_df = cur.fetch_pandas_all()

            full_scheduled_flight_df = pd.concat(
                [full_scheduled_flight_df, temp_flight_df]
            )

            start_date_str = intermediate_end_date_str

        full_scheduled_flight_df.sort_values(
            by=["AIRPORT_CODE", "TERMINAL", "VISIT_SLOT"], inplace=True
        )
        full_scheduled_flight_df.reset_index(drop=True, inplace=True)

        print(full_scheduled_flight_df.head())
        print(full_scheduled_flight_df.info())
        print(full_scheduled_flight_df.nunique())

        input("Check the extracted flight data above. Press Enter to continue ...")

        logger.info("Exporting extracted historical flight counts to local ...")

        filename = f"{filename_base}_by_airport_and_terminal_{start_date_str}_to_{intermediate_end_date_str}.csv"
        output_file_path = str(output_parent_dir / f"{filename}")

        # export the results to local
        full_scheduled_flight_df.to_csv(
            # the file is saved in the EC2 instance of the SageMaker used for the processing
            output_file_path,
            index=False,
        )

        cur.close()

        end_time = time.time()

        logger.info(
            "Finished extracting 15-min interval non-zero/null scheduled departure flight and seat counts per airport and terminal."
        )
        logger.info(f"Time used = {end_time - start_time} seconds.")

        return output_file_path

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e


if __name__ == "__main__":

    scheduled_flights_output_filepath = fetch_15min_scheduled_flights(
        directory / "data", directory / "analysis"
    )
