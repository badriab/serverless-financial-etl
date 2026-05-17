"""
lambdas/loader/handler.py

AWS Lambda entry point for the loader step.

Flow:
  1. Receives the processed S3 key from the transformer Lambda's output
  2. Downloads the Parquet file from S3
  3. Converts it to a list of dicts
  4. Upserts all records into RDS PostgreSQL via db_client.upsert_records()
  5. Returns a summary dict

Environment variables:
  PROCESSED_BUCKET  — S3 bucket containing transformed Parquet files
  DB_SECRET_NAME    — Secrets Manager secret name for DB credentials
  AWS_REGION        — Set automatically by Lambda runtime
"""

import io
import json
import logging
import os
from datetime import datetime, timezone

import boto3
import pandas as pd

from db_client import get_connection, upsert_records

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Columns that may contain NaN (from rolling calculations on short history)
NULLABLE_FLOAT_COLS = ["sma_7", "sma_30", "pct_change_1d", "volume_zscore"]


def _get_s3_client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-south-1"))


def _read_parquet_from_s3(s3_client, bucket: str, key: str) -> pd.DataFrame:
    """Downloads a Parquet file from S3 and returns it as a DataFrame."""
    logger.info({"action": "read_parquet", "bucket": bucket, "key": key})
    response = s3_client.get_object(Bucket=bucket, Key=key)
    df = pd.read_parquet(io.BytesIO(response["Body"].read()))
    logger.info({"action": "read_parquet_ok", "rows": len(df), "key": key})
    return df


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """
    Converts a DataFrame to a list of dicts safe for psycopg2.
    - Converts date objects to strings (DATE type in PostgreSQL)
    - Converts numpy booleans to Python booleans
    - Converts numpy floats to Python floats (or None for NaN)
    """
    records = []
    for row in df.itertuples(index=False):
        record = {
            "symbol":       str(row.symbol),
            "date":         str(row.date),
            "open":         float(row.open),
            "high":         float(row.high),
            "low":          float(row.low),
            "close":        float(row.close),
            "volume":       float(row.volume),
            "sma_7":        _safe_float(row.sma_7),
            "sma_30":       _safe_float(row.sma_30),
            "pct_change_1d":_safe_float(row.pct_change_1d),
            "daily_range":  float(row.daily_range),
            "volume_zscore":_safe_float(row.volume_zscore),
            "anomaly_flag": bool(row.anomaly_flag),
            "processed_at": str(row.processed_at),
        }
        records.append(record)
    return records


def _safe_float(value) -> float | None:
    """Returns float or None for NaN/None values."""
    try:
        f = float(value)
        return None if f != f else f  # NaN != NaN
    except (TypeError, ValueError):
        return None


def handler(event: dict, context) -> dict:
    """
    Lambda entry point.

    Expected event payload (passed from transformer Lambda output):
    {
      "processed_key": "processed/year=2025/month=05/day=08/data.parquet"
    }

    Returns:
    {
      "status": "ok",
      "processed_key": "processed/...",
      "rows_upserted": 35,
      "symbols": ["AAPL", "MSFT", ...],
      "loaded_at": "2025-05-09T06:05:12Z",
      "elapsed_ms": 842
    }
    """
    start = datetime.now(timezone.utc)
    logger.info({"action": "handler_start", "event": event})

    # ------------------------------------------------------------------
    # 1. Resolve configuration
    # ------------------------------------------------------------------
    processed_bucket = os.environ.get("PROCESSED_BUCKET")
    db_secret_name = os.environ.get("DB_SECRET_NAME")

    if not processed_bucket:
        raise EnvironmentError("PROCESSED_BUCKET environment variable must be set.")
    if not db_secret_name:
        raise EnvironmentError("DB_SECRET_NAME environment variable must be set.")

    processed_key = event.get("processed_key")
    if not processed_key:
        raise ValueError("Event payload must contain 'processed_key'.")

    # ------------------------------------------------------------------
    # 2. Read Parquet from S3
    # ------------------------------------------------------------------
    s3 = _get_s3_client()
    df = _read_parquet_from_s3(s3, processed_bucket, processed_key)

    if df.empty:
        raise ValueError(f"Parquet file is empty: s3://{processed_bucket}/{processed_key}")

    records = _df_to_records(df)

    # ------------------------------------------------------------------
    # 3. Upsert into RDS PostgreSQL
    # ------------------------------------------------------------------
    with get_connection(db_secret_name) as conn:
        rows_upserted = upsert_records(conn, records)
        conn.commit()

    # ------------------------------------------------------------------
    # 4. Return summary
    # ------------------------------------------------------------------
    elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    result = {
        "status": "ok",
        "processed_key": processed_key,
        "rows_upserted": rows_upserted,
        "symbols": sorted(df["symbol"].unique().tolist()),
        "anomalies_loaded": int(df["anomaly_flag"].sum()),
        "loaded_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": elapsed_ms,
    }
    logger.info({"action": "handler_complete", **result})
    return result


# ---------------------------------------------------------------------------
# Local test harness
#   python handler.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    os.environ.setdefault("PROCESSED_BUCKET", "my-etl-processed-bucket")
    os.environ.setdefault("DB_SECRET_NAME", "etl/rds-credentials")
    os.environ.setdefault("AWS_REGION", "ap-south-1")

    test_event = {
        "processed_key": "processed/year=2025/month=05/day=08/data.parquet"
    }

    print("Running loader locally (requires real AWS credentials + S3 + RDS)...")
    try:
        result = handler(test_event, context=None)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)