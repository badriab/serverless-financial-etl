"""
lambdas/loader/db_client.py

PostgreSQL connection management and upsert logic for the loader Lambda.
Credentials are always fetched from AWS Secrets Manager — never hardcoded.

Secret JSON shape (stored in Secrets Manager):
{
  "host":     "my-rds-endpoint.rds.amazonaws.com",
  "port":     5432,
  "dbname":   "financial_etl",
  "username": "etl_user",
  "password": "supersecret"
}
"""

import json
import logging
import os
from contextlib import contextmanager
from typing import Iterator

import boto3
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


def _get_db_credentials(secret_name: str) -> dict:
    client = boto3.client(
        "secretsmanager",
        region_name=os.environ.get("AWS_REGION", "ap-south-1"),
    )
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


@contextmanager
def get_connection(secret_name: str) -> Iterator[psycopg2.extensions.connection]:
    """
    Context manager that opens a PostgreSQL connection using credentials
    from Secrets Manager, yields it, then closes it cleanly.
    """
    creds = _get_db_credentials(secret_name)
    conn = psycopg2.connect(
        host=creds["host"],
        port=int(creds.get("port", 5432)),
        dbname=creds["dbname"],
        user=creds["username"],
        password=creds["password"],
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    logger.info({"action": "db_connect_ok", "host": creds["host"], "dbname": creds["dbname"]})
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        logger.info({"action": "db_close"})


def upsert_records(conn: psycopg2.extensions.connection, records: list) -> int:
    """
    Upserts equity snapshot dicts into equity_snapshots table using execute_batch.
    Returns number of rows affected.
    """
    if not records:
        logger.warning({"action": "upsert_skip", "reason": "empty records list"})
        return 0

    sql = """
        INSERT INTO equity_snapshots (
            symbol, date, open, high, low, close, volume,
            sma_7, sma_30, pct_change_1d, daily_range,
            volume_zscore, anomaly_flag, processed_at
        ) VALUES (
            %(symbol)s, %(date)s, %(open)s, %(high)s, %(low)s,
            %(close)s, %(volume)s, %(sma_7)s, %(sma_30)s,
            %(pct_change_1d)s, %(daily_range)s, %(volume_zscore)s,
            %(anomaly_flag)s, %(processed_at)s
        )
        ON CONFLICT (symbol, date) DO UPDATE SET
            open          = EXCLUDED.open,
            high          = EXCLUDED.high,
            low           = EXCLUDED.low,
            close         = EXCLUDED.close,
            volume        = EXCLUDED.volume,
            sma_7         = EXCLUDED.sma_7,
            sma_30        = EXCLUDED.sma_30,
            pct_change_1d = EXCLUDED.pct_change_1d,
            daily_range   = EXCLUDED.daily_range,
            volume_zscore = EXCLUDED.volume_zscore,
            anomaly_flag  = EXCLUDED.anomaly_flag,
            processed_at  = EXCLUDED.processed_at
    """

    # Replace NaN with None — psycopg2 maps Python None to SQL NULL
    clean = [
        {k: (None if (isinstance(v, float) and v != v) else v) for k, v in r.items()}
        for r in records
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, clean)
        row_count = cur.rowcount

    logger.info({"action": "upsert_ok", "rows": row_count})
    return row_count