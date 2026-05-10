"""
lambdas/ingestor/handler.py

AWS Lambda entry point for the ingestion step.

Flow:
  1. EventBridge triggers this Lambda daily at 06:00 UTC
  2. Reads target symbols and API key from environment / Secrets Manager
  3. Fetches OHLCV data for each symbol from Alpha Vantage
  4. Writes a single combined raw JSON file to S3
  5. Returns the S3 key so the transformer Lambda can pick it up

Environment variables:
  RAW_BUCKET           — S3 bucket to write raw JSON files
  TARGET_SYMBOLS       — comma-separated ticker list e.g. "AAPL,MSFT,GOOGL"
  ALPHA_VANTAGE_SECRET — Secrets Manager secret name storing the API key
  AWS_REGION           — AWS region (set automatically by Lambda runtime)
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from api_client import fetch_daily_ohlcv

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _get_s3_client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-south-1"))


def _get_secrets_client():
    return boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "ap-south-1"))


def _resolve_api_key() -> str:
    """
    Resolves the Alpha Vantage API key.
    Priority:
      1. Secrets Manager (production)
      2. ALPHA_VANTAGE_API_KEY env var (local dev / testing)
    """
    secret_name = os.environ.get("ALPHA_VANTAGE_SECRET")
    if secret_name:
        client = _get_secrets_client()
        response = client.get_secret_value(SecretId=secret_name)
        secret = json.loads(response["SecretString"])
        return secret["api_key"]

    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if api_key:
        return api_key

    raise EnvironmentError(
        "No API key found. Set ALPHA_VANTAGE_SECRET (Secrets Manager) "
        "or ALPHA_VANTAGE_API_KEY (env var)."
    )


def _build_s3_key(run_date: datetime) -> str:
    """
    Builds a date-partitioned S3 key for the raw JSON file.

    Example: raw/year=2025/month=05/day=09/data.json
    """
    return (
        f"raw/year={run_date.year}/"
        f"month={run_date.month:02d}/"
        f"day={run_date.day:02d}/data.json"
    )


def _write_to_s3(s3_client, records: list, bucket: str, key: str) -> None:
    """Serialises records to JSON and uploads to S3."""
    body = json.dumps(records, default=str).encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    logger.info({
        "action": "write_raw_ok",
        "bucket": bucket,
        "key": key,
        "records": len(records),
        "size_bytes": len(body),
    })


def handler(event: dict, context) -> dict:
    """
    Lambda entry point.

    Accepts an optional event override for symbols and date (useful for backfills):
    {
      "symbols": ["AAPL", "TSLA"],   # optional — overrides TARGET_SYMBOLS env var
      "run_date": "2025-05-08"        # optional — overrides today's date
    }

    Returns:
    {
      "status": "ok",
      "raw_key": "raw/year=2025/month=05/day=09/data.json",
      "symbols_fetched": ["AAPL", "MSFT", ...],
      "symbols_failed": [],
      "total_records": 500,
      "ingested_at": "2025-05-09T06:00:31Z"
    }
    """
    start = datetime.now(timezone.utc)
    logger.info({"action": "handler_start", "event": event})

    # ------------------------------------------------------------------
    # 1. Resolve configuration
    # ------------------------------------------------------------------
    raw_bucket = os.environ.get("RAW_BUCKET")
    if not raw_bucket:
        raise EnvironmentError("RAW_BUCKET environment variable must be set.")

    # Symbols: event override > env var > error
    symbols_raw = event.get("symbols") or os.environ.get("TARGET_SYMBOLS")
    if not symbols_raw:
        raise EnvironmentError("TARGET_SYMBOLS env var or event.symbols must be set.")

    symbols = (
        symbols_raw if isinstance(symbols_raw, list)
        else [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    )

    # Run date: event override > today UTC
    if event.get("run_date"):
        run_date = datetime.strptime(event["run_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        run_date = start

    api_key = _resolve_api_key()
    s3_key = _build_s3_key(run_date)

    # ------------------------------------------------------------------
    # 2. Fetch data for each symbol — collect failures without aborting
    # ------------------------------------------------------------------
    all_records = []
    symbols_fetched = []
    symbols_failed = []

    for symbol in symbols:
        try:
            records = fetch_daily_ohlcv(symbol, api_key)
            all_records.extend(records)
            symbols_fetched.append(symbol)
        except Exception as exc:
            logger.error({"action": "symbol_fetch_failed", "symbol": symbol, "error": str(exc)})
            symbols_failed.append(symbol)

    if not all_records:
        raise RuntimeError(
            f"No data fetched for any symbol. Failed: {symbols_failed}"
        )

    # ------------------------------------------------------------------
    # 3. Write combined JSON to S3
    # ------------------------------------------------------------------
    s3 = _get_s3_client()
    _write_to_s3(s3, all_records, raw_bucket, s3_key)

    # ------------------------------------------------------------------
    # 4. Return summary
    # ------------------------------------------------------------------
    elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    result = {
        "status": "ok",
        "raw_key": s3_key,
        "symbols_fetched": symbols_fetched,
        "symbols_failed": symbols_failed,
        "total_records": len(all_records),
        "ingested_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
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

    os.environ.setdefault("RAW_BUCKET", "my-etl-raw-bucket")
    os.environ.setdefault("TARGET_SYMBOLS", "AAPL,MSFT")
    os.environ.setdefault("AWS_REGION", "ap-south-1")
    # Set ALPHA_VANTAGE_API_KEY to your real key for local runs

    print("Running ingestor locally (requires real API key + AWS credentials)...")
    try:
        result = handler({}, context=None)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)