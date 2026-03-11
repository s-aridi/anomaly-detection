#!/usr/bin/env python3
import json
import io
import boto3
import pandas as pd
import logging
import traceback
from datetime import datetime

from baseline import BaselineManager
from detector import AnomalyDetector

LOG_PATH = "/opt/anomaly-detection/app.log"

logger = logging.getLogger("anomaly_processor")
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = logging.FileHandler(LOG_PATH)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

s3 = boto3.client("s3")


NUMERIC_COLS = ["temperature", "humidity", "pressure", "wind_speed"]  # students configure this

def upload_log_to_s3(bucket: str, log_path: str = LOG_PATH, log_key: str = "logs/app.log"):
    try:
        with open(log_path, "rb") as f:
            s3.put_object(
                Bucket=bucket,
                Key=log_key,
                Body=f.read(),
                ContentType="text/plain"
            )
        logger.info(f"Uploaded log file to s3://{bucket}/{log_key}")
    except Exception as e:
        logger.exception(f"Failed to upload log file to S3: {e}")


def process_file(bucket: str, key: str):
    logger.info(f"Starting processing for s3://{bucket}/{key}")

    try:
        # 1. Download raw file
        logger.info(f"Downloading raw file: s3://{bucket}/{key}")
        response = s3.get_object(Bucket=bucket, Key=key)
        df = pd.read_csv(io.BytesIO(response["Body"].read()))
        logger.info(f"Loaded {len(df)} rows with columns: {list(df.columns)}")

        # 2. Load current baseline
        logger.info("Loading current baseline from S3")
        baseline_mgr = BaselineManager(bucket=bucket)
        baseline = baseline_mgr.load()
        logger.info("Baseline loaded successfully")

        # 3. Update baseline with values from this batch BEFORE scoring
        for col in NUMERIC_COLS:
            if col in df.columns:
                clean_values = df[col].dropna().tolist()
                logger.info(f"Column '{col}' has {len(clean_values)} non-null values for baseline update")

                if clean_values:
                    baseline = baseline_mgr.update(baseline, col, clean_values)
                    logger.info(
                        f"Updated baseline for '{col}': "
                        f"count={baseline.get(col, {}).get('count', 0)}, "
                        f"mean={baseline.get(col, {}).get('mean', 0):.4f}, "
                        f"std={baseline.get(col, {}).get('std', 0):.4f}"
                    )
            else:
                logger.warning(f"Expected numeric column '{col}' not found in input file")

        # 4. Run detection
        logger.info("Running anomaly detector")
        detector = AnomalyDetector(z_threshold=3.0, contamination=0.05)
        scored_df = detector.run(df, NUMERIC_COLS, baseline, method="both")
        logger.info("Anomaly detection completed successfully")

        # 5. Write scored file to processed/ prefix
        output_key = key.replace("raw/", "processed/")
        csv_buffer = io.StringIO()
        scored_df.to_csv(csv_buffer, index=False)

        logger.info(f"Uploading scored CSV to s3://{bucket}/{output_key}")
        s3.put_object(
            Bucket=bucket,
            Key=output_key,
            Body=csv_buffer.getvalue(),
            ContentType="text/csv"
        )

        # 6. Save updated baseline back to S3
        logger.info("Saving updated baseline to S3")
        baseline_mgr.save(baseline)
        logger.info(f"Baseline saved to s3://{bucket}/state/baseline.json")

        # 7. Sync local log file to S3 right after baseline save
        upload_log_to_s3(bucket)

        # 8. Build processing summary
        anomaly_count = int(scored_df["anomaly"].sum()) if "anomaly" in scored_df else 0
        summary = {
            "source_key": key,
            "output_key": output_key,
            "processed_at": datetime.utcnow().isoformat(),
            "total_rows": len(df),
            "anomaly_count": anomaly_count,
            "anomaly_rate": round(anomaly_count / len(df), 4) if len(df) > 0 else 0,
            "baseline_observation_counts": {
                col: baseline.get(col, {}).get("count", 0) for col in NUMERIC_COLS
            }
        }

        # 9. Write summary JSON alongside processed file
        summary_key = output_key.replace(".csv", "_summary.json")
        logger.info(f"Uploading summary JSON to s3://{bucket}/{summary_key}")
        s3.put_object(
            Bucket=bucket,
            Key=summary_key,
            Body=json.dumps(summary, indent=2),
            ContentType="application/json"
        )

        logger.info(
            f"Finished processing s3://{bucket}/{key} | "
            f"rows={len(df)} | anomalies={anomaly_count}"
        )
        return summary

    except Exception as e:
        logger.exception(f"Error while processing s3://{bucket}/{key}: {e}")
        logger.error(traceback.format_exc())
        return {
            "status": "error",
            "source_key": key,
            "error": str(e),
            "processed_at": datetime.utcnow().isoformat()
        }
