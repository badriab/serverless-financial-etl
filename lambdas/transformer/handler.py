"""
lambdas/transformer/handler.py

AWS Lambda entry point for the transformation step.

Flow:
  1. EventBridge or ingestor Lambda passes an S3 key via the event payload
  2. This handler reads the raw JSON file from S3
  3. Calls transform() from transformations.py
  4. Writes the result as Parquet to the processed S3 bucket
  5. Returns a summary dict for downstream use / CloudWatch logging

Environment variables (set via CDK / Lambda config — never hardcoded):
  RAW_BUCKET       — S3 bucket containing raw JSON files
  PROCESSED_BUCKET — S3 bucket to write transformed Parquet files
  AWS_REGION       — AWS region (automatically set by Lambda runtime)
"""

import io
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pandas as pd

from transformations import transform

# ---------------------------------------------------------------------------
# Logger — structured output for CloudWatch Logs Insights queries
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _get_s3_client():
    """Returns a boto3 S3 client. Separated for easy mocking in tests."""
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-south-1"))


def _read_raw_json_from_s3(s3_client, bucket: str, key: str) -> pd.DataFrame:
    """
    Downloads a raw JSON file from S3 and returns it as a DataFrame.

    Expected JSON format — a list of OHLCV records:
    [
      {"symbol": "AAPL", "date": "2025-05-08", "open": 182.5, ...},
      ...
    ]
    """
    logger.info({"action": "read_raw", "bucket": bucket, "key": key})
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    records = json.loads(body)

    if not isinstance(records, list):
        raise ValueError(
            f"Expected a JSON array at s3://{bucket}/{key}, got {type(records).__name__}"
        )
    if len(records) == 0:
        raise ValueError(f"Empty JSON array at s3://{bucket}/{key}")

    df = pd.DataFrame(records)
    logger.info({"action": "read_raw_ok", "rows": len(df), "key": key})
    return df


def _build_processed_key(raw_key: str) -> str:
    """
    Derives the processed S3 key from the raw key.

    Example:
      raw_key:       raw/year=2025/month=05/day=08/data.json
      processed_key: processed/year=2025/month=05/day=08/data.parquet
    """
    processed_key = raw_key.replace("raw/", "processed/", 1).replace(
        ".json", ".parquet"
    )
    return processed_key


def _write_parquet_to_s3(
    s3_client, df: pd.DataFrame, bucket: str, key: str
) -> None:
    """Serialises a DataFrame to Parquet in-memory and uploads to S3."""
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False, engine="pyarrow")
    buffer.seek(0)

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=buffer.getvalue(),
        ContentType="application/octet-stream",
    )
    logger.info(
        {
            "action": "write_processed_ok",
            "bucket": bucket,
            "key": key,
            "rows": len(df),
            "size_bytes": buffer.tell(),
        }
    )


def handler(event: dict, context) -> dict:
    """
    Lambda entry point.

    Expected event payload (passed by EventBridge rule or upstream Lambda):
    {
      "raw_key": "raw/year=2025/month=05/day=08/data.json"
    }

    Returns:
    {
      "status": "ok",
      "raw_key": "raw/...",
      "processed_key": "processed/...",
      "rows_processed": 35,
      "processed_at": "2025-05-08T06:04:31Z"
    }
    """
    start = datetime.now(timezone.utc)
    logger.info({"action": "handler_start", "event": event})

    # ------------------------------------------------------------------
    # 1. Resolve configuration from environment + event
    # ------------------------------------------------------------------
    raw_bucket = os.environ.get("RAW_BUCKET")
    processed_bucket = os.environ.get("PROCESSED_BUCKET")

    if not raw_bucket or not processed_bucket:
        raise EnvironmentError(
            "RAW_BUCKET and PROCESSED_BUCKET environment variables must be set."
        )

    raw_key = event.get("raw_key")
    if not raw_key:
        raise ValueError("Event payload must contain 'raw_key'.")

    processed_key = _build_processed_key(raw_key)

    # ------------------------------------------------------------------
    # 2. Read → Transform → Write
    # ------------------------------------------------------------------
    s3 = _get_s3_client()

    raw_df = _read_raw_json_from_s3(s3, raw_bucket, raw_key)
    transformed_df = transform(raw_df)
    _write_parquet_to_s3(s3, transformed_df, processed_bucket, processed_key)

    # ------------------------------------------------------------------
    # 3. Build and return summary
    # ------------------------------------------------------------------
    elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    result = {
        "status": "ok",
        "raw_key": raw_key,
        "processed_key": processed_key,
        "rows_processed": len(transformed_df),
        "symbols": sorted(transformed_df["symbol"].unique().tolist()),
        "anomalies_flagged": int(transformed_df["anomaly_flag"].sum()),
        "processed_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": elapsed_ms,
    }
    logger.info({"action": "handler_complete", **result})
    return result


# ---------------------------------------------------------------------------
# Local test harness — run directly without deploying to AWS:
#   python handler.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    # Minimal env setup for local run
    os.environ.setdefault("RAW_BUCKET", "my-etl-raw-bucket")
    os.environ.setdefault("PROCESSED_BUCKET", "my-etl-processed-bucket")
    os.environ.setdefault("AWS_REGION", "ap-south-1")

    # Synthetic local event
    test_event = {"raw_key": "raw/year=2025/month=05/day=08/data.json"}

    print("Running handler locally (requires AWS credentials + real S3 buckets)...")
    print("To run unit tests instead: pytest tests/unit/test_handler.py -v")

    try:
        result = handler(test_event, context=None)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)