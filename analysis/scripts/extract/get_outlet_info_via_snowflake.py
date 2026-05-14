import datetime as dt
import logging
import os
from pathlib import Path
import time
import warnings

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


def fetch_outlet_info(data_dir: Path, code_dir: Path) -> Path:
    """
    Extracting outlet information like outlet type, total seating capacity,
    opening hours, etc from Snowflake and exporting the results to a local directory

    Parameters
    ----------
    data_dir
        local (EC2) directory path to export the extracted data to
    code_dir
        local directory where the config files are stored

    """

    try:
        start_time = time.time()

        logger.info("Starting the ETL job of extracting outlet information ...")

        # Load project tags
        with open(directory / "config/common_config.yaml", "r") as f:
            common_config = yaml.safe_load(f)

        project_tags = common_config["project_tag"]

        # Load parent main config
        with open(code_dir / "config/get_outlet_info.yaml", "r") as f:
            config = yaml.safe_load(f)

        path_config = config["paths"]
        sql_template = directory / path_config["sql_template"]["get_outlet_info"]

        logger.info(
            "Connecting to Snowflake and executing the queries for getting the outlet information ..."
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

        with open(sql_template) as f:
            sql_query = f.read()

        cur.execute(sql_query)
        outlet_info_df = cur.fetch_pandas_all()

        print(outlet_info_df.head())
        print(outlet_info_df.info())
        print(outlet_info_df.nunique())

        input("Check the extracted outlet info data above. Press Enter to continue ...")

        logger.info("Exporting extracted outlet info ...")

        today = dt.date.today()

        filename = f"{filename_base}_{today}.csv"
        output_file_path = str(output_parent_dir / f"{filename}")

        # export the results to local
        outlet_info_df.to_csv(
            output_file_path,
            index=False,
        )

        cur.close()

        end_time = time.time()

        logger.info("Finished extracting outlet information from Snowflake.")
        logger.info(f"Time used = {end_time - start_time} seconds.")

        return output_file_path

    except Exception as e:
        logger.error(f"Error: {e}")
        raise e


if __name__ == "__main__":

    outlet_info_filepath = fetch_outlet_info(directory / "data", directory / "analysis")
