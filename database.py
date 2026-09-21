from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Vercel functions have only ephemeral writable storage. This keeps SQLite
# working without ever attempting to write into the read-only deployment.
IS_SERVERLESS = bool(os.getenv('VERCEL') or os.getenv('VERCEL_ENV') or os.getenv('VERCEL_URL'))
DB_PATH = Path('/tmp/finanx.db') if IS_SERVERLESS else Path(os.getenv('FINANX_DB', 'finanx.db'))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database(app=None) -> None:
    conn = connect()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS mutual_fund_schemes (
        scheme_code TEXT PRIMARY KEY,
        scheme_name TEXT NOT NULL,
        isin TEXT,
        latest_nav REAL,
        latest_date TEXT,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mutual_fund_metrics (
        scheme_code TEXT PRIMARY KEY,
        scheme_name TEXT NOT NULL,
        return_1y REAL,
        return_3y REAL,
        return_5y REAL,
        latest_nav REAL,
        latest_date TEXT,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS daily_prices (
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        close REAL,
        volume REAL,
        PRIMARY KEY(symbol, trade_date)
    );
    ''')
    conn.commit()
    conn.close()


def save_daily_history(symbol: str, rows: list[tuple[str, float | None, float | None]]) -> None:
    conn = connect()
    conn.executemany(
        'INSERT INTO daily_prices(symbol,trade_date,close,volume) VALUES(?,?,?,?) '
        'ON CONFLICT(symbol,trade_date) DO UPDATE SET close=excluded.close,volume=excluded.volume',
        rows,
    )
    conn.commit()
    conn.close()


def save_mf_scheme(row: dict) -> None:
    conn = connect()
    conn.execute(
        'INSERT INTO mutual_fund_schemes(scheme_code,scheme_name,isin,latest_nav,latest_date,source,updated_at) '
        'VALUES(?,?,?,?,?,?,?) ON CONFLICT(scheme_code) DO UPDATE SET '
        'scheme_name=excluded.scheme_name,latest_nav=excluded.latest_nav,latest_date=excluded.latest_date,'
        'source=excluded.source,updated_at=excluded.updated_at',
        (
            row['scheme_code'], row['scheme_name'], row.get('isin'),
            row.get('latest_nav'), row.get('latest_date'), row['source'], utc_now()
        ),
    )
    conn.commit()
    conn.close()


def save_mf_metric(row: dict) -> None:
    conn = connect()
    conn.execute(
        'INSERT INTO mutual_fund_metrics(scheme_code,scheme_name,return_1y,return_3y,return_5y,'
        'latest_nav,latest_date,updated_at,source) VALUES(?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(scheme_code) DO UPDATE SET scheme_name=excluded.scheme_name,'
        'return_1y=excluded.return_1y,return_3y=excluded.return_3y,return_5y=excluded.return_5y,'
        'latest_nav=excluded.latest_nav,latest_date=excluded.latest_date,'
        'updated_at=excluded.updated_at,source=excluded.source',
        (
            row['scheme_code'], row['scheme_name'], row.get('return_1y'),
            row.get('return_3y'), row.get('return_5y'), row.get('latest_nav'),
            row.get('latest_date'), utc_now(), row['source']
        ),
    )
    conn.commit()
    conn.close()


def mutual_fund_metrics() -> list[dict]:
    conn = connect()
    rows = conn.execute('SELECT * FROM mutual_fund_metrics ORDER BY scheme_name').fetchall()
    conn.close()
    return [dict(r) for r in rows]
