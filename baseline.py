#!/usr/bin/env python3
import json
import math
import boto3
import logging
import traceback
from datetime import datetime
from typing import Optional
from botocore.exceptions import ClientError


LOG_PATH = "/opt/anomaly-detection/app.log"

logger = logging.getLogger("anomaly_baseline")
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


class BaselineManager:
    """
    Maintains a per-channel running baseline using Welford's online algorithm,
    which computes mean and variance incrementally without storing all past data.
    """

    def __init__(self, bucket: str, baseline_key: str = "state/baseline.json"):
        self.bucket = bucket
        self.baseline_key = baseline_key

    def load(self) -> dict:
        try:
            logger.info(f"Loading baseline from s3://{self.bucket}/{self.baseline_key}")
            response = s3.get_object(Bucket=self.bucket, Key=self.baseline_key)
            baseline = json.loads(response["Body"].read())
            logger.info("Baseline loaded successfully")
            return baseline

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")

            if error_code in ("NoSuchKey", "404"):
                logger.info(
                    f"No existing baseline found at s3://{self.bucket}/{self.baseline_key}; "
                    "starting with empty baseline"
                )
                return {}

            logger.exception(f"Unexpected S3 error while loading baseline: {e}")
            raise

        except Exception as e:
            logger.exception(f"Unexpected error while loading baseline: {e}")
            logger.error(traceback.format_exc())
            raise



    def save(self, baseline: dict):
        try:
            baseline["last_updated"] = datetime.utcnow().isoformat()

            logger.info(f"Saving baseline to s3://{self.bucket}/{self.baseline_key}")
            s3.put_object(
                Bucket=self.bucket,
                Key=self.baseline_key,
                Body=json.dumps(baseline, indent=2),
                ContentType="application/json"
            )
            logger.info("Baseline saved successfully")

        except ClientError as e:
            logger.exception(f"S3 error while saving baseline: {e}")
            raise

        except Exception as e:
            logger.exception(f"Unexpected error while saving baseline: {e}")
            logger.error(traceback.format_exc())
            raise



    def update(self, baseline: dict, channel: str, new_values: list[float]) -> dict:
        """
        Welford's online algorithm for numerically stable mean and variance.
        Each channel tracks: count, mean, M2 (sum of squared deviations).
        Variance = M2 / count, std = sqrt(variance).
        """
        try:
            if channel not in baseline:
                logger.info(f"Initializing baseline state for channel '{channel}'")
                baseline[channel] = {"count": 0, "mean": 0.0, "M2": 0.0}

            state = baseline[channel]
            original_count = state["count"]

            logger.info(
                f"Updating baseline for channel '{channel}' with {len(new_values)} new values"
            )

            for value in new_values:
                state["count"] += 1
                delta = value - state["mean"]
                state["mean"] += delta / state["count"]
                delta2 = value - state["mean"]
                state["M2"] += delta * delta2

            if state["count"] >= 2:
                variance = state["M2"] / state["count"]
                state["std"] = math.sqrt(variance)
            else:
                state["std"] = 0.0

            baseline[channel] = state

            logger.info(
                f"Updated channel '{channel}': "
                f"count {original_count} -> {state['count']}, "
                f"mean={state['mean']:.4f}, std={state['std']:.4f}"
            )

            return baseline

        except Exception as e:
            logger.exception(f"Error updating baseline for channel '{channel}': {e}")
            logger.error(traceback.format_exc())
            raise


    def get_stats(self, baseline: dict, channel: str) -> Optional[dict]:
        stats = baseline.get(channel)
        if stats is None:
            logger.warning(f"No baseline stats found for channel '{channel}'")
        return stats

