import argparse
import hashlib
import json
import os
import sqlite3
import time as time_module
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv
try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


BASE_DIR = Path(__file__).resolve().parent
PERSISTENT_DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR))
DEFAULT_DB_FILE = Path(os.getenv("CRM_BRAIN_DB_PATH", str(PERSISTENT_DATA_DIR / "crm_brain.db")))
DEFAULT_STATE_FILE = Path(os.getenv("CRM_BRAIN_STATE_PATH", str(PERSISTENT_DATA_DIR / "state.json")))
DEFAULT_START_SYNC_UTC = "2026-04-14T18:30:00+00:00"
RAW_HISTORY_START_UTC = "2000-01-01T00:00:00+00:00"
HISTORICAL_FILE = BASE_DIR / "oct_november.xlsx"
HISTORICAL_CUTOFF_LOCAL = "2026-04-14"
LOCAL_TIMEZONE = "Asia/Kolkata"
WALKIN_PLACEHOLDER = "(walkin with no details)"
SOURCE_TABLE = "sales_items"
LOCAL_CLEANED_TABLE = "sales_cleaned_local"
LOCAL_REMOVED_TABLE = "sales_removed_local"
HISTORICAL_TABLE = "sales_raw"
RAW_HISTORY_TABLE = "sales_items_history"
JOINED_HISTORY_TABLE = "sales_items_joined"
SALES_RAW_COLUMNS = {
    "id",
    "sales_date",
    "sales_no",
    "mob_no",
    "net_amount",
    "branch_code",
    "order_type",
}
FETCH_PAGE_SIZE = 1000
SYNC_LOOKBACK_MINUTES = 10
UNWANTED_NAMES = [
    "Anmasa Noida",
    "ANMASA CONSUMER Noi 121",
    "ANMASA CONSUMER PRIVATE LIMITED",
    "Faridabad -",
    "Noida Warehouse -",
    "Gurgaon Central -",
]
SOURCE_COLUMN_MAP = {
    "sales_no": "OrderID",
    "mob_no": "Phone",
    "sales_date": "Date",
    "branch_name": "Branch",
    "order_type": "Order Type",
    "net_amount": "Amount",
    "customer_name": "Party Name",
    "product_name": "Product Name",
    "qty": "QTY",
    "other_discount": "Other Discount",
}


@dataclass
class PipelineSummary:
    source_rows: int
    removed_rows: int
    cleaned_rows: int
    incremental_rows: int
    source_amount: float
    removed_amount: float
    cleaned_amount: float
    source_max_utc: Optional[str]


@dataclass
class SupabaseConfig:
    url: str
    key: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read sales data from Supabase, clean it, group it, and store it in local SQLite."
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help="Path to the JSON state file that stores the last synced UTC timestamp.",
    )
    parser.add_argument(
        "--db-file",
        default=str(DEFAULT_DB_FILE),
        help="Path to the local SQLite database file.",
    )
    parser.add_argument(
        "--start-utc",
        default=DEFAULT_START_SYNC_UTC,
        help="Initial UTC timestamp to use when there is no state file.",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Rebuild all grouped rows from the configured start UTC timestamp.",
    )
    parser.add_argument(
        "--rebuild-yesterday",
        action="store_true",
        help="Delete local yesterday data and fetch that full local day again from Supabase.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep syncing in a loop.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Refresh interval for watch mode. Default is 300 seconds.",
    )
    parser.add_argument(
        "--raw-history-start-utc",
        default=RAW_HISTORY_START_UTC,
        help="Initial UTC timestamp for the full raw history mirror table.",
    )
    parser.add_argument(
        "--cleaned-output",
        help="Optional path to export the grouped cleaned data as Excel.",
    )
    parser.add_argument(
        "--removed-output",
        help="Optional path to export removed rows as Excel.",
    )
    return parser.parse_args()


def load_supabase_config() -> SupabaseConfig:
    load_dotenv(BASE_DIR / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        for candidate in (
            BASE_DIR / ".streamlit" / "secrets.toml",
            Path.home() / ".streamlit" / "secrets.toml",
        ):
            if not candidate.exists():
                continue
            with candidate.open("rb") as handle:
                secrets = tomllib.load(handle)
            url = url or secrets.get("SUPABASE_URL")
            key = key or secrets.get("SUPABASE_SERVICE_ROLE_KEY") or secrets.get("SUPABASE_KEY")
            if url and key:
                break
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE key in .env or Streamlit secrets.")
    return SupabaseConfig(url=url.rstrip("/"), key=key)


def parse_utc_timestamp(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def parse_utc_series(series: pd.Series) -> pd.Series:
    return series.apply(
        lambda value: parse_utc_timestamp(value) if str(value).strip() not in {"", "nan", "NaN", "None", "<NA>"} else pd.NaT
    )


def historical_cutoff_utc() -> pd.Timestamp:
    local_midnight = pd.Timestamp(HISTORICAL_CUTOFF_LOCAL).tz_localize(LOCAL_TIMEZONE) + pd.Timedelta(days=1)
    return local_midnight.tz_convert("UTC")


def read_state(state_path: Path, start_utc: str) -> str:
    if not state_path.exists():
        return start_utc

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if data.get("last_sync_utc"):
        return data["last_sync_utc"]
    if data.get("last_sync_date"):
        return "{0}T23:59:59+00:00".format(data["last_sync_date"])
    if data.get("last_sync"):
        return parse_utc_timestamp(data["last_sync"]).isoformat()
    return start_utc


def write_state(state_path: Path, last_sync_utc: str) -> None:
    parsed = parse_utc_timestamp(last_sync_utc)
    payload = {
        "last_sync_utc": parsed.isoformat(),
        "last_sync_date": parsed.date().isoformat(),
        "last_sync": parsed.isoformat(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clean_phone(value: Any) -> str:
    if pd.isna(value):
        return WALKIN_PLACEHOLDER
    text = str(value).strip()
    if text in {"", "nan", "NaN", "None", "<NA>"}:
        return WALKIN_PLACEHOLDER
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def clean_numeric_series(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(r"[,₹$()]", "", regex=True).replace("", "0"),
        errors="coerce",
    ).fillna(0.0)


def normalize_text_series(series: pd.Series) -> pd.Series:
    return (
        series.fillna("")
        .astype(str)
        .str.strip()
        .replace({"nan": "", "NaN": "", "None": "", "<NA>": ""})
    )


def update_branch(row: pd.Series) -> str:
    party_name = str(row.get("Party Name", "")).lower()
    branch = str(row.get("Branch", "")).lower()

    if "reliance retail" in party_name or "tifit services private limited" in party_name:
        if "noida" in branch:
            return "B2B NOIDA"
        if any(token in branch for token in ("gurgaon", "gurugram", "gargaon")):
            return "B2B GURGAON"
    return row.get("Branch", "")


def create_group_key(row: pd.Series) -> str:
    order_id = str(row.get("OrderID", "")).strip()
    order_type = str(row.get("Order Type", "")).strip()
    branch = str(row.get("Branch", "")).strip()
    phone = str(row.get("Phone", "")).strip()
    sales_date = row.get("Date")

    if order_id:
        return "ORDER_{0}_{1}_{2}".format(order_id, order_type, branch)

    date_str = sales_date.strftime("%Y%m%d") if pd.notna(sales_date) else "NODATE"
    phone_str = phone if phone else "NOPHONE"
    return "WALKIN_{0}_{1}_{2}".format(branch, date_str, phone_str)


def fetch_source_rows(
    config: SupabaseConfig,
    start_utc: str,
    end_utc: Optional[str] = None,
    select_columns: str = "*",
) -> pd.DataFrame:
    collected: List[Dict[str, Any]] = []
    offset = 0
    headers = {
        "apikey": config.key,
        "Authorization": "Bearer {0}".format(config.key),
    }
    session = requests.Session()
    session.trust_env = False
    while True:
        params = {
            "select": select_columns,
            "sales_date": "gte.{0}".format(start_utc),
            "order": "sales_date.asc",
            "offset": offset,
            "limit": FETCH_PAGE_SIZE,
        }
        if end_utc:
            params["and"] = "(sales_date.lt.{0})".format(end_utc)
        response = session.get(
            "{0}/rest/v1/{1}".format(config.url, SOURCE_TABLE),
            headers=headers,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json() or []
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < FETCH_PAGE_SIZE:
            break
        offset += FETCH_PAGE_SIZE

    df = pd.DataFrame(collected)
    if df.empty:
        return df

    return df


def prepare_cleaning_input(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df.empty:
        return raw_df.copy()

    df = raw_df.copy()
    available_map = {key: value for key, value in SOURCE_COLUMN_MAP.items() if key in df.columns}
    df = df.rename(columns=available_map)

    for column in ["OrderID", "Phone", "Date", "Branch", "Order Type", "Amount"]:
        if column not in df.columns:
            df[column] = None

    if "Party Name" not in df.columns:
        df["Party Name"] = ""
    if "Product Name" not in df.columns:
        df["Product Name"] = ""
    if "QTY" not in df.columns:
        df["QTY"] = 1
    if "Other Discount" not in df.columns:
        df["Other Discount"] = 0

    return df


def load_historical_sales_seed() -> pd.DataFrame:
    if not HISTORICAL_FILE.exists():
        raise FileNotFoundError("Historical file not found: {0}".format(HISTORICAL_FILE))

    frame = pd.read_excel(HISTORICAL_FILE, sheet_name=0)
    required_columns = ["Date", "OrderID", "Phone", "Amount", "Branch Code", "Order Type"]
    missing_columns = [column for column in required_columns if column not in frame.columns]
    if missing_columns:
        raise ValueError("Historical file is missing columns: {0}".format(", ".join(missing_columns)))

    frame = frame[required_columns].copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    frame = frame.dropna(subset=["Date"]).copy()
    frame = frame[frame["Date"].dt.date <= pd.Timestamp(HISTORICAL_CUTOFF_LOCAL).date()].copy()

    frame["OrderID"] = normalize_text_series(frame["OrderID"])
    frame["Phone"] = frame["Phone"].apply(clean_phone)
    frame["Amount"] = clean_numeric_series(frame["Amount"])
    frame["Branch Code"] = normalize_text_series(frame["Branch Code"])
    frame["Order Type"] = normalize_text_series(frame["Order Type"])

    frame["sales_date"] = (
        frame["Date"].dt.normalize().dt.tz_localize(LOCAL_TIMEZONE).dt.tz_convert("UTC").astype(str)
    )
    frame.rename(
        columns={
            "OrderID": "sales_no",
            "Phone": "mob_no",
            "Branch Code": "branch_code",
            "Order Type": "order_type",
            "Amount": "net_amount",
        },
        inplace=True,
    )
    frame.insert(0, "id", range(1, len(frame) + 1))
    return frame[["id", "sales_date", "sales_no", "mob_no", "net_amount", "branch_code", "order_type"]].copy()


def clean_sales_dataframe(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, PipelineSummary]:
    if df.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            PipelineSummary(0, 0, 0, 0, 0.0, 0.0, 0.0, None),
        )

    df = df.copy()
    df.columns = df.columns.str.strip()
    source_rows = len(df)
    source_amount = float(clean_numeric_series(df["Amount"]).sum()) if "Amount" in df.columns else 0.0

    for column in df.select_dtypes(include="object").columns:
        df[column] = normalize_text_series(df[column])

    df["sales_date_utc"] = parse_utc_series(df["Date"])
    df = df.dropna(subset=["sales_date_utc"]).copy()
    df["Date"] = df["sales_date_utc"].dt.tz_convert(LOCAL_TIMEZONE).dt.tz_localize(None)
    source_max_utc = None if df.empty else df["sales_date_utc"].max().tz_convert("UTC").isoformat()

    removed_frames: List[pd.DataFrame] = []

    df["QTY"] = pd.to_numeric(
        df["QTY"].astype(str).str.replace(r"[, ]", "", regex=True).replace("", "0"),
        errors="coerce",
    ).fillna(0.0)
    qty_zero = df[df["QTY"] == 0].copy()
    if not qty_zero.empty:
        qty_zero["Removal_Reason"] = "QTY = 0"
        removed_frames.append(qty_zero)
    df = df[df["QTY"] != 0].copy()
    df.drop(columns=["QTY"], inplace=True)

    pattern = "|".join(name.lower() for name in UNWANTED_NAMES)
    mask_unwanted = df.apply(
        lambda row: row.astype(str).str.lower().str.contains(pattern, regex=True).any(),
        axis=1,
    )
    unwanted_df = df[mask_unwanted].copy()
    if not unwanted_df.empty:
        unwanted_df["Removal_Reason"] = "Unwanted Company"
        removed_frames.append(unwanted_df)
    df = df[~mask_unwanted].copy()

    for column in ("Amount", "Other Discount"):
        if column in df.columns:
            df[column] = clean_numeric_series(df[column])

    zero_amount = df[df["Amount"] == 0].copy()
    if not zero_amount.empty:
        zero_amount["Removal_Reason"] = "Amount = 0"
        removed_frames.append(zero_amount)
    df = df[df["Amount"] != 0].copy()

    df["Phone"] = df["Phone"].apply(clean_phone)
    df["OrderID"] = normalize_text_series(df["OrderID"])
    df["Party Name"] = normalize_text_series(df["Party Name"])
    df["Product Name"] = normalize_text_series(df["Product Name"])
    df["Branch"] = normalize_text_series(df["Branch"])
    df["Order Type"] = normalize_text_series(df["Order Type"])

    df["Branch"] = df.apply(update_branch, axis=1)
    df["GroupKey"] = df.apply(create_group_key, axis=1)

    aggregated = (
        df.groupby("GroupKey", as_index=False)
        .agg(
            {
                "Date": "first",
                "sales_date_utc": "max",
                "OrderID": lambda values: ", ".join(value for value in values.unique() if value),
                "Phone": "first",
                "Amount": lambda values: round(float(sum(values)), 2),
                "Product Name": lambda values: ", ".join(dict.fromkeys(value for value in values if value)),
                "Branch": lambda values: values.mode().iat[0] if not values.mode().empty else values.iloc[0],
                "Order Type": lambda values: values.mode().iat[0] if not values.mode().empty else values.iloc[0],
            }
        )
        .rename(columns={"Branch": "Branch Code"})
    )

    if not aggregated.empty:
        aggregated["Amount"] = aggregated["Amount"].round(2)
        aggregated["sales_date"] = aggregated["Date"].dt.date
        aggregated["sales_date_utc"] = aggregated["sales_date_utc"].dt.tz_convert("UTC")
        aggregated["source_table"] = SOURCE_TABLE
        aggregated["synced_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        aggregated["record_id"] = aggregated.apply(build_record_id, axis=1)

    removed_rows = pd.concat(removed_frames, ignore_index=True) if removed_frames else pd.DataFrame()
    if not removed_rows.empty:
        removed_rows["removed_id"] = removed_rows.apply(build_removed_id, axis=1)

    removed_amount = (
        float(clean_numeric_series(removed_rows["Amount"]).sum())
        if not removed_rows.empty and "Amount" in removed_rows.columns
        else 0.0
    )

    summary = PipelineSummary(
        source_rows=source_rows,
        removed_rows=0 if removed_rows.empty else len(removed_rows),
        cleaned_rows=len(aggregated),
        incremental_rows=len(aggregated),
        source_amount=round(source_amount, 2),
        removed_amount=round(removed_amount, 2),
        cleaned_amount=round(float(aggregated["Amount"].sum()), 2) if not aggregated.empty else 0.0,
        source_max_utc=source_max_utc,
    )
    return aggregated, removed_rows, summary


def export_excel_if_requested(df: pd.DataFrame, output_path: Optional[str]) -> None:
    if not output_path:
        return
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    export_df = df.copy()
    for column in export_df.columns:
        if pd.api.types.is_datetime64_any_dtype(export_df[column]):
            export_df[column] = export_df[column].astype(str)
    export_df.to_excel(output_path, index=False)


def build_fetch_start_utc(state_utc: str, full_refresh: bool, start_utc: str) -> str:
    if full_refresh:
        return start_utc
    watermark = pd.Timestamp(state_utc)
    fetch_from = watermark - pd.Timedelta(minutes=SYNC_LOOKBACK_MINUTES)
    return fetch_from.tz_convert("UTC").isoformat()


def build_yesterday_window_utc(reference_time: Optional[datetime] = None) -> Tuple[str, str]:
    local_now = (
        pd.Timestamp(reference_time).tz_convert(LOCAL_TIMEZONE)
        if reference_time and pd.Timestamp(reference_time).tzinfo is not None
        else pd.Timestamp.now(tz=LOCAL_TIMEZONE)
    )
    yesterday_local = (local_now - pd.Timedelta(days=1)).normalize()
    local_end = yesterday_local + pd.Timedelta(days=1)
    return yesterday_local.tz_convert("UTC").isoformat(), local_end.tz_convert("UTC").isoformat()


def build_record_id(row: pd.Series) -> str:
    parts = [
        str(row.get("sales_date_utc", "")),
        str(row.get("OrderID", "")),
        str(row.get("Phone", "")),
        str(row.get("Branch Code", "")),
        str(row.get("Order Type", "")),
        "{0:.2f}".format(float(row.get("Amount", 0.0))),
    ]
    return "|".join(parts)


def build_removed_id(row: pd.Series) -> str:
    amount = row.get("Amount", 0.0)
    if pd.isna(amount):
        amount = 0.0
    parts = [
        str(row.get("sales_date_utc", "")),
        str(row.get("OrderID", "")),
        str(row.get("Phone", "")),
        str(row.get("Removal_Reason", "")),
        "{0:.2f}".format(float(amount)),
    ]
    return "|".join(parts)


def ensure_local_db(connection: sqlite3.Connection) -> None:
    expected_cleaned_columns = {
        "record_id",
        "sales_date",
        "sales_date_utc",
        "order_id",
        "phone",
        "amount",
        "branch_code",
        "order_type",
        "source_table",
        "synced_at",
    }
    expected_removed_columns = {
        "removed_id",
        "sales_date_utc",
        "order_id",
        "phone",
        "amount",
        "removal_reason",
        "source_table",
        "logged_at",
        "raw_payload",
    }

    existing_cleaned = {
        row[1]
        for row in connection.execute("PRAGMA table_info(sales_cleaned_local)").fetchall()
    }
    if existing_cleaned and not expected_cleaned_columns.issubset(existing_cleaned):
        connection.execute("DROP TABLE sales_cleaned_local")

    existing_removed = {
        row[1]
        for row in connection.execute("PRAGMA table_info(sales_removed_local)").fetchall()
    }
    if existing_removed and not expected_removed_columns.issubset(existing_removed):
        connection.execute("DROP TABLE sales_removed_local")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_cleaned_local (
            record_id TEXT PRIMARY KEY,
            sales_date TEXT NOT NULL,
            sales_date_utc TEXT NOT NULL,
            order_id TEXT,
            phone TEXT,
            amount REAL NOT NULL,
            branch_code TEXT,
            order_type TEXT,
            source_table TEXT NOT NULL,
            synced_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_removed_local (
            removed_id TEXT PRIMARY KEY,
            sales_date_utc TEXT,
            order_id TEXT,
            phone TEXT,
            amount REAL,
            removal_reason TEXT NOT NULL,
            source_table TEXT NOT NULL,
            logged_at TEXT NOT NULL,
            raw_payload TEXT NOT NULL
        )
        """
    )
    expected_sales_raw_columns = SALES_RAW_COLUMNS
    existing_sales_raw_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(sales_raw)").fetchall()
    }
    if existing_sales_raw_columns and existing_sales_raw_columns != expected_sales_raw_columns:
        connection.execute("DROP TABLE sales_raw")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_raw (
            id INTEGER,
            sales_date DATETIME,
            sales_no TEXT,
            mob_no TEXT,
            net_amount REAL,
            branch_code TEXT,
            order_type TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_date ON sales_cleaned_local (sales_date)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_date_utc ON sales_cleaned_local (sales_date_utc)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_phone ON sales_cleaned_local (phone)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sales_raw_sales_no ON sales_raw (sales_no)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sales_items_history (
            _row_hash TEXT PRIMARY KEY,
            _synced_at TEXT NOT NULL,
            _source_table TEXT NOT NULL
        )
        """
    )
    raw_history_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info({0})".format(RAW_HISTORY_TABLE)).fetchall()
    }
    if "sales_no" in raw_history_columns:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sales_items_history_sales_no ON sales_items_history (sales_no)"
        )
    connection.commit()


def clear_local_tables(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM {0}".format(LOCAL_CLEANED_TABLE))
    connection.execute("DELETE FROM {0}".format(LOCAL_REMOVED_TABLE))
    connection.execute("DELETE FROM {0}".format(HISTORICAL_TABLE))
    connection.execute("DELETE FROM {0}".format(RAW_HISTORY_TABLE))
    connection.execute("DROP TABLE IF EXISTS {0}".format(JOINED_HISTORY_TABLE))
    connection.commit()


def clear_local_window(connection: sqlite3.Connection, start_utc: str, end_utc: Optional[str]) -> None:
    if end_utc:
        cleaned_filter = "sales_date_utc >= ? AND sales_date_utc < ?"
        historical_filter = "sales_date >= ? AND sales_date < ?"
        params = (start_utc, end_utc)
    else:
        cleaned_filter = "sales_date_utc >= ?"
        historical_filter = "sales_date >= ?"
        params = (start_utc,)

    connection.execute(
        "DELETE FROM sales_cleaned_local WHERE {0}".format(cleaned_filter),
        params,
    )
    connection.execute(
        "DELETE FROM sales_removed_local WHERE {0}".format(cleaned_filter),
        params,
    )
    connection.execute(
        "DELETE FROM sales_raw WHERE {0}".format(historical_filter),
        params,
    )
    raw_history_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info({0})".format(RAW_HISTORY_TABLE)).fetchall()
    }
    if "sales_date" in raw_history_columns:
        connection.execute(
            "DELETE FROM sales_items_history WHERE {0}".format(historical_filter),
            params,
        )
    connection.commit()


def prepare_sqlite_cleaned_rows(df: pd.DataFrame) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    for _, row in df.iterrows():
        rows.append(
            (
                row["record_id"],
                row["sales_date"].isoformat() if pd.notna(row["sales_date"]) else None,
                row["sales_date_utc"].isoformat() if pd.notna(row["sales_date_utc"]) else None,
                row["OrderID"] or None,
                row["Phone"] or None,
                float(row["Amount"]),
                row["Branch Code"] or None,
                row["Order Type"] or None,
                row["source_table"],
                row["synced_at"],
            )
        )
    return rows


def prepare_sqlite_removed_rows(df: pd.DataFrame) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    if df.empty:
        return rows

    for _, row in df.iterrows():
        rows.append(
            (
                row["removed_id"],
                row["sales_date_utc"].isoformat() if pd.notna(row.get("sales_date_utc")) else None,
                row.get("OrderID") or None,
                row.get("Phone") or None,
                float(row["Amount"]) if pd.notna(row.get("Amount")) else None,
                row["Removal_Reason"],
                SOURCE_TABLE,
                datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                json.dumps(row.dropna().to_dict(), default=str, sort_keys=True),
            )
        )
    return rows


def prepare_sales_raw_rows(df: pd.DataFrame) -> List[Tuple[Any, ...]]:
    rows: List[Tuple[Any, ...]] = []
    for _, row in df.iterrows():
        rows.append(
            (
                row["OrderID"] or None,
                row["Phone"] or None,
                row["sales_date_utc"].isoformat() if pd.notna(row["sales_date_utc"]) else None,
                row["Branch Code"] or None,
                row["Order Type"] or None,
                float(row["Amount"]),
            )
        )
    return rows


def infer_sqlite_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "INTEGER"
    if pd.api.types.is_numeric_dtype(series):
        return "REAL"
    return "TEXT"


def ensure_raw_history_columns(connection: sqlite3.Connection, raw_df: pd.DataFrame) -> None:
    existing_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info({0})".format(RAW_HISTORY_TABLE)).fetchall()
    }
    for column in raw_df.columns:
        if column in existing_columns:
            continue
        connection.execute(
            'ALTER TABLE {0} ADD COLUMN "{1}" {2}'.format(
                RAW_HISTORY_TABLE,
                column,
                infer_sqlite_type(raw_df[column]),
            )
        )


def rebuild_joined_history_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS {0}".format(JOINED_HISTORY_TABLE))
    connection.execute(
        """
        CREATE TABLE {joined_table} AS
        SELECT
            h.*,
            r.sales_no AS cleaned_sales_no,
            r.mob_no AS cleaned_mob_no,
            r.sales_date AS cleaned_sales_date,
            r.branch_code AS cleaned_branch_code,
            r.order_type AS cleaned_order_type,
            r.net_amount AS cleaned_net_amount
        FROM {raw_table} h
        LEFT JOIN {historical_table} r
            ON h.sales_no = r.sales_no
        """.format(
            joined_table=JOINED_HISTORY_TABLE,
            raw_table=RAW_HISTORY_TABLE,
            historical_table=HISTORICAL_TABLE,
        )
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_{0}_sales_no ON {0} (sales_no)".format(JOINED_HISTORY_TABLE)
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_{0}_cleaned_sales_no ON {0} (cleaned_sales_no)".format(JOINED_HISTORY_TABLE)
    )


def prepare_raw_history_rows(raw_df: pd.DataFrame) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    if raw_df.empty:
        return [], []

    raw_copy = raw_df.copy()
    raw_copy = raw_copy.where(pd.notna(raw_copy), None)
    raw_copy["_synced_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    raw_copy["_source_table"] = SOURCE_TABLE
    raw_copy["_row_hash"] = raw_copy.apply(
        lambda row: hashlib.sha256(
            json.dumps(
                {key: row[key] for key in raw_df.columns},
                default=str,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        axis=1,
    )

    column_order = ["_row_hash", "_synced_at", "_source_table", *raw_df.columns.tolist()]
    rows = [tuple(row[column] for column in column_order) for _, row in raw_copy.iterrows()]
    return column_order, rows


def persist_raw_history(
    connection: sqlite3.Connection,
    raw_df: pd.DataFrame,
) -> None:
    if raw_df.empty:
        return

    ensure_raw_history_columns(connection, raw_df)
    column_order, rows = prepare_raw_history_rows(raw_df)
    if not rows:
        return

    quoted_columns = ", ".join('"{}"'.format(column) for column in column_order)
    placeholders = ", ".join("?" for _ in column_order)
    connection.executemany(
        "INSERT OR REPLACE INTO {0} ({1}) VALUES ({2})".format(
            RAW_HISTORY_TABLE,
            quoted_columns,
            placeholders,
        ),
        rows,
    )


def append_to_historical_table(connection: sqlite3.Connection, cleaned_df: pd.DataFrame) -> None:
    payload = prepare_sales_raw_rows(cleaned_df)
    if not payload:
        return

    connection.executemany(
        """
        INSERT INTO sales_raw (
            sales_no, mob_no, sales_date, branch_code, order_type, net_amount
        )
        SELECT ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1
            FROM sales_raw
            WHERE sales_no IS ?
              AND mob_no IS ?
              AND sales_date IS ?
              AND IFNULL(branch_code, '') = IFNULL(?, '')
              AND IFNULL(order_type, '') = IFNULL(?, '')
              AND net_amount = ?
        )
        """,
        [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
            )
            for row in payload
        ],
    )


def insert_historical_seed_rows(connection: sqlite3.Connection, historical_df: pd.DataFrame) -> None:
    if historical_df.empty:
        return
    payload = [
        (
            row["id"] if pd.notna(row["id"]) else None,
            row["sales_date"] or None,
            row["sales_no"] or None,
            row["mob_no"] or None,
            float(row["net_amount"]),
            row["branch_code"] or None,
            row["order_type"] or None,
        )
        for _, row in historical_df.iterrows()
    ]
    connection.executemany(
        """
        INSERT INTO sales_raw (
            id, sales_date, sales_no, mob_no, net_amount, branch_code, order_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def persist_to_local_db(
    db_path: Path,
    raw_df: pd.DataFrame,
    cleaned_df: pd.DataFrame,
    removed_rows: pd.DataFrame,
    full_refresh: bool,
    delete_window_utc: Optional[Tuple[str, str]] = None,
    historical_seed_df: Optional[pd.DataFrame] = None,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    try:
        ensure_local_db(connection)
        if full_refresh:
            clear_local_tables(connection)
            if historical_seed_df is not None:
                insert_historical_seed_rows(connection, historical_seed_df)
        elif delete_window_utc:
            clear_local_window(connection, delete_window_utc[0], delete_window_utc[1])

        cleaned_payload = prepare_sqlite_cleaned_rows(cleaned_df)
        if cleaned_payload:
            connection.executemany(
                """
                INSERT OR REPLACE INTO sales_cleaned_local (
                    record_id, sales_date, sales_date_utc, order_id, phone,
                    amount, branch_code, order_type, source_table, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                cleaned_payload,
            )

        append_to_historical_table(connection, cleaned_df)
        persist_raw_history(connection, raw_df)
        rebuild_joined_history_table(connection)

        removed_payload = prepare_sqlite_removed_rows(removed_rows)
        if removed_payload:
            connection.executemany(
                """
                INSERT OR REPLACE INTO sales_removed_local (
                    removed_id, sales_date_utc, order_id, phone, amount,
                    removal_reason, source_table, logged_at, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                removed_payload,
            )
        connection.commit()
    finally:
        connection.close()


def load_cleaned_sales_data(
    config: SupabaseConfig,
    state_path: Path,
    start_utc: str,
    raw_history_start_utc: str,
    full_refresh: bool,
    end_utc: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, PipelineSummary, str]:
    configured_start_utc = parse_utc_timestamp(start_utc).isoformat()
    configured_raw_history_start_utc = parse_utc_timestamp(raw_history_start_utc).isoformat()
    state_utc = configured_start_utc if full_refresh else read_state(state_path, configured_start_utc)
    fetch_start_utc = build_fetch_start_utc(state_utc, full_refresh, configured_start_utc)
    raw_fetch_start_utc = configured_raw_history_start_utc if full_refresh else fetch_start_utc
    raw_df = fetch_source_rows(config, raw_fetch_start_utc, end_utc=end_utc, select_columns="*")
    cleaned_input = prepare_cleaning_input(raw_df)
    cleaned_df, removed_rows, summary = clean_sales_dataframe(cleaned_input)
    if full_refresh and not cleaned_df.empty:
        cleaned_df = cleaned_df[cleaned_df["sales_date_utc"] >= historical_cutoff_utc()].copy()
    return raw_df, cleaned_df, removed_rows, summary, raw_fetch_start_utc


def load_rebuild_window_data(
    config: SupabaseConfig,
    start_utc: str,
    end_utc: str,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, PipelineSummary]:
    raw_df = fetch_source_rows(config, start_utc, end_utc=end_utc, select_columns="*")
    cleaned_input = prepare_cleaning_input(raw_df)
    cleaned_df, removed_rows, summary = clean_sales_dataframe(cleaned_input)
    return raw_df, cleaned_df, removed_rows, summary


def run_sync(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    db_path = Path(args.db_file).expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config = load_supabase_config()

    if args.rebuild_yesterday:
        rebuild_start_utc, rebuild_end_utc = build_yesterday_window_utc()
        raw_df, cleaned_df, removed_rows, summary = load_rebuild_window_data(
            config=config,
            start_utc=rebuild_start_utc,
            end_utc=rebuild_end_utc,
        )

        export_excel_if_requested(cleaned_df, args.cleaned_output)
        export_excel_if_requested(removed_rows, args.removed_output)

        print(
            "Rebuilding local yesterday window from {0} to {1} ({2})".format(
                rebuild_start_utc,
                rebuild_end_utc,
                LOCAL_TIMEZONE,
            )
        )
        print("Source rows: {0:,}".format(summary.source_rows))
        print("Removed rows: {0:,}".format(summary.removed_rows))
        print("Grouped cleaned rows: {0:,}".format(summary.cleaned_rows))
        print("Source amount: {0:,.2f}".format(summary.source_amount))
        print("Removed amount: {0:,.2f}".format(summary.removed_amount))
        print("Cleaned amount: {0:,.2f}".format(summary.cleaned_amount))

        persist_to_local_db(
            db_path,
            raw_df,
            cleaned_df,
            removed_rows,
            full_refresh=False,
            delete_window_utc=(rebuild_start_utc, rebuild_end_utc),
        )
        print("Local SQLite yesterday rebuild complete: {0}".format(db_path))
        print("State file left unchanged after yesterday rebuild.")
        return 0

    raw_df, cleaned_df, removed_rows, summary, fetch_start_utc = load_cleaned_sales_data(
        config=config,
        state_path=state_path,
        start_utc=args.start_utc,
        raw_history_start_utc=args.raw_history_start_utc,
        full_refresh=args.full_refresh,
    )

    export_excel_if_requested(cleaned_df, args.cleaned_output)
    export_excel_if_requested(removed_rows, args.removed_output)

    print("Fetching source rows from {0} starting at {1}".format(SOURCE_TABLE, fetch_start_utc))
    print("Source rows: {0:,}".format(summary.source_rows))
    print("Removed rows: {0:,}".format(summary.removed_rows))
    print("Grouped cleaned rows: {0:,}".format(summary.cleaned_rows))
    print("Source amount: {0:,.2f}".format(summary.source_amount))
    print("Removed amount: {0:,.2f}".format(summary.removed_amount))
    print("Cleaned amount: {0:,.2f}".format(summary.cleaned_amount))

    if cleaned_df.empty:
        print("No clean rows found in the fetched window.")
        if summary.source_max_utc:
            write_state(state_path, summary.source_max_utc)
        return 0

    persist_to_local_db(
        db_path,
        raw_df,
        cleaned_df,
        removed_rows,
        full_refresh=args.full_refresh,
        delete_window_utc=None if args.full_refresh else (fetch_start_utc, None),
        historical_seed_df=load_historical_sales_seed() if args.full_refresh else None,
    )

    if summary.source_max_utc:
        write_state(state_path, summary.source_max_utc)
        print("Updated state to {0}".format(summary.source_max_utc))
    else:
        print("State file left unchanged because no source timestamp was found.")

    print("Local SQLite refresh complete: {0}".format(db_path))
    return 0


def main() -> int:
    args = parse_args()
    if not args.watch:
        return run_sync(args)

    while True:
        started_at = datetime.now().isoformat(timespec="seconds")
        print("Starting sync cycle at {0}".format(started_at))
        try:
            run_sync(args)
        except Exception as exc:
            print("Sync cycle failed: {0}".format(exc))
        print("Sleeping for {0} seconds".format(args.interval_seconds))
        time_module.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
