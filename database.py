from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os

DB_PATH = Path(os.getenv("FINANX_DB", "finanx.db"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_database(app=None) -> None:
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            price REAL,
            previous_close REAL,
            day_change_pct REAL,
            volume REAL,
            source TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_time
        ON market_ticks(symbol, timestamp);

        CREATE TABLE IF NOT EXISTS market_latest (
            symbol TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            currency TEXT,
            timestamp TEXT NOT NULL,
            price REAL,
            previous_close REAL,
            day_change_pct REAL,
            volume REAL,
            source TEXT NOT NULL,
            freshness TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            close REAL,
            volume REAL,
            PRIMARY KEY(symbol, trade_date)
        );

        CREATE TABLE IF NOT EXISTS market_metrics (
            symbol TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            return_30d REAL,
            return_90d REAL,
            return_1y REAL,
            return_3y REAL,
            return_5y REAL,
            volatility_annualized REAL,
            max_drawdown REAL,
            trend_score REAL,
            data_points INTEGER,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS provider_status (
            provider TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_success TEXT,
            last_error TEXT,
            records_last_run INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS fd_rates (
            bank TEXT NOT NULL,
            tenure TEXT NOT NULL,
            annual_rate REAL NOT NULL,
            source_url TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(bank, tenure)
        );

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
        """
    )
    # Backward-compatible schema upgrades for an existing FinanX database.
    _ensure_column(conn, "market_metrics", "return_3y", "REAL")
    _ensure_column(conn, "market_metrics", "return_5y", "REAL")
    conn.commit()
    conn.close()


def save_tick(row: dict) -> None:
    conn = connect()
    conn.execute(
        """
        INSERT INTO market_ticks
        (symbol,category,name,timestamp,price,previous_close,day_change_pct,volume,source)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            row["symbol"], row["category"], row["name"], row["timestamp"],
            row.get("price"), row.get("previous_close"), row.get("day_change_pct"),
            row.get("volume"), row["source"],
        ),
    )
    conn.execute(
        """
        INSERT INTO market_latest
        (symbol,category,name,currency,timestamp,price,previous_close,day_change_pct,volume,source,freshness)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
          category=excluded.category,
          name=excluded.name,
          currency=excluded.currency,
          timestamp=excluded.timestamp,
          price=excluded.price,
          previous_close=excluded.previous_close,
          day_change_pct=excluded.day_change_pct,
          volume=excluded.volume,
          source=excluded.source,
          freshness=excluded.freshness
        """,
        (
            row["symbol"], row["category"], row["name"], row.get("currency"), row["timestamp"],
            row.get("price"), row.get("previous_close"), row.get("day_change_pct"),
            row.get("volume"), row["source"], row.get("freshness", "latest-available"),
        ),
    )
    conn.commit()
    conn.close()


def save_daily_history(symbol: str, rows: list[tuple[str, float | None, float | None]]) -> None:
    conn = connect()
    conn.executemany(
        """
        INSERT INTO daily_prices(symbol,trade_date,close,volume) VALUES(?,?,?,?)
        ON CONFLICT(symbol,trade_date) DO UPDATE SET close=excluded.close, volume=excluded.volume
        """,
        [(symbol, d, c, v) for d, c, v in rows],
    )
    conn.commit()
    conn.close()


def daily_closes(symbol: str, limit: int = 1600) -> list[float]:
    conn = connect()
    rows = conn.execute(
        "SELECT close FROM daily_prices WHERE symbol=? AND close IS NOT NULL ORDER BY trade_date DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    conn.close()
    return [float(r[0]) for r in reversed(rows)]


def save_metrics(metrics: dict) -> None:
    conn = connect()
    conn.execute(
        """
        INSERT INTO market_metrics
        (symbol,category,updated_at,return_30d,return_90d,return_1y,return_3y,return_5y,
         volatility_annualized,max_drawdown,trend_score,data_points,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
          category=excluded.category,
          updated_at=excluded.updated_at,
          return_30d=excluded.return_30d,
          return_90d=excluded.return_90d,
          return_1y=excluded.return_1y,
          return_3y=excluded.return_3y,
          return_5y=excluded.return_5y,
          volatility_annualized=excluded.volatility_annualized,
          max_drawdown=excluded.max_drawdown,
          trend_score=excluded.trend_score,
          data_points=excluded.data_points,
          status=excluded.status
        """,
        (
            metrics["symbol"], metrics["category"], metrics["updated_at"], metrics.get("return_30d"),
            metrics.get("return_90d"), metrics.get("return_1y"), metrics.get("return_3y"), metrics.get("return_5y"),
            metrics.get("volatility_annualized"), metrics.get("max_drawdown"), metrics.get("trend_score"),
            metrics.get("data_points", 0), metrics["status"],
        ),
    )
    conn.commit()
    conn.close()


def market_rows() -> list[dict]:
    conn = connect()
    rows = conn.execute(
        """
        SELECT ml.symbol,ml.category,ml.name,ml.currency,ml.timestamp,ml.price,ml.previous_close,
               ml.day_change_pct,ml.volume,ml.source,ml.freshness,
               mm.return_30d,mm.return_90d,mm.return_1y,mm.return_3y,mm.return_5y,
               mm.volatility_annualized,mm.max_drawdown,mm.trend_score,mm.data_points,mm.status AS metric_status
        FROM market_latest ml
        LEFT JOIN market_metrics mm ON mm.symbol=ml.symbol
        ORDER BY CASE ml.category
          WHEN 'STOCKS' THEN 1 WHEN 'FNO' THEN 2 WHEN 'GOLD' THEN 3 WHEN 'COMMODITY' THEN 4
          WHEN 'CURRENCY' THEN 5 WHEN 'BONDS' THEN 6 ELSE 7 END, ml.name
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recent_closes(symbol: str, limit: int = 400) -> list[float]:
    conn = connect()
    rows = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND price IS NOT NULL ORDER BY timestamp DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    conn.close()
    return [float(r[0]) for r in reversed(rows)]


def set_provider_status(provider: str, status: str, *, success: bool = False, error: str | None = None, records: int = 0) -> None:
    conn = connect()
    now = utc_now()
    conn.execute(
        """
        INSERT INTO provider_status(provider,status,updated_at,last_success,last_error,records_last_run)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(provider) DO UPDATE SET
          status=excluded.status,
          updated_at=excluded.updated_at,
          last_success=COALESCE(excluded.last_success,provider_status.last_success),
          last_error=excluded.last_error,
          records_last_run=excluded.records_last_run
        """,
        (provider, status, now, now if success else None, error, records),
    )
    conn.commit()
    conn.close()


def save_mf_scheme(row: dict) -> None:
    conn = connect()
    conn.execute(
        """
        INSERT INTO mutual_fund_schemes(scheme_code,scheme_name,isin,latest_nav,latest_date,source,updated_at)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(scheme_code) DO UPDATE SET
          scheme_name=excluded.scheme_name, isin=excluded.isin, latest_nav=excluded.latest_nav,
          latest_date=excluded.latest_date, source=excluded.source, updated_at=excluded.updated_at
        """,
        (row['scheme_code'], row['scheme_name'], row.get('isin'), row.get('latest_nav'), row.get('latest_date'), row['source'], utc_now())
    )
    conn.commit(); conn.close()


def save_mf_metric(row: dict) -> None:
    conn = connect()
    conn.execute(
        """
        INSERT INTO mutual_fund_metrics(scheme_code,scheme_name,return_1y,return_3y,return_5y,latest_nav,latest_date,updated_at,source)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(scheme_code) DO UPDATE SET
          scheme_name=excluded.scheme_name, return_1y=excluded.return_1y, return_3y=excluded.return_3y,
          return_5y=excluded.return_5y, latest_nav=excluded.latest_nav, latest_date=excluded.latest_date,
          updated_at=excluded.updated_at, source=excluded.source
        """,
        (row['scheme_code'], row['scheme_name'], row.get('return_1y'), row.get('return_3y'), row.get('return_5y'),
         row.get('latest_nav'), row.get('latest_date'), utc_now(), row['source'])
    )
    conn.commit(); conn.close()


def mutual_fund_metrics() -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM mutual_fund_metrics ORDER BY scheme_name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
