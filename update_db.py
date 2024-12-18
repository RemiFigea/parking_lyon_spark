"""
Script: Parking Data Streaming to PostgreSQL

Description:
------------
This script continuously collects real-time parking availability data 
from a public API for parking facilities located in Lyon. The collected data 
is processed and stored in an existing PostgreSQL database. Key features include:

1. Fetching JSON data from the API every 60 seconds.
2. Processing the data using Apache Spark to detect changes 
   in parking space availability.
3. Storing the processed data in a PostgreSQL table named `parking_data`.

Note: The stream is also written to the console to check easily the output.

The PostgreSQL table should have the following schema:
- `parking_id` (STRING): Unique identifier for the parking facility.
- `nb_of_available_parking_spaces` (INT): Number of available parking spaces.
- `ferme` (BOOLEAN): Indicates if the parking facility is closed.
- `date` (TIMESTAMP): Timestamp of the data.

Requirements:
-------------
1. An existing PostgreSQL database with a `parking_data` table.
2. A stable network connection to fetch real-time API data.
3. Apache Spark for processing and streaming the data.
"""


import json
import os
import pandas as pd
from pyspark.sql import SparkSession
from pyspark.sql.types import StructType, StructField, IntegerType, BooleanType, StringType, TimestampType
from pyspark.sql.streaming.state import GroupStateTimeout
import requests
import shutil
import threading
import time

# API URL and PostgreSQL database configuration
API_URL = "https://download.data.grandlyon.com/files/rdata/lpa_mobilite.donnees/parking_temps_reel.json"
JAR_FILES_PATH = "./libs/postgresql-42.7.4.jar"
JDBC_URL = "jdbc:postgresql://172.31.4.218:5432/parking_lyon_db"
OUTPUT_PATH = "./parking_data"
CHECKPOINT_DIR = "/tmp/checkpoints/query_psql"
PGPASSWORD = os.environ.get('PGPASSWORD')

if not PGPASSWORD:
    raise ValueError("PGPASSWORD environment variable not set. To set it, use the following command:\n"
                      "export PGPASSWORD='your_password_here'")

# Empty the checkpoints dir to avoid raising error in case of previous stopping of the script
if os.path.exists(CHECKPOINT_DIR):
    shutil.rmtree(CHECKPOINT_DIR)

os.makedirs('./libs', exist_ok=True)

# Create a directory to store logs
log_dir = "./logs"
os.makedirs(log_dir, exist_ok=True)

# Specify the path to logs (for Spark and it's related libraries) configuration file
log4j_properties_path = "log4j.properties"
print("path to log4j.properties found:", os.path.exists(log4j_properties_path))

# Spark session configuration
spark = SparkSession.builder \
    .appName("Parking Availability Streaming") \
    .config("spark.jars", JAR_FILES_PATH) \
    .config("spark.driver.extraJavaOptions", f"-Dlog4j.configuration=file:{log4j_properties_path}") \
    .getOrCreate()

# Add thoses lines to sparksession to store eventlog performance of spark
# .config("spark.eventLog.enabled", "true") \
# .config("spark.eventLog.dir", log_dir) \

# Set log level for logs to display in the console
spark.sparkContext.setLogLevel("ERROR")

# PostgreSQL connection properties
postgres_properties = {
    "user": "postgres",  # PostgreSQL user
    "password": PGPASSWORD,  # PostgreSQL password
    "driver": "org.postgresql.Driver"
}

def fetch_data_and_save(api_url, output_path):
    """
    Function that fetches data from the API and saves it as JSON files every 60 seconds.
    
    Parameters:
    -----------
    - api_url (str): The URL of the API.
    - output_path (str): The directory to store the temporary JSON files.
    """
    # Create a directory to store temporary JSON files
    if os.path.exists(output_path):
        shutil.rmtree(output_path)
    os.makedirs(output_path, exist_ok=True)

    while True:
        try:
            # Generate the filename based on the current timestamp
            timestamp = time.time()
            json_filename = f"data_{timestamp}.json"
            json_filepath = os.path.join(output_path, json_filename)

            response = requests.get(api_url)
            localtime = time.localtime()
            hour = f"{localtime.tm_hour}h:{localtime.tm_min}min:{localtime.tm_sec}s"
            print(f"Time: {hour} - status_code request of API: {response.status_code}.")
            if response.status_code == 200:
                print(f"Time: {hour} - Getting response.json ")
                data = response.json()
                print(f"Time: {hour} - response.json ok ")
                # Save the JSON file
                with open(json_filepath, "w") as f:
                    print(f"Time: {hour} - json_filepath opened ")
                    json.dump(data, f)
                    print(f"Time: {hour} - json_filepath written ")
            else:
                print(f"HTTP Error: {response.status_code}")
        except Exception as e:
            print(f"Error fetching data: {str(e)}")
        time.sleep(120)

        # Remove the temporary JSON file after processing
        if os.path.exists(json_filepath):
            os.remove(json_filepath)

# Start the data fetching in a separate thread to simulate continuous streaming
fetch_thread = threading.Thread(target=fetch_data_and_save, args=(API_URL, OUTPUT_PATH))
fetch_thread.daemon = True  # Allow this thread to close when the main program stops
fetch_thread.start()

# Define the schema for the incoming JSON data
input_schema = StructType([
    StructField("mv:currentValue", IntegerType(), True),
    StructField("ferme", BooleanType(), True),
    StructField("Parking_schema:identifier", StringType(), True),
    StructField("Parking_schema:name", StringType(), True),
    StructField("dct:date", TimestampType(), True)
])

def process_parking_data(key, pdfs, state):
    """
    Function to process parking data and detect changes in parking availability.
    
    Parameters:
    -----------
    - key: Parking identifier.
    - pdfs: DataFrame containing the parking data.
    - state: Previous state of the parking data to compare current values.

    Returns:
    --------
    - pd.DataFrame: A DataFrame with the processed results.
    """
    (parking_id,) = key
    pdf = next(pdfs)
    current_value = pdf["current_value"][0]
    date = pdf["date"][0]
    ferme = pdf["ferme"][0]
    value_has_changed = True
    old_value = 0

    # Check if there is an existing state
    if state.exists:
        (old_value,) = state.get

    state.update((current_value,))

    # Detect if the number of available parking spaces has changed
    if current_value == old_value:
        value_has_changed = False

    # Return the results as a DataFrame
    yield pd.DataFrame({
        "parking_id": [parking_id],
        "nb_of_available_parking_spaces": [current_value],
        "ferme": [ferme],
        "date": [date],
        "value_has_change": [value_has_changed]
    })

def write_to_postgresql(batch_df, epoch_id):
    """
    Function to write the processed data to PostgreSQL database.
    
    Parameters:
    -----------
    - df: DataFrame to be written to the database.
    - epoch_id: Batch ID.
    """
    try:
        print("Start writing in PostgreSQL database.")
        batch_df.write.jdbc(JDBC_URL, "parking_table", mode="append", properties=postgres_properties)
        print("Data written in PostgreSQL database.")
    except Exception as e:
        print(f"Error inserting into PostgreSQL: {str(e)}")

# Define the schema for the output data to be written to the database
output_schema = "parking_id STRING, nb_of_available_parking_spaces INT, ferme BOOLEAN, date TIMESTAMP, value_has_change BOOLEAN"
state_schema = "nb_of_available_parking_spaces INT"

# Read the streaming data from the output directory
streaming_df = spark.readStream \
    .schema(input_schema) \
    .json(OUTPUT_PATH)

# Rename the columns to match the PostgreSQL database schema
streaming_df = streaming_df \
    .withColumnRenamed("mv:currentValue", "current_value") \
    .withColumnRenamed("Parking_schema:identifier", "parking_id") \
    .withColumnRenamed("dct:date", "date") \
    .drop("Parking_schema:name")  # Parking name is not needed as the identifier is sufficient

# Apply the function to process the parking data with state
streaming_df = streaming_df.groupBy("parking_id") \
    .applyInPandasWithState(
        process_parking_data,
        output_schema,
        state_schema,
        "append",
        timeoutConf=GroupStateTimeout.NoTimeout
    ) \
    .filter("value_has_change == True") \
    .drop("value_has_change")

# Start the stream to output data to the console for visualization
# query_console = streaming_df.writeStream \
#    .outputMode("append") \
#    .format("console") \
#    .start()

# Start the stream to write the results to PostgreSQL database
query_postgresql = streaming_df.writeStream \
    .outputMode("append") \
    .foreachBatch(write_to_postgresql) \
    .start()
    # .option("checkpointLocation", "/tmp/checkpoints/query_psql") \
    # .start()

# Wait for the termination of both streams
query_postgresql.awaitTermination()
# query_console.awaitTermination()
