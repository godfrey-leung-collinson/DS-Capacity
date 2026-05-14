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


# only for local run when storing the extracted data to local
directory = Path(__file__).parent.parent.parent.parent

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

warnings.filterwarnings("ignore")



def update_sql_for_extracting_15min_visits(
    sql_filepath: str, start: str, end: str,
) -> pd.core.frame.DataFrame:
    """
    Updating the SQL template for extracting visits
     from Snowflake PPass tracking visit table

    Parameters
    ----------
    sql_filepath
        path to the SQL template
    start
        start datetime of the period of interest
    end
        end datetime of the period of interest

    Returns
    -------
        15-min interval visit counts (non-null/zero only) per outlet
        in the given period of interest

    """

    with open(sql_filepath) as f:
        sql_query = f.read()

    sql_query = sql_query.replace("{start_datetime}", start)
    sql_query = sql_query.replace("{end_datetime}", end)

    return sql_query


def fetch_15min_visit(
    data_dir: Path, code_dir: Path
) -> Path:
    """
    Extracting 15-min interval visit counts per outlet from Snowflake
    (consolidated visit) and exporting the results to a local directory

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
            "Starting the ETL job of extracting 15-min visits per outlets ..."
        )

        # Load project tags
        with open(directory / "config/common_config.yaml", "r") as f:
            common_config = yaml.safe_load(f)

        project_tags = common_config["project_tag"]

        # Load parent main config
        with open(code_dir / "config/get_outlet_visit.yaml", "r") as f:
            config = yaml.safe_load(f)

        path_config = config["paths"]
        sql_template = directory / path_config["sql_template"]["get_15min_visit"]

        start_date = config["start_date"]
        start_date_str = start_date
        end_date = config["end_date"]

        logger.info(
            "Connecting to Snowflake and executing the queries for counting 15min visit per outlets ..."
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

        intermediate_end_date = dt.date.fromisoformat(start_date)
        days_to_run = (dt.date.fromisoformat(end_date) - dt.date.fromisoformat(start_date)).days

        full_visit_df = pd.DataFrame()
        for _ in range(days_to_run):
            intermediate_end_date = intermediate_end_date + dt.timedelta(days=1)
            if intermediate_end_date > dt.date.fromisoformat(end_date):
                break

            intermediate_end_date_str = intermediate_end_date.strftime("%Y-%m-%d")
            temp_end_date = intermediate_end_date_str

            logger.info(
                "Extracting 15-min visits per outlets from {} to {} ...".format(start_date, temp_end_date)
            )

            final_sql_query = update_sql_for_extracting_15min_visits(
                str(sql_template), start_date, temp_end_date
            )

            cur.execute(final_sql_query)
            temp_visit_df = cur.fetch_pandas_all()

            full_visit_df = pd.concat([full_visit_df, temp_visit_df])

            start_date = intermediate_end_date_str

        full_visit_df.sort_values(by=["OUTLET_CODE", "VISIT_SLOT"], inplace=True)
        full_visit_df = full_visit_df.reset_index(drop=True)

        print(full_visit_df.head())
        print(full_visit_df.info())
        print(full_visit_df.nunique())

        input("Check the extracted visit data above. Press Enter to continue ...")

        logger.info("Exporting extracted visits ...")

        filename = f"{filename_base}_by_outlet_{start_date_str}_to_{end_date}.csv"
        output_file_path = str(output_parent_dir / f"{filename}")

        # export the results to local
        full_visit_df.to_csv(
            output_file_path,
            index=False,
        )

        cur.close()

        end_time = time.time()

        logger.info(
            "Finished extracting 15-min interval non-null/zero visits per outlet."
        )
        logger.info(f"Time used = {end_time - start_time} seconds.")

        return output_file_path

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e


if __name__ == "__main__":

    visit_output_filepath = (
        fetch_15min_visit(
            directory / "data", directory / "analysis"
        )
    )
