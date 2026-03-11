# app.py
import io
import json
import os
import boto3
import pandas as pd
import requests
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request
from baseline import BaselineManager
from processor import process_file

# Logging Setup
LOG_PATH = "/opt/anomaly-detection/app.log"

logger = logging.getLogger("anomaly_app")
logger.setLevel(logging.INFO)

# Prevent duplicate handlers if the module gets reloaded
if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    # Log to file
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # Log to console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


app = FastAPI(title="Anomaly Detection Pipeline")

s3 = boto3.client("s3")
BUCKET_NAME = os.environ["BUCKET_NAME"]

logger.info("Starting anomaly detection API")
logger.info(f"Configured bucket: {BUCKET_NAME}")


# ── SNS subscription confirmation + message handler ──────────────────────────

@app.post("/notify")
async def notify(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        logger.info("Received request on /notify")
        logger.info(f"SNS message type: {body.get('Type', 'UNKNOWN')}")

        msg_type = body.get("Type")

        if msg_type == "SubscriptionConfirmation":
            confirm_url = body.get("SubscribeURL")
            if not confirm_url:
                logger.error("SubscriptionConfirmation received without SubscribeURL")
                return {"status": "error", "message": "Missing SubscribeURL"}

            logger.info(f"Confirming SNS subscription using URL: {confirm_url}")
            response = requests.get(confirm_url, timeout=10)
            logger.info(f"SNS subscription confirmation response code: {response.status_code}")
            return {"status": "subscription confirmed"}

        elif msg_type == "Notification":
            message_str = body.get("Message")
            if not message_str:
                logger.error("Notification received without Message field")
                return {"status": "error", "message": "Missing Message"}

            logger.info("Parsing SNS notification message")
            message = json.loads(message_str)

            records = message.get("Records", [])
            logger.info(f"Notification contains {len(records)} record(s)")

            for record in records:
                key = record["s3"]["object"]["key"]
                logger.info(f"Received S3 object key: {key}")

                if key.startswith("raw/") and key.endswith(".csv"):
                    logger.info(f"Queueing background processing for file: {key}")
                    background_tasks.add_task(process_file, BUCKET_NAME, key)
                else:
                    logger.info(f"Skipping non-matching object key: {key}")

            return {"status": "notification processed"}

        else:
            logger.warning(f"Unhandled SNS message type: {msg_type}")
            return {"status": "ignored", "message": f"Unknown SNS type: {msg_type}"}

    except Exception as e:
        logger.exception(f"Error in /notify: {e}")
        return {"status": "error", "message": str(e)}



# ── Query endpoints ───────────────────────────────────────────────────────────

@app.get("/anomalies/recent")
def get_recent_anomalies(limit: int = 50):
    """Return rows flagged as anomalies across the 10 most recent processed files."""
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

    keys = sorted(
        [
            obj["Key"]
            for page in pages
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(".csv")
        ],
        reverse=True,
    )[:10]

    all_anomalies = []
    for key in keys:
        response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        df = pd.read_csv(io.BytesIO(response["Body"].read()))
        if "anomaly" in df.columns:
            flagged = df[df["anomaly"] == True].copy()
            flagged["source_file"] = key
            all_anomalies.append(flagged)

    if not all_anomalies:
        return {"count": 0, "anomalies": []}

    combined = pd.concat(all_anomalies).head(limit)
    return {"count": len(combined), "anomalies": combined.to_dict(orient="records")}


@app.get("/anomalies/summary")
def get_anomaly_summary():
    """Aggregate anomaly rates across all processed files using their summary JSONs."""
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

    summaries = []
    for page in pages:
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("_summary.json"):
                response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
                summaries.append(json.loads(response["Body"].read()))

    if not summaries:
        return {"message": "No processed files yet."}

    total_rows = sum(s["total_rows"] for s in summaries)
    total_anomalies = sum(s["anomaly_count"] for s in summaries)

    return {
        "files_processed": len(summaries),
        "total_rows_scored": total_rows,
        "total_anomalies": total_anomalies,
        "overall_anomaly_rate": round(total_anomalies / total_rows, 4) if total_rows > 0 else 0,
        "most_recent": sorted(summaries, key=lambda x: x["processed_at"], reverse=True)[:5],
    }


@app.get("/baseline/current")
def get_current_baseline():
    """Show the current per-channel statistics the detector is working from."""
    baseline_mgr = BaselineManager(bucket=BUCKET_NAME)
    baseline = baseline_mgr.load()

    channels = {}
    for channel, stats in baseline.items():
        if channel == "last_updated":
            continue
        channels[channel] = {
            "observations": stats["count"],
            "mean": round(stats["mean"], 4),
            "std": round(stats.get("std", 0.0), 4),
            "baseline_mature": stats["count"] >= 30,
        }

    return {
        "last_updated": baseline.get("last_updated"),
        "channels": channels,
    }


@app.get("/health")
def health():
    logger.info("Health check endpoint called")
    return {"status": "ok", "bucket": BUCKET_NAME}
