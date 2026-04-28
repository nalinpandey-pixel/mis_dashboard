import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import date, timedelta
from typing import Dict, List, Tuple

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

from sales_pipeline import (
    DEFAULT_DB_FILE,
    WALKIN_PLACEHOLDER,
    load_target_supabase_config,
    parse_utc_timestamp,
    supabase_headers,
)


APP_DIR = Path(__file__).resolve().parent
SUPABASE_FETCH_PAGE_SIZE = 1000
SUPABASE_SALES_RAW_TABLE = "sales_raw_backup"
SUPABASE_HISTORY_TABLE = "sales_items_history_backup"


B2B_BRANCHES = {"b2b noida", "b2b gurgaon"}
GIFT_MILESTONES = [
    ("Gift 1", "Glass Jar", "1000_tag", 1000),
    ("Gift 2", "Chakla Belan", "2000_tag", 2000),
    ("Gift 3", "Napkin Holder", "5000_tag", 5000),
    ("Gift 4", "Atta Maker", "10000_tag", 10000),
]


def normalize_branch(value: str) -> str:
    text = "" if value is None else str(value).strip()
    lowered = text.lower()
    if lowered in {"b2b noida", "b2b noida "}:
        return "B2B Noida"
    if lowered in {"b2b gurgaon", "b2b gurugram"}:
        return "B2B Gurgaon"
    return text or "Unspecified"


def normalize_order_type(value: str) -> str:
    text = "" if value is None else str(value).strip().lower()
    if text == "walkin":
        return "walkin"
    if text == "delivery":
        return "delivery"
    return ""


def safe_parse_sales_date(value: str) -> pd.Timestamp:
    return parse_utc_timestamp(value).tz_convert("Asia/Kolkata").tz_localize(None)


@st.cache_resource
def ensure_sqlite_indexes() -> bool:
    # Kept for compatibility with existing call sites; dashboard now reads from Supabase.
    return True


@st.cache_data(ttl=1800)
def load_supabase_table(table_name: str, select_columns: str = "*") -> pd.DataFrame:
    config = load_target_supabase_config()
    headers = supabase_headers(config)
    session = requests.Session()
    session.trust_env = False

    offset = 0
    all_rows: List[Dict[str, object]] = []
    while True:
        response = session.get(
            f"{config.url}/rest/v1/{table_name}",
            headers=headers,
            params={
                "select": select_columns,
                "order": "sales_date.asc",
                "limit": SUPABASE_FETCH_PAGE_SIZE,
                "offset": offset,
            },
            timeout=60,
        )
        response.raise_for_status()
        rows = response.json() or []
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < SUPABASE_FETCH_PAGE_SIZE:
            break
        offset += SUPABASE_FETCH_PAGE_SIZE

    return pd.DataFrame(all_rows)


@st.cache_data(ttl=1800)
def load_joined_items_from_supabase() -> pd.DataFrame:
    history = load_supabase_table(SUPABASE_HISTORY_TABLE)
    if history.empty:
        return history
    sales_raw = load_supabase_table(SUPABASE_SALES_RAW_TABLE, "sales_no,mob_no,sales_date,branch_code,order_type,net_amount")

    history["sales_no"] = history["sales_no"].fillna("").astype(str).str.strip()
    if sales_raw.empty:
        history["cleaned_sales_no"] = history["sales_no"]
        history["cleaned_mob_no"] = history["mob_no"]
        history["cleaned_sales_date"] = history["sales_date"]
        history["cleaned_branch_code"] = history.get("branch_name")
        history["cleaned_order_type"] = history.get("order_type")
        history["cleaned_net_amount"] = history.get("net_amount")
        return history

    sales_raw = sales_raw.copy()
    sales_raw["sales_no"] = sales_raw["sales_no"].fillna("").astype(str).str.strip()
    sales_raw["sales_date"] = sales_raw["sales_date"].fillna("").astype(str)
    sales_raw = sales_raw.sort_values("sales_date").drop_duplicates("sales_no", keep="last")
    sales_raw = sales_raw.rename(
        columns={
            "sales_no": "cleaned_sales_no",
            "mob_no": "cleaned_mob_no",
            "sales_date": "cleaned_sales_date",
            "branch_code": "cleaned_branch_code",
            "order_type": "cleaned_order_type",
            "net_amount": "cleaned_net_amount",
        }
    )
    merged = history.merge(
        sales_raw,
        left_on="sales_no",
        right_on="cleaned_sales_no",
        how="left",
    )
    return merged


@st.cache_data(ttl=3600)
def load_sales_data() -> pd.DataFrame:
    ensure_sqlite_indexes()
    frame = load_supabase_table(SUPABASE_SALES_RAW_TABLE, "sales_date,sales_no,mob_no,net_amount,branch_code,order_type")
    if not frame.empty:
        frame = frame.rename(
            columns={
                "sales_date": "salesDate",
                "sales_no": "order_id",
                "mob_no": "phone",
                "net_amount": "amount",
            }
        )

    if frame.empty:
        return frame

    frame["sales_date"] = frame["salesDate"].apply(safe_parse_sales_date)
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    frame["order_id"] = frame["order_id"].fillna("").astype(str).str.strip()
    frame["phone"] = frame["phone"].fillna("").astype(str).str.strip()
    frame["phone"] = frame["phone"].replace({"": WALKIN_PLACEHOLDER, "None": WALKIN_PLACEHOLDER})
    frame["branch_code"] = frame["branch_code"].apply(normalize_branch)
    frame["order_type"] = frame["order_type"].apply(normalize_order_type)
    frame["sales_day"] = frame["sales_date"].dt.normalize()
    frame["sales_month"] = frame["sales_date"].dt.to_period("M").dt.to_timestamp()
    frame["year"] = frame["sales_date"].dt.year
    frame["month_name"] = frame["sales_date"].dt.strftime("%B")
    frame["month_label"] = frame["sales_date"].dt.strftime("%Y %B")
    frame["is_online"] = frame["order_id"].str.contains("online", case=False, na=False)
    frame["channel"] = frame["is_online"].map({True: "Online revenue", False: "Offline revenue"})
    frame["is_walkin"] = frame["phone"].str.lower().str.contains("walkin", na=False)
    frame["walkin"] = frame["is_walkin"].astype(int)
    frame["nonwalkin"] = (~frame["is_walkin"]).astype(int)
    frame["is_b2b"] = frame["branch_code"].str.lower().isin(B2B_BRANCHES)
    frame["order_month"] = frame["sales_date"].dt.to_period("M").dt.to_timestamp()
    frame["order_month_label"] = frame["sales_date"].dt.strftime("%Y-%m")
    frame["year_month_label"] = frame["sales_date"].dt.strftime("%Y %B")

    first_order_lookup = (
        frame.loc[frame["phone"].ne(WALKIN_PLACEHOLDER)]
        .groupby("phone", as_index=False)
        .agg(first_order_date=("sales_date", "min"))
    )
    frame = frame.merge(first_order_lookup, on="phone", how="left")
    frame["first_order_date"] = frame["first_order_date"].where(
        frame["phone"].ne(WALKIN_PLACEHOLDER),
        frame["sales_date"],
    )
    frame["first_order_date"] = frame["first_order_date"].fillna(frame["sales_date"])
    frame["is_new_customer"] = (frame["sales_date"].dt.normalize() == frame["first_order_date"].dt.normalize()).astype(int)
    frame["cohort_month"] = frame["first_order_date"].dt.to_period("M").dt.to_timestamp()
    frame["cohort_month1"] = frame["cohort_month"].dt.strftime("%Y-%m")
    frame["days_diff"] = (frame["sales_day"] - frame["first_order_date"].dt.normalize()).dt.days
    frame["cohort_index"] = (
        (frame["order_month"].dt.to_period("M") - frame["cohort_month"].dt.to_period("M")).apply(lambda x: x.n)
        + 1
    )
    frame.loc[frame["phone"].eq(WALKIN_PLACEHOLDER), "cohort_index"] = -1
    frame["cohort_index"] = frame["cohort_index"].astype(int)
    frame["bin_value_30"] = frame["days_diff"].apply(
        lambda value: 0 if value == 0 else (int(value / 30) + 1) * 30
    )
    frame["bin_value_45"] = frame["days_diff"].apply(
        lambda value: 0 if value == 0 else (int(value / 45) + 1) * 45
    )
    frame["bin_value_60"] = frame["days_diff"].apply(
        lambda value: 0 if value == 0 else (int(value / 60) + 1) * 60
    )
    frame["bin_value_90"] = frame["days_diff"].apply(
        lambda value: 0 if value == 0 else (int(value / 90) + 1) * 90
    )
    frame.loc[frame["phone"].eq(WALKIN_PLACEHOLDER), "bin_value_30"] = -1
    frame.loc[frame["phone"].eq(WALKIN_PLACEHOLDER), "bin_value_45"] = -1
    frame.loc[frame["phone"].eq(WALKIN_PLACEHOLDER), "bin_value_60"] = -1
    frame.loc[frame["phone"].eq(WALKIN_PLACEHOLDER), "bin_value_90"] = -1
    frame["bin_range"] = pd.cut(
        frame["amount"],
        bins=[-float("inf"), 200, 400, 600, float("inf")],
        labels=["1-0–200", "2-201–400", "3-401–600", "4-600+"],
    ).astype(str)

    # Match Power BI DAX more closely:
    # Customer Order Count =
    # DISTINCTCOUNT(OrderID) for the same phone where month <= current month.
    order_history = frame.loc[frame["phone"].ne(WALKIN_PLACEHOLDER), ["phone", "order_id", "sales_month"]].copy()
    order_history["order_key"] = order_history["order_id"].replace("", "__BLANK_ORDER__")
    order_history = order_history.drop_duplicates(["phone", "order_key", "sales_month"])

    if order_history.empty:
        frame["customer_order_count"] = 0
    else:
        first_seen = (
            order_history.groupby(["phone", "order_key"], as_index=False)
            .agg(first_month=("sales_month", "min"))
            .sort_values(["phone", "first_month", "order_key"])
        )
        first_seen["new_orders_in_month"] = 1
        monthly_orders = (
            first_seen.groupby(["phone", "first_month"], as_index=False)
            .agg(new_orders_in_month=("new_orders_in_month", "sum"))
            .rename(columns={"first_month": "sales_month"})
            .sort_values(["phone", "sales_month"])
        )
        monthly_orders["customer_order_count"] = monthly_orders.groupby("phone")["new_orders_in_month"].cumsum()

        phone_months = (
            frame.loc[frame["phone"].ne(WALKIN_PLACEHOLDER), ["phone", "sales_month"]]
            .drop_duplicates()
            .sort_values(["phone", "sales_month"])
        )
        monthly_orders = (
            phone_months.merge(monthly_orders, on=["phone", "sales_month"], how="left")
            .sort_values(["phone", "sales_month"])
        )
        monthly_orders["new_orders_in_month"] = monthly_orders["new_orders_in_month"].fillna(0)
        monthly_orders["customer_order_count"] = monthly_orders.groupby("phone")["new_orders_in_month"].cumsum()
        frame = frame.merge(
            monthly_orders[["phone", "sales_month", "customer_order_count"]],
            on=["phone", "sales_month"],
            how="left",
        )
        frame["customer_order_count"] = frame["customer_order_count"].fillna(0).astype(int)

    frame["is_repeat_order"] = (frame["customer_order_count"] > 1).astype(int)
    frame["is_new_order"] = (frame["customer_order_count"] == 1).astype(int)
    frame["repeat_customer_flag"] = (
        (frame["is_repeat_order"] == 1) & (frame["nonwalkin"] == 1) & (~frame["is_b2b"])
    )
    frame["new_customer_flag"] = (
        (frame["is_new_order"] == 1) & (~frame["is_b2b"])
    )
    frame["walkin_customer_flag"] = (frame["walkin"] == 1) & (~frame["is_b2b"])
    frame["b2b_sales_flag"] = frame["is_b2b"]
    non_walkin_rank_source = frame[frame["phone"].ne(WALKIN_PLACEHOLDER)].copy()
    non_walkin_rank_source["order_rank"] = (
        non_walkin_rank_source.sort_values(["phone", "sales_date", "order_id"])
        .groupby("phone")["sales_date"]
        .rank(method="dense", ascending=True)
        .astype(int)
    )
    frame = frame.merge(
        non_walkin_rank_source[["phone", "salesDate", "order_id", "order_rank"]],
        on=["phone", "salesDate", "order_id"],
        how="left",
    )
    frame["order_rank"] = frame["order_rank"].fillna(0).astype(int)
    return frame


@st.cache_data(ttl=3600)
def load_product_penetration_data(
    query_start: str,
    query_end: str,
    branch_filter: str,
    type_filter: str,
) -> pd.DataFrame:
    frame = load_joined_items_from_supabase().copy()

    if frame.empty:
        return frame

    query_start_ts = pd.Timestamp(query_start)
    query_end_ts = pd.Timestamp(query_end)
    raw_sales_date = pd.to_datetime(frame["sales_date"], errors="coerce")
    frame = frame[(raw_sales_date >= query_start_ts) & (raw_sales_date <= query_end_ts)].copy()

    frame["sales_date"] = frame["cleaned_sales_date"].fillna(frame["sales_date"])
    frame["sales_date"] = frame["sales_date"].apply(safe_parse_sales_date)
    frame["sales_day"] = frame["sales_date"].dt.normalize()
    frame["sales_month"] = frame["sales_date"].dt.to_period("M").dt.to_timestamp()
    frame["year_month_label"] = frame["sales_date"].dt.strftime("%Y %B")
    frame["sales_no"] = frame["cleaned_sales_no"].fillna(frame["sales_no"]).fillna("").astype(str).str.strip()
    frame["mob_no"] = frame["cleaned_mob_no"].fillna(frame["mob_no"]).fillna("").astype(str).str.strip()
    frame["mob_no"] = frame["mob_no"].replace({"": WALKIN_PLACEHOLDER, "None": WALKIN_PLACEHOLDER})
    frame["branch_code"] = frame["cleaned_branch_code"].fillna(frame["branch_name"]).apply(normalize_branch)
    frame["order_type"] = frame["cleaned_order_type"].fillna(frame["order_type"]).apply(normalize_order_type)
    frame["product_name"] = frame["product_name"].fillna("").astype(str).str.strip()
    frame["category_name"] = frame["category_name"].fillna("").astype(str).str.strip()
    frame = frame[frame["product_name"] != ""].copy()
    frame["qty"] = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
    frame["net_amount"] = pd.to_numeric(frame["net_amount"], errors="coerce").fillna(0.0)
    if branch_filter != "All Branches":
        frame = frame[frame["branch_code"] == branch_filter].copy()
    if type_filter != "All Types":
        type_value = "" if type_filter == "Unspecified" else type_filter
        frame = frame[frame["order_type"] == type_value.lower()].copy()

    customer_profile = load_penetration_customer_profile()
    if not customer_profile.empty:
        frame = frame.merge(customer_profile, on="mob_no", how="left")
        frame["is_new_customer"] = (
            frame["mob_no"].ne(WALKIN_PLACEHOLDER)
            & frame["first_order_day"].notna()
            & frame["sales_day"].eq(frame["first_order_day"])
        )
        frame["is_one_time_customer"] = (
            frame["mob_no"].ne(WALKIN_PLACEHOLDER)
            & frame["lifetime_order_count"].fillna(0).le(1)
        )
    else:
        frame["first_order_day"] = pd.NaT
        frame["lifetime_order_count"] = 0
        frame["is_new_customer"] = False
        frame["is_one_time_customer"] = False
    return frame


@st.cache_data(ttl=3600)
def load_penetration_customer_profile() -> pd.DataFrame:
    frame = load_supabase_table(SUPABASE_SALES_RAW_TABLE, "sales_no,sales_date,mob_no")

    if frame.empty:
        return pd.DataFrame(columns=["mob_no", "first_order_day", "lifetime_order_count"])

    frame["sales_date"] = frame["sales_date"].apply(safe_parse_sales_date)
    frame["sales_day"] = frame["sales_date"].dt.normalize()
    frame["mob_no"] = frame["mob_no"].fillna("").astype(str).str.strip()
    frame["mob_no"] = frame["mob_no"].replace({"": WALKIN_PLACEHOLDER, "None": WALKIN_PLACEHOLDER})
    frame = frame[frame["mob_no"] != WALKIN_PLACEHOLDER].copy()
    if frame.empty:
        return pd.DataFrame(columns=["mob_no", "first_order_day", "lifetime_order_count"])

    order_key = frame["sales_no"].fillna("").astype(str).str.strip()
    order_key = order_key.where(order_key.ne(""), frame["mob_no"] + "|" + frame["sales_day"].astype(str))
    frame["order_key"] = order_key

    profile = (
        frame.groupby("mob_no", as_index=False)
        .agg(
            first_order_day=("sales_day", "min"),
            lifetime_order_count=("order_key", "nunique"),
        )
    )
    return profile


@st.cache_data(ttl=3600)
def load_persona_item_data(branch_filter: str, type_filter: str) -> pd.DataFrame:
    frame = load_joined_items_from_supabase().copy()

    if frame.empty:
        return frame

    frame["sales_date"] = frame["cleaned_sales_date"].fillna(frame["sales_date"])
    frame["sales_date"] = frame["sales_date"].apply(safe_parse_sales_date)
    frame["sales_day"] = frame["sales_date"].dt.normalize()
    frame["sales_no"] = frame["cleaned_sales_no"].fillna(frame["sales_no"]).fillna("").astype(str).str.strip()
    frame["mob_no"] = frame["cleaned_mob_no"].fillna(frame["mob_no"]).fillna("").astype(str).str.strip()
    frame["mob_no"] = frame["mob_no"].replace({"": WALKIN_PLACEHOLDER, "None": WALKIN_PLACEHOLDER})
    frame["branch_code"] = frame["cleaned_branch_code"].fillna(frame["branch_name"]).apply(normalize_branch)
    frame["order_type"] = frame["cleaned_order_type"].fillna(frame["order_type"]).apply(normalize_order_type)
    frame["product_name"] = frame["product_name"].fillna("").astype(str).str.strip()
    frame["category_name"] = frame["category_name"].fillna("").astype(str).str.strip()
    frame["qty"] = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
    frame["net_amount"] = pd.to_numeric(frame["net_amount"], errors="coerce").fillna(0.0)
    frame = frame[(frame["product_name"] != "") & (frame["mob_no"] != WALKIN_PLACEHOLDER)].copy()
    if branch_filter != "All Branches":
        frame = frame[frame["branch_code"] == branch_filter].copy()
    if type_filter != "All Types":
        type_value = "" if type_filter == "Unspecified" else type_filter
        frame = frame[frame["order_type"] == type_value.lower()].copy()
    return frame


def build_persona_product_mix(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()

    summary = (
        frame.groupby(["product_name", "category_name"], as_index=False)
        .agg(
            qty=("qty", "sum"),
            revenue=("net_amount", "sum"),
            orders=("sales_no", "nunique"),
            last_bought=("sales_day", "max"),
        )
        .sort_values(["revenue", "qty"], ascending=[False, False])
    )
    total_revenue = float(summary["revenue"].sum())
    summary["revenue_mix_pct"] = 0.0 if total_revenue == 0 else (summary["revenue"] / total_revenue) * 100
    summary["last_bought"] = pd.to_datetime(summary["last_bought"]).dt.strftime("%Y-%m-%d")
    return summary


def build_persona_repeat_profile(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()

    analysis_date = frame["sales_day"].max()
    rows: List[Dict[str, object]] = []
    for product_name, product_frame in frame.groupby("product_name"):
        purchase_days = (
            product_frame["sales_day"]
            .drop_duplicates()
            .sort_values()
            .reset_index(drop=True)
        )
        if purchase_days.empty:
            continue
        gaps = purchase_days.diff().dropna().dt.days
        avg_repeat_days = float(gaps.mean()) if not gaps.empty else None
        last_bought = purchase_days.iloc[-1]
        days_since_last = int((analysis_date - last_bought).days)
        rows.append(
            {
                "product_name": product_name,
                "purchase_days": int(len(purchase_days)),
                "repeat_cycles": int(len(gaps)),
                "avg_repeat_days": None if avg_repeat_days is None else round(avg_repeat_days, 1),
                "last_bought": pd.to_datetime(last_bought).strftime("%Y-%m-%d"),
                "days_since_last_purchase": days_since_last,
                "reorder_signal": (
                    "Needs attention"
                    if avg_repeat_days is not None and days_since_last >= avg_repeat_days
                    else "Watch"
                ),
            }
        )

    repeat_profile = pd.DataFrame(rows)
    if repeat_profile.empty:
        return repeat_profile
    repeat_profile["avg_repeat_days_sort"] = repeat_profile["avg_repeat_days"].fillna(10**9)
    repeat_profile = repeat_profile.sort_values(
        ["reorder_signal", "avg_repeat_days_sort", "purchase_days", "product_name"],
        ascending=[True, True, False, True],
    ).drop(columns=["avg_repeat_days_sort"])
    return repeat_profile


def build_persona_recommendations(frame: pd.DataFrame, limit: int) -> pd.DataFrame:
    repeat_profile = build_persona_repeat_profile(frame)
    if repeat_profile.empty:
        return repeat_profile
    ranked = repeat_profile[repeat_profile["avg_repeat_days"].notna()].copy()
    if ranked.empty:
        return pd.DataFrame()
    ranked["gap_vs_average"] = ranked["days_since_last_purchase"] - ranked["avg_repeat_days"]
    ranked = ranked.sort_values(
        ["gap_vs_average", "purchase_days"],
        ascending=[False, False],
    )
    return ranked.head(limit)


def build_customer_churn_profile(
    sales_frame: pd.DataFrame,
    item_frame: pd.DataFrame,
    reference_date: date,
    churn_days: int = 60,
) -> pd.DataFrame:
    base_sales = sales_frame[
        (sales_frame["phone"] != WALKIN_PLACEHOLDER)
        & (~sales_frame["is_b2b"])
    ].copy()
    if base_sales.empty:
        return pd.DataFrame()

    reference_day = pd.Timestamp(reference_date).normalize()
    order_key = base_sales["order_id"].fillna("").astype(str).str.strip()
    order_key = order_key.where(order_key.ne(""), base_sales["phone"] + "|" + base_sales["sales_day"].astype(str))
    base_sales["order_key"] = order_key

    base_sales = base_sales.sort_values(["phone", "sales_day", "order_key"])
    order_summary = (
        base_sales.groupby(["phone", "sales_day", "order_key"], as_index=False)
        .agg(order_amount=("amount", "sum"))
    )

    customer_summary = (
        order_summary.groupby("phone", as_index=False)
        .agg(
            first_purchase=("sales_day", "min"),
            last_purchase=("sales_day", "max"),
            total_orders=("order_key", "nunique"),
            total_spend=("order_amount", "sum"),
        )
    )
    customer_summary["avg_order_value"] = (
        customer_summary["total_spend"] / customer_summary["total_orders"].replace(0, pd.NA)
    ).fillna(0.0)
    customer_summary["days_since_last_purchase"] = (
        reference_day - customer_summary["last_purchase"]
    ).dt.days.astype(int)

    orders_last_30 = (
        order_summary[order_summary["sales_day"] >= (reference_day - pd.Timedelta(days=30))]
        .groupby("phone")["order_key"].nunique()
        .rename("orders_last_30")
    )
    orders_last_60 = (
        order_summary[order_summary["sales_day"] >= (reference_day - pd.Timedelta(days=60))]
        .groupby("phone")["order_key"].nunique()
        .rename("orders_last_60")
    )
    orders_last_90 = (
        order_summary[order_summary["sales_day"] >= (reference_day - pd.Timedelta(days=90))]
        .groupby("phone")["order_key"].nunique()
        .rename("orders_last_90")
    )
    spend_last_90 = (
        base_sales[base_sales["sales_day"] >= (reference_day - pd.Timedelta(days=90))]
        .groupby("phone")["amount"].sum()
        .rename("spend_last_90")
    )

    reorder_base = (
        order_summary.sort_values(["phone", "sales_day"])
        .drop_duplicates(["phone", "sales_day"])
        .copy()
    )
    reorder_base["days_gap"] = reorder_base.groupby("phone")["sales_day"].diff().dt.days
    reorder_profile = (
        reorder_base.groupby("phone", as_index=False)
        .agg(
            avg_reorder_days=("days_gap", "mean"),
            max_reorder_days=("days_gap", "max"),
        )
    )

    customer_summary = customer_summary.merge(orders_last_30, on="phone", how="left")
    customer_summary = customer_summary.merge(orders_last_60, on="phone", how="left")
    customer_summary = customer_summary.merge(orders_last_90, on="phone", how="left")
    customer_summary = customer_summary.merge(spend_last_90, on="phone", how="left")
    customer_summary = customer_summary.merge(reorder_profile, on="phone", how="left")

    fill_zero_columns = ["orders_last_30", "orders_last_60", "orders_last_90", "spend_last_90"]
    customer_summary[fill_zero_columns] = customer_summary[fill_zero_columns].fillna(0)
    customer_summary["avg_reorder_days"] = customer_summary["avg_reorder_days"].round(1)
    customer_summary["max_reorder_days"] = customer_summary["max_reorder_days"].fillna(0).astype(int)

    def classify_status(days_since_last: int) -> str:
        if days_since_last > churn_days:
            return "Churned"
        if days_since_last > 30:
            return "At Risk"
        return "Active"

    customer_summary["status"] = customer_summary["days_since_last_purchase"].apply(classify_status)
    customer_summary["customer_segment"] = pd.cut(
        customer_summary["total_spend"],
        bins=[-float("inf"), 2000, 5000, 15000, float("inf")],
        labels=["Low Value", "Growing", "Core", "VIP"],
    ).astype(str)

    if not item_frame.empty:
        item_base = item_frame.copy()
        item_base["sales_day"] = pd.to_datetime(item_base["sales_day"]).dt.normalize()

        product_pref = (
            item_base.groupby(["mob_no", "product_name"], as_index=False)
            .agg(product_revenue=("net_amount", "sum"), product_qty=("qty", "sum"), last_bought=("sales_day", "max"))
            .sort_values(["mob_no", "product_revenue", "product_qty"], ascending=[True, False, False])
            .drop_duplicates("mob_no")
            .rename(
                columns={
                    "mob_no": "phone",
                    "product_name": "favorite_product",
                    "product_revenue": "favorite_product_revenue",
                    "last_bought": "favorite_product_last_bought",
                }
            )
        )
        category_pref = (
            item_base[item_base["category_name"] != ""]
            .groupby(["mob_no", "category_name"], as_index=False)
            .agg(category_revenue=("net_amount", "sum"))
            .sort_values(["mob_no", "category_revenue"], ascending=[True, False])
            .drop_duplicates("mob_no")
            .rename(columns={"mob_no": "phone", "category_name": "favorite_category"})
        )
        product_repeat = (
            item_base.sort_values(["mob_no", "product_name", "sales_day"])
            .drop_duplicates(["mob_no", "product_name", "sales_day"])
            .copy()
        )
        product_repeat["product_gap"] = product_repeat.groupby(["mob_no", "product_name"])["sales_day"].diff().dt.days
        product_repeat = (
            product_repeat.groupby(["mob_no", "product_name"], as_index=False)
            .agg(avg_product_repeat_days=("product_gap", "mean"), product_purchase_days=("sales_day", "nunique"))
            .sort_values(["mob_no", "product_purchase_days", "avg_product_repeat_days"], ascending=[True, False, True])
        )
        product_repeat = product_repeat.drop_duplicates("mob_no").rename(
            columns={"mob_no": "phone", "product_name": "repeat_anchor_product"}
        )

        customer_summary = customer_summary.merge(product_pref, on="phone", how="left")
        customer_summary = customer_summary.merge(category_pref, on="phone", how="left")
        customer_summary = customer_summary.merge(product_repeat, on="phone", how="left")

    customer_summary["favorite_product_last_bought"] = pd.to_datetime(
        customer_summary["favorite_product_last_bought"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    customer_summary["priority_score"] = (
        customer_summary["total_spend"].rank(pct=True).fillna(0) * 50
        + customer_summary["days_since_last_purchase"].clip(upper=120) / 120 * 30
        + customer_summary["orders_last_90"].eq(0).astype(int) * 20
    ).round(1)

    def safe_label(value: object, fallback: str) -> str:
        if pd.isna(value):
            return fallback
        text = str(value).strip()
        return text if text else fallback

    def action_hint(row: pd.Series) -> str:
        repeat_product = safe_label(row.get("repeat_anchor_product"), "")
        favorite_product = safe_label(row.get("favorite_product"), "")
        favorite_category = safe_label(row.get("favorite_category"), "")
        if row["status"] == "Churned":
            target = repeat_product or favorite_product or "core staples"
            return "Win-back on " + target
        if row["status"] == "At Risk":
            target = repeat_product or favorite_product or "recent products"
            return "Reminder for " + target
        target = favorite_category or "basket mix"
        return "Upsell " + target

    customer_summary["action_hint"] = customer_summary.apply(action_hint, axis=1)
    return customer_summary.sort_values(["priority_score", "total_spend"], ascending=[False, False])


def build_churn_summary_cards(profile: pd.DataFrame) -> Dict[str, float]:
    if profile.empty:
        return {
            "Customers": 0,
            "Active": 0,
            "At Risk": 0,
            "Churned": 0,
            "Churn Rate %": 0.0,
        }
    total_customers = int(profile["phone"].nunique())
    active = int((profile["status"] == "Active").sum())
    at_risk = int((profile["status"] == "At Risk").sum())
    churned = int((profile["status"] == "Churned").sum())
    churn_rate = 0.0 if total_customers == 0 else (churned / total_customers) * 100
    return {
        "Customers": total_customers,
        "Active": active,
        "At Risk": at_risk,
        "Churned": churned,
        "Churn Rate %": round(churn_rate, 2),
    }


def build_churn_status_mix(profile: pd.DataFrame) -> pd.DataFrame:
    if profile.empty:
        return pd.DataFrame(columns=["status", "customers"])
    return (
        profile.groupby("status", as_index=False)
        .agg(customers=("phone", "nunique"))
        .sort_values("customers", ascending=False)
    )


def build_churn_segment_summary(profile: pd.DataFrame) -> pd.DataFrame:
    if profile.empty:
        return pd.DataFrame()
    summary = (
        profile.groupby(["customer_segment", "status"], as_index=False)
        .agg(
            customers=("phone", "nunique"),
            revenue=("total_spend", "sum"),
        )
    )
    return summary.sort_values(["customer_segment", "status"])


def build_monthly_churn_trend(profile: pd.DataFrame) -> pd.DataFrame:
    if profile.empty:
        return pd.DataFrame()
    trend = profile.copy()
    trend["last_purchase_month"] = pd.to_datetime(trend["last_purchase"]).dt.to_period("M").dt.to_timestamp()
    trend["month_label"] = trend["last_purchase_month"].dt.strftime("%Y-%m")
    return (
        trend.groupby(["month_label", "status"], as_index=False)
        .agg(customers=("phone", "nunique"))
        .sort_values("month_label")
    )


def build_churn_action_list(profile: pd.DataFrame, limit: int) -> pd.DataFrame:
    if profile.empty:
        return pd.DataFrame()
    action_list = profile[profile["status"].isin(["At Risk", "Churned"])].copy()
    if action_list.empty:
        return pd.DataFrame()
    action_list["days_over_repeat_gap"] = (
        action_list["days_since_last_purchase"] - action_list["avg_reorder_days"].fillna(action_list["days_since_last_purchase"])
    ).round(1)
    action_list = action_list.sort_values(
        ["status", "priority_score", "days_since_last_purchase", "total_spend"],
        ascending=[True, False, False, False],
    )
    return action_list.head(limit)


def build_churn_reason_analysis(profile: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, float]]:
    if profile.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {
            "churned_customers": 0,
            "one_time_pct": 0.0,
            "no_orders_l90_pct": 0.0,
            "cycle_break_pct": 0.0,
        }

    churned = profile[profile["status"] == "Churned"].copy()
    if churned.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {
            "churned_customers": 0,
            "one_time_pct": 0.0,
            "no_orders_l90_pct": 0.0,
            "cycle_break_pct": 0.0,
        }

    churned["one_time_buyer"] = churned["total_orders"].le(1)
    churned["no_orders_l90"] = churned["orders_last_90"].eq(0)
    churned["repeat_cycle_broken"] = (
        churned["avg_reorder_days"].notna()
        & churned["avg_reorder_days"].gt(0)
        & churned["days_since_last_purchase"].gt(churned["avg_reorder_days"] * 1.5)
    )

    def reason_tag(row: pd.Series) -> str:
        if bool(row["one_time_buyer"]):
            return "One-time buyer did not return"
        if bool(row["no_orders_l90"]):
            return "No orders in last 90 days"
        if bool(row["repeat_cycle_broken"]):
            return "Missed expected reorder cycle"
        if str(row.get("customer_segment", "")) == "Low Value":
            return "Low-value sporadic behavior"
        return "Needs manual review"

    churned["primary_churn_reason"] = churned.apply(reason_tag, axis=1)

    reason_summary = (
        churned.groupby("primary_churn_reason", as_index=False)
        .agg(
            customers=("phone", "nunique"),
            revenue_lost=("total_spend", "sum"),
            avg_days_since_last=("days_since_last_purchase", "mean"),
        )
        .sort_values(["customers", "revenue_lost"], ascending=[False, False])
    )
    reason_summary["avg_days_since_last"] = reason_summary["avg_days_since_last"].round(1)

    category_risk = (
        churned[churned["favorite_category"].notna() & churned["favorite_category"].astype(str).ne("")]
        .groupby("favorite_category", as_index=False)
        .agg(
            churned_customers=("phone", "nunique"),
            churned_revenue=("total_spend", "sum"),
        )
        .sort_values(["churned_customers", "churned_revenue"], ascending=[False, False])
    )

    total_churned = max(int(churned["phone"].nunique()), 1)
    metrics = {
        "churned_customers": int(churned["phone"].nunique()),
        "one_time_pct": round((float(churned["one_time_buyer"].sum()) / total_churned) * 100, 2),
        "no_orders_l90_pct": round((float(churned["no_orders_l90"].sum()) / total_churned) * 100, 2),
        "cycle_break_pct": round((float(churned["repeat_cycle_broken"].sum()) / total_churned) * 100, 2),
    }
    return reason_summary, churned, category_risk, metrics


def render_churn_definitions(churn_days: int) -> None:
    with st.expander("Definitions", expanded=False):
        st.markdown(
            "\n".join(
                [
                    f"- `Churned`: customer whose `Days Since Last Purchase` is more than `{churn_days}` days.",
                    "- `At Risk`: customer with no purchase in the last `31-60` days.",
                    "- `Active`: customer with a purchase in the last `0-30` days.",
                    "- `Days Since Last Purchase`: number of days from selected end date to latest purchase date.",
                    "- `Avg Reorder Days`: average gap in days between customer orders.",
                    "- `Days Over Repeat Gap`: `Days Since Last Purchase - Avg Reorder Days`.",
                    "- `Total Orders`: distinct order count for that customer (non-walkin, non-B2B scope).",
                    "- `Total Spend`: total revenue generated by that customer in scope.",
                    "- `AOV`: Average Order Value (`Total Spend / Total Orders`).",
                    "- `Orders L30 / L60 / L90`: distinct orders in last 30/60/90 days.",
                    "- `Favorite Category`: category with highest revenue contribution for that customer.",
                    "- `Favorite Product`: product with highest revenue contribution for that customer.",
                    "- `Repeat Product`: most stable repeatedly bought product for that customer.",
                    "- `Priority Score`: ranking score to prioritize follow-up; higher means higher attention needed.",
                    "- `Action Hint`: suggested next action (reminder, win-back, or upsell direction).",
                ]
            )
        )


def check_local_db_ready() -> bool:
    try:
        config = load_target_supabase_config()
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            f"{config.url}/rest/v1/{SUPABASE_SALES_RAW_TABLE}",
            headers=supabase_headers(config, {"Prefer": "count=exact"}),
            params={"select": "sales_no", "limit": 1},
            timeout=30,
        )
        response.raise_for_status()
    except Exception:
        return False
    return True


def local_db_has_sales_rows() -> bool:
    if not check_local_db_ready():
        return False
    try:
        count_df = load_supabase_table(SUPABASE_SALES_RAW_TABLE, "id")
    except Exception:
        return False
    return not count_df.empty


def run_pipeline_command(*extra_args: str) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, str(APP_DIR / "sales_pipeline.py"), *extra_args],
            cwd=str(APP_DIR),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        return False, str(exc)

    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return result.returncode == 0, output.strip()


def build_measure_cards(frame: pd.DataFrame) -> Dict[str, float]:
    if frame.empty:
        return {
            "Net Sales": 0.0,
            "Orders": 0,
            "Customers": 0,
            "Repeat Customers": 0,
            "Average Order Value": 0.0,
        }

    order_keys = frame["order_id"].replace("", pd.NA)
    order_keys = order_keys.where(order_keys.notna(), pd.Series(frame.index.astype(str), index=frame.index))
    orders = int(order_keys.nunique())
    customers = int(frame.loc[frame["phone"].ne(WALKIN_PLACEHOLDER), "phone"].nunique())
    repeat_customers = int(frame.loc[frame["repeat_customer_flag"], "phone"].nunique())
    net_sales = float(frame["amount"].sum())
    aov = net_sales / orders if orders else 0.0
    return {
        "Net Sales": net_sales,
        "Orders": orders,
        "Customers": customers,
        "Repeat Customers": repeat_customers,
        "Average Order Value": aov,
    }


def shift_date_one_month(value: date) -> date:
    timestamp = pd.Timestamp(value)
    shifted = timestamp - pd.DateOffset(months=1)
    return shifted.date()


def build_product_summary(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=["product_name", "qty", "revenue", "orders", "customers", "order_penetration", "customer_penetration"]
        )

    total_orders = max(frame["sales_no"].replace("", pd.NA).nunique(), 1)
    total_customers = max(frame.loc[frame["mob_no"].ne(WALKIN_PLACEHOLDER), "mob_no"].nunique(), 1)
    summary = (
        frame.groupby("product_name", as_index=False)
        .agg(
            qty=("qty", "sum"),
            revenue=("net_amount", "sum"),
            orders=("sales_no", "nunique"),
            customers=("mob_no", lambda values: values[values != WALKIN_PLACEHOLDER].nunique()),
        )
        .sort_values(["revenue", "qty"], ascending=[False, False])
    )
    summary["order_penetration"] = (summary["orders"] / total_orders) * 100
    summary["customer_penetration"] = (summary["customers"] / total_customers) * 100
    return summary


def build_category_summary(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["category_name", "qty", "revenue", "orders", "customers", "customer_penetration"])

    category_frame = frame[frame["category_name"] != ""].copy()
    if category_frame.empty:
        return pd.DataFrame(columns=["category_name", "qty", "revenue", "orders", "customers", "customer_penetration"])

    total_customers = max(category_frame.loc[category_frame["mob_no"].ne(WALKIN_PLACEHOLDER), "mob_no"].nunique(), 1)
    summary = (
        category_frame.groupby("category_name", as_index=False)
        .agg(
            qty=("qty", "sum"),
            revenue=("net_amount", "sum"),
            orders=("sales_no", "nunique"),
            customers=("mob_no", lambda values: values[values != WALKIN_PLACEHOLDER].nunique()),
        )
        .sort_values(["customers", "revenue", "qty"], ascending=[False, False, False])
    )
    summary["customer_penetration"] = (summary["customers"] / total_customers) * 100
    return summary


def compare_product_summary(current_frame: pd.DataFrame, previous_frame: pd.DataFrame) -> pd.DataFrame:
    current_summary = build_product_summary(current_frame)
    previous_summary = build_product_summary(previous_frame)
    merged = current_summary.merge(
        previous_summary,
        on="product_name",
        how="outer",
        suffixes=("_current", "_previous"),
    ).fillna(0)
    merged["qty_change"] = merged["qty_current"] - merged["qty_previous"]
    merged["revenue_change"] = merged["revenue_current"] - merged["revenue_previous"]
    merged["orders_change"] = merged["orders_current"] - merged["orders_previous"]
    merged["customers_change"] = merged["customers_current"] - merged["customers_previous"]
    merged["order_penetration_change"] = (
        merged["order_penetration_current"] - merged["order_penetration_previous"]
    )
    merged["customer_penetration_change"] = (
        merged["customer_penetration_current"] - merged["customer_penetration_previous"]
    )
    merged["revenue_change_pct"] = merged.apply(
        lambda row: 0.0 if row["revenue_previous"] == 0 else (row["revenue_change"] / row["revenue_previous"]) * 100,
        axis=1,
    )
    merged["qty_change_pct"] = merged.apply(
        lambda row: 0.0 if row["qty_previous"] == 0 else (row["qty_change"] / row["qty_previous"]) * 100,
        axis=1,
    )
    return merged.sort_values(["revenue_change", "qty_change"], ascending=[False, False])


def compare_category_summary(current_frame: pd.DataFrame, previous_frame: pd.DataFrame) -> pd.DataFrame:
    current_summary = build_category_summary(current_frame)
    previous_summary = build_category_summary(previous_frame)
    merged = current_summary.merge(
        previous_summary,
        on="category_name",
        how="outer",
        suffixes=("_current", "_previous"),
    ).fillna(0)
    merged["qty_change"] = merged["qty_current"] - merged["qty_previous"]
    merged["revenue_change"] = merged["revenue_current"] - merged["revenue_previous"]
    merged["orders_change"] = merged["orders_current"] - merged["orders_previous"]
    merged["customers_change"] = merged["customers_current"] - merged["customers_previous"]
    merged["customer_penetration_change"] = (
        merged["customer_penetration_current"] - merged["customer_penetration_previous"]
    )
    merged["revenue_change_pct"] = merged.apply(
        lambda row: 0.0 if row["revenue_previous"] == 0 else (row["revenue_change"] / row["revenue_previous"]) * 100,
        axis=1,
    )
    merged["qty_change_pct"] = merged.apply(
        lambda row: 0.0 if row["qty_previous"] == 0 else (row["qty_change"] / row["qty_previous"]) * 100,
        axis=1,
    )
    return merged.sort_values(["customers_change", "revenue_change"], ascending=[False, False])


def build_product_penetration_cards(comparison: pd.DataFrame) -> Dict[str, str]:
    if comparison.empty:
        return {
            "Top Revenue Driver": "N/A",
            "Top Revenue Drop": "N/A",
            "Top Qty Gainer": "N/A",
            "Top Penetration Gainer": "N/A",
        }

    top_revenue = comparison.sort_values("revenue_change", ascending=False).iloc[0]
    top_drop = comparison.sort_values("revenue_change", ascending=True).iloc[0]
    top_qty = comparison.sort_values("qty_change", ascending=False).iloc[0]
    top_pen = comparison.sort_values("order_penetration_change", ascending=False).iloc[0]
    return {
        "Top Revenue Driver": f"{top_revenue['product_name']} ({top_revenue['revenue_change']:,.0f})",
        "Top Revenue Drop": f"{top_drop['product_name']} ({top_drop['revenue_change']:,.0f})",
        "Top Qty Gainer": f"{top_qty['product_name']} ({top_qty['qty_change']:,.0f})",
        "Top Penetration Gainer": f"{top_pen['product_name']} ({top_pen['order_penetration_change']:.2f} pts)",
    }


def build_category_penetration_cards(comparison: pd.DataFrame) -> Dict[str, str]:
    if comparison.empty:
        return {
            "Top Customer Gainer": "N/A",
            "Top Customer Drop": "N/A",
            "Top Revenue Driver": "N/A",
            "Top Penetration Gainer": "N/A",
        }

    top_customers = comparison.sort_values("customers_change", ascending=False).iloc[0]
    top_drop = comparison.sort_values("customers_change", ascending=True).iloc[0]
    top_revenue = comparison.sort_values("revenue_change", ascending=False).iloc[0]
    top_pen = comparison.sort_values("customer_penetration_change", ascending=False).iloc[0]
    return {
        "Top Customer Gainer": f"{top_customers['category_name']} ({top_customers['customers_change']:,.0f})",
        "Top Customer Drop": f"{top_drop['category_name']} ({top_drop['customers_change']:,.0f})",
        "Top Revenue Driver": f"{top_revenue['category_name']} ({top_revenue['revenue_change']:,.0f})",
        "Top Penetration Gainer": f"{top_pen['category_name']} ({top_pen['customer_penetration_change']:.2f} pts)",
    }


def build_product_change_summary(comparison: pd.DataFrame) -> Dict[str, float]:
    if comparison.empty:
        return {
            "Products Increased": 0,
            "Products Decreased": 0,
            "New This Period": 0,
            "Dropped vs Last Month": 0,
        }

    increased = int((comparison["revenue_change"] > 0).sum())
    decreased = int((comparison["revenue_change"] < 0).sum())
    new_products = int(((comparison["revenue_current"] > 0) & (comparison["revenue_previous"] == 0)).sum())
    dropped_products = int(((comparison["revenue_current"] == 0) & (comparison["revenue_previous"] > 0)).sum())
    return {
        "Products Increased": increased,
        "Products Decreased": decreased,
        "New This Period": new_products,
        "Dropped vs Last Month": dropped_products,
    }


def build_category_change_summary(comparison: pd.DataFrame) -> Dict[str, float]:
    if comparison.empty:
        return {
            "Categories Increased": 0,
            "Categories Decreased": 0,
            "New This Period": 0,
            "Dropped vs Last Month": 0,
        }

    increased = int((comparison["customers_change"] > 0).sum())
    decreased = int((comparison["customers_change"] < 0).sum())
    new_categories = int(((comparison["customers_current"] > 0) & (comparison["customers_previous"] == 0)).sum())
    dropped_categories = int(((comparison["customers_current"] == 0) & (comparison["customers_previous"] > 0)).sum())
    return {
        "Categories Increased": increased,
        "Categories Decreased": decreased,
        "New This Period": new_categories,
        "Dropped vs Last Month": dropped_categories,
    }


def style_product_comparison_table(frame: pd.DataFrame):
    if frame.empty:
        return frame

    def change_bg(value: float) -> str:
        if value > 0:
            return "background-color: #e8f7ea; color: #146c2e;"
        if value < 0:
            return "background-color: #fdecec; color: #b42318;"
        return ""

    format_map = {
        "Current Qty": "{:,.0f}",
        "Previous Qty": "{:,.0f}",
        "Qty Change": "{:,.0f}",
        "Qty Change %": "{:,.2f}%",
        "Current Revenue": "{:,.2f}",
        "Previous Revenue": "{:,.2f}",
        "Revenue Change": "{:,.2f}",
        "Revenue Change %": "{:,.2f}%",
        "Current Orders": "{:,.0f}",
        "Previous Orders": "{:,.0f}",
        "Current Order Penetration %": "{:,.2f}%",
        "Previous Order Penetration %": "{:,.2f}%",
        "Order Penetration Change": "{:,.2f}",
    }

    return (
        frame.style.format(format_map)
        .map(change_bg, subset=["Qty Change", "Revenue Change", "Order Penetration Change"])
    )


def style_category_comparison_table(frame: pd.DataFrame):
    if frame.empty:
        return frame

    def change_bg(value: float) -> str:
        if value > 0:
            return "background-color: #e8f7ea; color: #146c2e;"
        if value < 0:
            return "background-color: #fdecec; color: #b42318;"
        return ""

    format_map = {
        "Current Qty": "{:,.0f}",
        "Previous Qty": "{:,.0f}",
        "Qty Change": "{:,.0f}",
        "Qty Change %": "{:,.2f}%",
        "Current Revenue": "{:,.2f}",
        "Previous Revenue": "{:,.2f}",
        "Revenue Change": "{:,.2f}",
        "Revenue Change %": "{:,.2f}%",
        "Current Orders": "{:,.0f}",
        "Previous Orders": "{:,.0f}",
        "Current Customers": "{:,.0f}",
        "Previous Customers": "{:,.0f}",
        "Customers Change": "{:,.0f}",
        "Current Customer Penetration %": "{:,.2f}%",
        "Previous Customer Penetration %": "{:,.2f}%",
        "Customer Penetration Change": "{:,.2f}",
    }

    return (
        frame.style.format(format_map)
        .map(change_bg, subset=["Qty Change", "Revenue Change", "Customers Change", "Customer Penetration Change"])
    )


def build_last_month_revenue_metrics(current_frame: pd.DataFrame, previous_frame: pd.DataFrame) -> pd.DataFrame:
    metrics: List[Dict[str, float]] = []

    def distinct_orders(frame: pd.DataFrame) -> int:
        order_keys = frame["order_id"].replace("", pd.NA)
        order_keys = order_keys.where(order_keys.notna(), pd.Series(frame.index.astype(str), index=frame.index))
        return int(order_keys.nunique())

    def distinct_customers(frame: pd.DataFrame) -> int:
        return int(frame.loc[frame["phone"].ne(WALKIN_PLACEHOLDER), "phone"].nunique())

    def walkin_count(frame: pd.DataFrame) -> int:
        return int(frame.loc[frame["walkin_customer_flag"], "phone"].count())

    metric_builders = [
        ("Revenue", lambda frame: float(frame["amount"].sum())),
        ("Distinct Order Count", distinct_orders),
        ("Distinct Customer Count", distinct_customers),
        ("Average Order Value", lambda frame: 0.0 if distinct_orders(frame) == 0 else float(frame["amount"].sum()) / distinct_orders(frame)),
        ("Repeat Customers", lambda frame: int(frame.loc[frame["repeat_customer_flag"], "phone"].nunique())),
        ("New Customers", lambda frame: int(frame.loc[frame["new_customer_flag"], "phone"].nunique())),
        ("Walkin Customers", walkin_count),
        ("Online Revenue", lambda frame: float(frame.loc[frame["is_online"], "amount"].sum())),
        ("Offline Revenue", lambda frame: float(frame.loc[~frame["is_online"], "amount"].sum())),
    ]

    for label, builder in metric_builders:
        current_value = float(builder(current_frame))
        previous_value = float(builder(previous_frame))
        change = current_value - previous_value
        change_pct = 0.0 if previous_value == 0 else (change / previous_value) * 100
        metrics.append(
            {
                "Metric": label,
                "Current": current_value,
                "Previous": previous_value,
                "Change": change,
                "Change %": change_pct,
                "Direction": "Increased" if change > 0 else ("Decreased" if change < 0 else "Flat"),
            }
        )

    return pd.DataFrame(metrics)


def build_last_month_revenue_cards(metrics_df: pd.DataFrame) -> Dict[str, str]:
    if metrics_df.empty:
        return {
            "Revenue Change": "0",
            "Orders Change": "0",
            "Customers Change": "0",
            "AOV Change": "0",
        }

    lookup = metrics_df.set_index("Metric")
    return {
        "Revenue Change": f"{lookup.loc['Revenue', 'Change']:,.2f}",
        "Orders Change": f"{lookup.loc['Distinct Order Count', 'Change']:,.0f}",
        "Customers Change": f"{lookup.loc['Distinct Customer Count', 'Change']:,.0f}",
        "AOV Change": f"{lookup.loc['Average Order Value', 'Change']:,.2f}",
    }


def build_last_month_revenue_description() -> None:
    st.caption(
        "This page compares your selected current period with the same shifted period last month. "
        "For example, April 1 to April 15 is compared against March 1 to March 15."
    )
    st.caption(
        "Distinct Order Count: unique orders in the period. "
        "Distinct Customer Count: unique non-walkin customers in the period. "
        "Average Order Value: revenue divided by distinct order count. "
        "Online and Offline Revenue are split using whether the order id contains 'online'."
    )


def style_revenue_comparison_table(frame: pd.DataFrame):
    if frame.empty:
        return frame

    def color_change(value: float) -> str:
        if value > 0:
            return "background-color: #e8f7ea; color: #146c2e;"
        if value < 0:
            return "background-color: #fdecec; color: #b42318;"
        return ""

    return (
        frame.style.format({"Current": "{:,.2f}", "Previous": "{:,.2f}", "Change": "{:,.2f}", "Change %": "{:,.2f}%"})
        .map(color_change, subset=["Change", "Change %"])
    )


def monthly_summary(frame: pd.DataFrame) -> pd.DataFrame:
    summary = (
        frame.groupby(["year", "month_label", "sales_month"], as_index=False)
        .agg(
            total_orders=("order_id", "nunique"),
            revenue=("amount", "sum"),
        )
        .sort_values(["sales_month"])
    )
    summary["AOV"] = (summary["revenue"] / summary["total_orders"]).round(2)
    summary["Revenue"] = summary["revenue"].round(2)
    summary["Month"] = summary["month_label"].str.split(" ").str[1:].str.join(" ")
    summary.rename(columns={"year": "Year", "total_orders": "Total Orders"}, inplace=True)
    return summary[["Year", "Month", "Total Orders", "AOV", "Revenue"]]


def current_month_distributions(frame: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if frame.empty:
        return pd.DataFrame(columns=["segment", "count"]), pd.DataFrame(columns=["channel", "revenue"])

    current_month = frame["sales_month"].max()
    month_frame = frame[frame["sales_month"] == current_month].copy()

    repeated = month_frame.loc[month_frame["repeat_customer_flag"], "phone"].nunique()
    new = month_frame.loc[month_frame["new_customer_flag"], "phone"].nunique()
    walkin = int(month_frame.loc[month_frame["walkin_customer_flag"], "phone"].count())

    user_distribution = pd.DataFrame(
        {
            "segment": ["Walkin", "Repeated", "New"],
            "count": [walkin, repeated, new],
        }
    )

    channel_distribution = (
        month_frame.groupby("channel", as_index=False)
        .agg(revenue=("amount", "sum"))
        .sort_values("revenue", ascending=False)
    )
    return user_distribution, channel_distribution


def segment_monthly_table(frame: pd.DataFrame, segment: str) -> pd.DataFrame:
    if segment == "repeated":
        base = frame[frame["repeat_customer_flag"]].copy()
    elif segment == "new":
        base = frame[frame["new_customer_flag"]].copy()
    elif segment == "walkin":
        base = frame[frame["walkin_customer_flag"]].copy()
    else:
        base = frame[frame["b2b_sales_flag"]].copy()

    if base.empty:
        return pd.DataFrame()

    grouped = (
        base.groupby(["year", "month_label", "sales_month"], as_index=False)
        .agg(
            Customer=("phone", "nunique" if segment != "walkin" else "count"),
            Orders=("order_id", "nunique"),
            Sales=("amount", "sum"),
        )
        .sort_values(["sales_month"])
    )
    grouped["AOV"] = (grouped["Sales"] / grouped["Orders"]).round(2)
    grouped["Year"] = grouped["year"]
    grouped["Month"] = grouped["month_label"].str.split(" ").str[1:].str.join(" ")

    if segment == "repeated":
        grouped["Freq"] = (grouped["Orders"] / grouped["Customer"]).round(2)
        grouped["Wallet Capture"] = (grouped["Sales"] / grouped["Customer"]).round(2)
        return grouped[["Year", "Month", "Customer", "Orders", "Sales", "AOV", "Freq", "Wallet Capture"]]

    if segment == "b2b":
        grouped.rename(columns={"Sales": "B2B sales"}, inplace=True)
        return grouped[["Year", "Month", "B2B sales"]]

    return grouped[["Year", "Month", "Customer", "Orders", "Sales", "AOV"]]


def build_cohort_count_matrix(
    frame: pd.DataFrame, column_name: str, base_column: int
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    cohort_frame = frame[frame[column_name] > -1].copy()
    if cohort_frame.empty:
        return pd.DataFrame(), pd.DataFrame()

    month_order = (
        cohort_frame[["cohort_month1", "cohort_month"]]
        .drop_duplicates()
        .sort_values("cohort_month")
    )
    pivot = pd.pivot_table(
        cohort_frame,
        index="cohort_month1",
        columns=column_name,
        values="phone",
        aggfunc=pd.Series.nunique,
        fill_value=0,
    )
    pivot = pivot.reindex(month_order["cohort_month1"].tolist())
    pivot.columns = [int(column) for column in pivot.columns]

    percentage = pivot.div(pivot.get(base_column, 0).replace(0, pd.NA), axis=0).fillna(0)

    total_row = pd.DataFrame([pivot.sum(axis=0)], index=["Total"])
    count_matrix = pd.concat([pivot, total_row])
    return count_matrix, percentage


def build_binrange_matrix(
    frame: pd.DataFrame, column_name: str, base_column: int
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    cohort_frame = frame[frame[column_name] > -1].copy()
    if cohort_frame.empty:
        return pd.DataFrame(), pd.DataFrame()

    month_order = (
        cohort_frame[["cohort_month1", "cohort_month"]]
        .drop_duplicates()
        .sort_values("cohort_month")
    )
    pivot = pd.pivot_table(
        cohort_frame,
        index=["cohort_month1", "bin_range"],
        columns=column_name,
        values="phone",
        aggfunc=pd.Series.nunique,
        fill_value=0,
    )
    desired_index = []
    for cohort in month_order["cohort_month1"].tolist():
        for bin_name in ["1-0–200", "2-201–400", "3-401–600", "4-600+"]:
            desired_index.append((cohort, bin_name))
    pivot = pivot.reindex(pd.MultiIndex.from_tuples(desired_index, names=["CohortMonth1", "BinRange"]))
    pivot = pivot.fillna(0)
    pivot.columns = [int(column) for column in pivot.columns]
    pivot["Total"] = pivot.sum(axis=1)

    percentage = pivot.drop(columns=["Total"]).div(
        pivot.get(base_column, 0).replace(0, pd.NA), axis=0
    ).fillna(0)

    total_row = pd.DataFrame([pivot.sum(axis=0)], index=pd.MultiIndex.from_tuples([("Total", "")]))
    count_matrix = pd.concat([pivot, total_row])
    return count_matrix, percentage


def build_revenue_repeat_matrices(frame: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    revenue_frame = frame[~frame["is_b2b"]].copy()
    if revenue_frame.empty:
        return pd.DataFrame(), pd.DataFrame()

    pivot = pd.pivot_table(
        revenue_frame,
        index="cohort_month1",
        columns="cohort_index",
        values="amount",
        aggfunc="sum",
        fill_value=0.0,
    )
    month_order = (
        revenue_frame[["cohort_month1", "cohort_month"]]
        .drop_duplicates()
        .sort_values("cohort_month")
    )
    pivot = pivot.reindex(month_order["cohort_month1"].tolist()).fillna(0.0)
    pivot.columns = [int(column) for column in pivot.columns]
    pivot["Total"] = pivot.sum(axis=1)
    percentage = pivot.drop(columns=["Total"]).div(
        pivot.get(1, 0).replace(0, pd.NA), axis=0
    ).fillna(0.0)
    total_row = pd.DataFrame([pivot.sum(axis=0)], index=["Total"])
    count_matrix = pd.concat([pivot, total_row])
    return count_matrix, percentage


def build_monthly_wallet_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    wallet_frame = frame[~frame["is_b2b"]].copy()
    wallet_frame = wallet_frame[wallet_frame["cohort_index"] >= 1]
    if wallet_frame.empty:
        return pd.DataFrame()

    grouped = (
        wallet_frame.groupby(["cohort_month1", "cohort_month", "cohort_index"], as_index=False)
        .agg(
            revenue_without_b2b=("amount", "sum"),
            total_customer=("phone", pd.Series.nunique),
        )
        .sort_values(["cohort_month", "cohort_index"])
    )
    grouped["monthly_wallet_capture"] = (
        grouped["revenue_without_b2b"] / grouped["total_customer"].replace(0, pd.NA)
    ).round(0).fillna(0)

    pivot = pd.pivot_table(
        grouped,
        index="cohort_month1",
        columns="cohort_index",
        values="monthly_wallet_capture",
        aggfunc="first",
        fill_value=0,
    )
    month_order = (
        grouped[["cohort_month1", "cohort_month"]]
        .drop_duplicates()
        .sort_values("cohort_month")["cohort_month1"]
        .tolist()
    )
    pivot = pivot.reindex(month_order).fillna(0)
    pivot.columns = [int(column) for column in pivot.columns]
    pivot["Total"] = pivot.sum(axis=1)
    total_row = pd.DataFrame([pivot.sum(axis=0)], index=["Total"])
    return pd.concat([pivot, total_row])


def build_revenue_repeat_cards(frame: pd.DataFrame) -> Dict[str, float]:
    revenue_frame = frame[~frame["is_b2b"]].copy()
    if revenue_frame.empty:
        return {
            "Last Month Sales": 0.0,
            "MTD": 0.0,
            "Last Month AOV": 0.0,
            "MTD AOV": 0.0,
            "Repeat Revenue": 0.0,
            "New Revenue": 0.0,
        }

    current_month = revenue_frame["sales_month"].max()
    last_month = current_month - pd.offsets.MonthBegin(1)
    current_frame = revenue_frame[revenue_frame["sales_month"] == current_month]
    last_frame = revenue_frame[revenue_frame["sales_month"] == last_month]

    current_non_walkin = current_frame[current_frame["cohort_index"] != -1]
    repeat_revenue = float(current_non_walkin[current_non_walkin["cohort_index"] != 1]["amount"].sum())
    new_revenue = float(current_non_walkin[current_non_walkin["cohort_index"] == 1]["amount"].sum())

    current_orders = max(current_frame["order_id"].replace("", pd.NA).nunique(), 1)
    last_orders = max(last_frame["order_id"].replace("", pd.NA).nunique(), 1)

    return {
        "Last Month Sales": float(last_frame["amount"].sum()),
        "MTD": float(current_frame["amount"].sum()),
        "Last Month AOV": float(last_frame["amount"].sum()) / last_orders if not last_frame.empty else 0.0,
        "MTD AOV": float(current_frame["amount"].sum()) / current_orders if not current_frame.empty else 0.0,
        "Repeat Revenue": repeat_revenue,
        "New Revenue": new_revenue,
    }


def build_revenue_monthwise_split(frame: pd.DataFrame) -> pd.DataFrame:
    revenue_frame = frame[~frame["is_b2b"]].copy()
    if revenue_frame.empty:
        return pd.DataFrame()

    revenue_frame["segment"] = revenue_frame["cohort_index"].apply(
        lambda value: "New" if value == 1 else ("Repeat" if value != -1 else "Walkin")
    )
    monthwise = (
        revenue_frame.groupby(["order_month_label", "sales_month", "segment"], as_index=False)
        .agg(revenue=("amount", "sum"))
        .sort_values("sales_month")
    )
    monthwise = monthwise[monthwise["segment"].isin(["Repeat", "New"])]
    totals = monthwise.groupby("order_month_label")["revenue"].transform("sum")
    monthwise["share"] = monthwise["revenue"] / totals
    return monthwise


def build_order_count_matrix(
    frame: pd.DataFrame, row_column: str, col_column: str, base_column: int = None
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    base = frame.copy()
    if row_column == "cohort_month1":
        order = (
            base[[row_column, "cohort_month"]]
            .drop_duplicates()
            .sort_values("cohort_month")[row_column]
            .tolist()
        )
    else:
        order = (
            base[[row_column, "order_month"]]
            .drop_duplicates()
            .sort_values("order_month")[row_column]
            .tolist()
        )

    order_keys = base["order_id"].replace("", pd.NA)
    order_keys = order_keys.where(order_keys.notna(), pd.Series(base.index.astype(str), index=base.index))
    base = base.assign(order_key=order_keys)

    pivot = pd.pivot_table(
        base,
        index=row_column,
        columns=col_column,
        values="order_key",
        aggfunc=pd.Series.nunique,
        fill_value=0,
    )
    pivot = pivot.reindex(order).fillna(0)
    pivot.columns = [int(column) for column in pivot.columns]
    total_row = pd.DataFrame([pivot.sum(axis=0)], index=["Total"])
    counts = pd.concat([pivot, total_row])

    percentages = pd.DataFrame()
    if base_column is not None and base_column in pivot.columns:
        percentages = pivot.div(pivot[base_column].replace(0, pd.NA), axis=0).fillna(0)
    return counts, percentages


def build_order_aov_distribution(frame: pd.DataFrame) -> pd.DataFrame:
    base = frame.copy()
    order_keys = base["order_id"].replace("", pd.NA)
    order_keys = order_keys.where(order_keys.notna(), pd.Series(base.index.astype(str), index=base.index))
    base = base.assign(order_key=order_keys)
    pivot = pd.pivot_table(
        base,
        index="order_month_label",
        columns="bin_range",
        values="order_key",
        aggfunc=pd.Series.nunique,
        fill_value=0,
    )
    order = (
        base[["order_month_label", "order_month"]]
        .drop_duplicates()
        .sort_values("order_month")["order_month_label"]
        .tolist()
    )
    pivot = pivot.reindex(order).fillna(0)
    for column in ["1-0–200", "2-201–400", "3-401–600", "4-600+"]:
        if column not in pivot.columns:
            pivot[column] = 0
    pivot = pivot[["1-0–200", "2-201–400", "3-401–600", "4-600+"]]
    pivot["Total"] = pivot.sum(axis=1)
    total_row = pd.DataFrame([pivot.sum(axis=0)], index=["Total"])
    return pd.concat([pivot, total_row])


def build_branchwise_summary(frame: pd.DataFrame) -> pd.DataFrame:
    base = frame[~frame["is_b2b"]].copy()
    if base.empty:
        return pd.DataFrame()
    grouped = (
        base.groupby(["year", "month_name", "sales_month", "branch_code"], as_index=False)
        .agg(
            sales=("amount", "sum"),
            total_orders=("order_id", "nunique"),
            total_customer=("phone", "nunique"),
        )
        .sort_values(["sales_month", "branch_code"])
    )
    grouped["AOV"] = (grouped["sales"] / grouped["total_orders"]).round(2)
    grouped.rename(
        columns={
            "year": "Year",
            "month_name": "Month",
            "branch_code": "Branch Code",
            "sales": "Sum of Amount",
            "total_orders": "Total Orders",
            "total_customer": "total customer",
        },
        inplace=True,
    )
    return grouped[["Year", "Month", "Branch Code", "Sum of Amount", "AOV", "Total Orders", "total customer"]]


def build_month_day_summary(frame: pd.DataFrame, month_value: pd.Timestamp) -> pd.DataFrame:
    month_frame = frame[(frame["sales_month"] == month_value) & (~frame["is_b2b"])].copy()
    if month_frame.empty:
        return pd.DataFrame()
    month_frame["Day"] = month_frame["sales_date"].dt.day
    grouped = (
        month_frame.groupby("Day", as_index=False)
        .agg(
            sales=("amount", "sum"),
            total_customer=("phone", "nunique"),
        )
    )
    grouped["Month"] = month_value.strftime("%B")
    grouped["New"] = (
        month_frame[month_frame["is_new_customer"].eq(1)]
        .groupby("Day")["phone"]
        .nunique()
        .reindex(grouped["Day"], fill_value=0)
        .values
    )
    grouped["online"] = (
        month_frame[month_frame["is_online"]]
        .groupby("Day")["phone"]
        .nunique()
        .reindex(grouped["Day"], fill_value=0)
        .values
    )
    grouped["New AOV"] = (
        month_frame[month_frame["is_new_customer"].eq(1)]
        .groupby("Day")["amount"]
        .mean()
        .reindex(grouped["Day"], fill_value=0)
        .round(2)
        .values
    )
    grouped["AOV"] = (grouped["sales"] / grouped["total_customer"]).round(2)
    grouped.rename(columns={"total_customer": "total customer"}, inplace=True)
    return grouped[["Month", "Day", "New", "online", "New AOV", "total customer", "AOV"]]


def build_order_rank_summary(frame: pd.DataFrame, month_value: pd.Timestamp) -> pd.DataFrame:
    month_frame = frame[(frame["sales_month"] == month_value) & (~frame["is_b2b"]) & (frame["order_rank"] > 0)].copy()
    rows = []
    for n in range(1, 11):
        users_at_n = month_frame.loc[month_frame["order_rank"] == n, "phone"].nunique()
        users_at_np1 = month_frame.loc[month_frame["order_rank"] == n + 1, "phone"].nunique()
        rows.append({"Value": n, "Users at N-": users_at_n, "Users at N+1": users_at_np1})
    table = pd.DataFrame(rows)
    total = pd.DataFrame([{"Value": "Total", "Users at N-": table["Users at N-"].iloc[0] if not table.empty else 0, "Users at N+1": ""}])
    return pd.concat([table, total], ignore_index=True)


def build_month_customer_summary(frame: pd.DataFrame) -> pd.DataFrame:
    base = frame[~frame["is_b2b"]].copy()
    if base.empty:
        return pd.DataFrame()
    grouped = (
        base.groupby(["year", "month_name", "sales_month"], as_index=False)
        .agg(
            total_customer=("phone", "nunique"),
            sales=("amount", "sum"),
            total_orders=("order_id", "nunique"),
        )
        .sort_values("sales_month")
    )
    grouped["New"] = (
        base[base["is_new_customer"].eq(1)]
        .groupby("sales_month")["phone"]
        .nunique()
        .reindex(grouped["sales_month"], fill_value=0)
        .values
    )
    grouped["AOV"] = (grouped["sales"] / grouped["total_orders"]).round(2)
    grouped.rename(columns={"year": "Year", "month_name": "Month"}, inplace=True)
    return grouped[["Year", "Month", "total_customer", "New", "AOV"]]


def build_tags_customer_spend(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    base = frame[
        (frame["sales_month"] == month_value)
        & (frame["nonwalkin"] == 1)
        & (~frame["is_b2b"])
    ].copy()
    if cutoff_day is not None:
        base = base[base["sales_day"] <= cutoff_day]
    if base.empty:
        return pd.DataFrame()

    grouped = (
        base.groupby("phone", as_index=False)
        .agg(spend=("amount", "sum"))
        .sort_values(["spend", "phone"], ascending=[False, True])
    )
    grouped["spend"] = grouped["spend"].round(2)

    def reached_gifts(total: float) -> str:
        unlocked = [gift_name for _, gift_name, _, milestone in GIFT_MILESTONES if total >= milestone]
        return ", ".join(unlocked) if unlocked else ""

    def next_milestone(total: float) -> str:
        for gift_label, gift_name, _, milestone in GIFT_MILESTONES:
            if total < milestone:
                return f"{gift_label} - {gift_name}"
        return "All Gifts Unlocked"

    def remaining_to_next(total: float) -> float:
        for _, _, _, milestone in GIFT_MILESTONES:
            if total < milestone:
                return round(milestone - total, 2)
        return 0.0

    grouped["Gifts Unlocked"] = grouped["spend"].apply(reached_gifts)
    grouped["Next Milestone"] = grouped["spend"].apply(next_milestone)
    grouped["Gifts To Be Unlock"] = grouped["spend"].apply(remaining_to_next)
    grouped["Date"] = cutoff_day.strftime("%Y-%m-%d") if cutoff_day is not None else month_value.strftime("%Y-%m")
    grouped.rename(columns={"phone": "Phone", "spend": "Spend"}, inplace=True)
    return grouped[["Date", "Phone", "Spend", "Gifts Unlocked", "Next Milestone", "Gifts To Be Unlock"]]


def build_tags_inventory(frame: pd.DataFrame, month_value: pd.Timestamp, cutoff_day: pd.Timestamp = None) -> pd.DataFrame:
    spend_frame = build_tags_customer_spend(frame, month_value, cutoff_day=cutoff_day)
    if spend_frame.empty:
        return pd.DataFrame()

    rows = []
    for gift_label, gift_name, tag_name, milestone in GIFT_MILESTONES:
        unlocked_count = int((spend_frame["Spend"] >= milestone).sum())
        rows.append(
            {
                "Gift": gift_label,
                "Gift Name": gift_name,
                "Milestone Tag": tag_name,
                "Milestone Spend": milestone,
                "Unlocked Count": unlocked_count,
            }
        )
    return pd.DataFrame(rows)


def build_tags_below_1000(frame: pd.DataFrame, month_value: pd.Timestamp, cutoff_day: pd.Timestamp = None) -> pd.DataFrame:
    spend_frame = build_tags_customer_spend(frame, month_value, cutoff_day=cutoff_day)
    if spend_frame.empty:
        return pd.DataFrame()

    below = spend_frame[spend_frame["Spend"] < 1000].copy()
    if below.empty:
        return pd.DataFrame()
    below["Remaining To 1000"] = (1000 - below["Spend"]).round(2)
    below["Snapshot Date"] = month_value.strftime("%Y-%m")
    below.insert(1, "Name", "")
    below.rename(columns={"Phone": "Phone Number"}, inplace=True)
    return below[["Phone Number", "Name", "Spend", "Remaining To 1000", "Snapshot Date"]]


def build_tags_chart_source(frame: pd.DataFrame, month_value: pd.Timestamp, cutoff_day: pd.Timestamp = None) -> pd.DataFrame:
    spend_frame = build_tags_customer_spend(frame, month_value, cutoff_day=cutoff_day)
    inventory = build_tags_inventory(frame, month_value, cutoff_day=cutoff_day)
    if spend_frame.empty or inventory.empty:
        return pd.DataFrame()

    milestone_counts = inventory[["Gift Name", "Unlocked Count"]].rename(
        columns={"Gift Name": "label", "Unlocked Count": "value"}
    )
    milestone_counts["chart"] = "Unlocked Count"

    slab_counts = pd.DataFrame(
        [
            {"label": "Below 1000", "value": int((spend_frame["Spend"] < 1000).sum()), "chart": "Customer Split"},
            {"label": "1000-1999", "value": int(((spend_frame["Spend"] >= 1000) & (spend_frame["Spend"] < 2000)).sum()), "chart": "Customer Split"},
            {"label": "2000-4999", "value": int(((spend_frame["Spend"] >= 2000) & (spend_frame["Spend"] < 5000)).sum()), "chart": "Customer Split"},
            {"label": "5000-9999", "value": int(((spend_frame["Spend"] >= 5000) & (spend_frame["Spend"] < 10000)).sum()), "chart": "Customer Split"},
            {"label": "10000+", "value": int((spend_frame["Spend"] >= 10000).sum()), "chart": "Customer Split"},
        ]
    )
    return pd.concat([milestone_counts, slab_counts], ignore_index=True)


def build_daily_tag_counts(frame: pd.DataFrame, month_value: pd.Timestamp, cutoff_day: pd.Timestamp = None) -> pd.DataFrame:
    daily_counts = build_monthly_tag_progression(frame, month_value, cutoff_day=cutoff_day)
    if daily_counts.empty:
        return pd.DataFrame()

    display = daily_counts[
        [
            "sales_day",
            "1000_tag contains",
            "2000_tag contains",
            "5000_tag contains",
            "10000_tag contains",
        ]
    ].copy()
    display.rename(columns={"sales_day": month_value.strftime("%B")}, inplace=True)
    display[month_value.strftime("%B")] = pd.to_datetime(display[month_value.strftime("%B")]).dt.strftime("%Y-%m-%d")
    return display


def build_monthly_tag_progression(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    base = frame[
        (frame["sales_month"] == month_value)
        & (frame["nonwalkin"] == 1)
        & (~frame["is_b2b"])
    ].copy()
    if cutoff_day is not None:
        base = base[base["sales_day"] <= cutoff_day]
    if base.empty:
        return pd.DataFrame()

    daily_phone_sales = (
        base.groupby(["sales_day", "phone"], as_index=False)
        .agg(spend=("amount", "sum"))
        .sort_values(["phone", "sales_day"])
    )
    all_days = pd.date_range(
        start=base["sales_day"].min(),
        end=base["sales_day"].max(),
        freq="D",
    )
    all_phones = daily_phone_sales["phone"].drop_duplicates().tolist()
    full_grid = pd.MultiIndex.from_product(
        [all_days, all_phones],
        names=["sales_day", "phone"],
    ).to_frame(index=False)

    daily_phone_sales = full_grid.merge(
        daily_phone_sales,
        on=["sales_day", "phone"],
        how="left",
    )
    daily_phone_sales["spend"] = daily_phone_sales["spend"].fillna(0.0)
    daily_phone_sales["cumulative_spend"] = daily_phone_sales.groupby("phone")["spend"].cumsum()

    daily_counts = (
        daily_phone_sales.groupby("sales_day", as_index=False)
        .agg(
            **{
                "1000_tag contains": ("cumulative_spend", lambda values: int((values >= 1000).sum())),
                "2000_tag contains": ("cumulative_spend", lambda values: int((values >= 2000).sum())),
                "5000_tag contains": ("cumulative_spend", lambda values: int((values >= 5000).sum())),
                "10000_tag contains": ("cumulative_spend", lambda values: int((values >= 10000).sum())),
            }
        )
        .sort_values("sales_day")
    )
    return daily_counts


def build_daily_tag_increments(frame: pd.DataFrame, month_value: pd.Timestamp, cutoff_day: pd.Timestamp = None) -> pd.DataFrame:
    progression = build_monthly_tag_progression(frame, month_value, cutoff_day=cutoff_day)
    if progression.empty:
        return pd.DataFrame()

    increments = progression.copy().sort_values("sales_day")
    tag_columns = ["1000_tag contains", "2000_tag contains", "5000_tag contains", "10000_tag contains"]
    increments[tag_columns] = increments[tag_columns].diff().fillna(increments[tag_columns]).clip(lower=0)
    return increments


def build_tag_day_comparison(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    current_cutoff_day: pd.Timestamp = None,
    previous_cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    current = build_monthly_tag_progression(frame, month_value, cutoff_day=current_cutoff_day)
    previous_month = month_value - pd.offsets.MonthBegin(1)
    previous = build_monthly_tag_progression(frame, previous_month, cutoff_day=previous_cutoff_day)
    if current.empty and previous.empty:
        return pd.DataFrame()

    current = current.copy()
    previous = previous.copy()
    current["Day"] = pd.to_datetime(current["sales_day"]).dt.day
    previous["Day"] = pd.to_datetime(previous["sales_day"]).dt.day

    current = current.drop(columns=["sales_day"]).rename(
        columns={
            "1000_tag contains": f"{month_value.strftime('%b %Y')} 1000",
            "2000_tag contains": f"{month_value.strftime('%b %Y')} 2000",
            "5000_tag contains": f"{month_value.strftime('%b %Y')} 5000",
            "10000_tag contains": f"{month_value.strftime('%b %Y')} 10000",
        }
    )
    previous = previous.drop(columns=["sales_day"]).rename(
        columns={
            "1000_tag contains": f"{previous_month.strftime('%b %Y')} 1000",
            "2000_tag contains": f"{previous_month.strftime('%b %Y')} 2000",
            "5000_tag contains": f"{previous_month.strftime('%b %Y')} 5000",
            "10000_tag contains": f"{previous_month.strftime('%b %Y')} 10000",
        }
    )
    return current.merge(previous, on="Day", how="outer").sort_values("Day").fillna(0)


def build_tag_week_comparison(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    current_cutoff_day: pd.Timestamp = None,
    previous_cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    current = build_monthly_tag_progression(frame, month_value, cutoff_day=current_cutoff_day)
    previous_month = month_value - pd.offsets.MonthBegin(1)
    previous = build_monthly_tag_progression(frame, previous_month, cutoff_day=previous_cutoff_day)
    if current.empty and previous.empty:
        return pd.DataFrame()

    def summarize(progress: pd.DataFrame, label: str) -> pd.DataFrame:
        if progress.empty:
            return pd.DataFrame(columns=["Week"])
        summary = progress.copy()
        summary["Week"] = ((pd.to_datetime(summary["sales_day"]).dt.day - 1) // 7) + 1
        summary = (
            summary.groupby("Week", as_index=False)
            .agg(
                **{
                    f"{label} 1000": ("1000_tag contains", "max"),
                    f"{label} 2000": ("2000_tag contains", "max"),
                    f"{label} 5000": ("5000_tag contains", "max"),
                    f"{label} 10000": ("10000_tag contains", "max"),
                }
            )
        )
        return summary

    current_summary = summarize(current, month_value.strftime("%b %Y"))
    previous_summary = summarize(previous, previous_month.strftime("%b %Y"))
    return current_summary.merge(previous_summary, on="Week", how="outer").sort_values("Week").fillna(0)


def build_tag_daytype_comparison(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    current_cutoff_day: pd.Timestamp = None,
    previous_cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    current = build_monthly_tag_progression(frame, month_value, cutoff_day=current_cutoff_day)
    previous_month = month_value - pd.offsets.MonthBegin(1)
    previous = build_monthly_tag_progression(frame, previous_month, cutoff_day=previous_cutoff_day)
    if current.empty and previous.empty:
        return pd.DataFrame()

    def summarize(progress: pd.DataFrame, label: str) -> pd.DataFrame:
        if progress.empty:
            return pd.DataFrame(columns=["Segment"])
        summary = progress.copy()
        summary["Segment"] = pd.to_datetime(summary["sales_day"]).dt.dayofweek.map(
            lambda value: "Weekend" if value >= 5 else "Weekday"
        )
        summary = (
            summary.groupby("Segment", as_index=False)
            .agg(
                **{
                    f"{label} 1000": ("1000_tag contains", "max"),
                    f"{label} 2000": ("2000_tag contains", "max"),
                    f"{label} 5000": ("5000_tag contains", "max"),
                    f"{label} 10000": ("10000_tag contains", "max"),
                }
            )
        )
        return summary

    current_summary = summarize(current, month_value.strftime("%b %Y"))
    previous_summary = summarize(previous, previous_month.strftime("%b %Y"))
    merged = current_summary.merge(previous_summary, on="Segment", how="outer").fillna(0)
    segment_order = pd.Categorical(merged["Segment"], categories=["Weekday", "Weekend"], ordered=True)
    return merged.assign(_segment_order=segment_order).sort_values("_segment_order").drop(columns=["_segment_order"])


def build_tag_change_summary(
    frame: pd.DataFrame,
    month_value: pd.Timestamp,
    current_cutoff_day: pd.Timestamp = None,
    previous_cutoff_day: pd.Timestamp = None,
) -> pd.DataFrame:
    current = build_monthly_tag_progression(frame, month_value, cutoff_day=current_cutoff_day)
    previous_month = month_value - pd.offsets.MonthBegin(1)
    previous = build_monthly_tag_progression(frame, previous_month, cutoff_day=previous_cutoff_day)

    tag_columns = [
        ("1000_tag contains", "1000 Tag"),
        ("2000_tag contains", "2000 Tag"),
        ("5000_tag contains", "5000 Tag"),
        ("10000_tag contains", "10000 Tag"),
    ]

    current_last = current.iloc[-1] if not current.empty else pd.Series(dtype="object")
    previous_last = previous.iloc[-1] if not previous.empty else pd.Series(dtype="object")
    rows = []
    for column_name, label in tag_columns:
        current_value = float(current_last.get(column_name, 0) or 0)
        previous_value = float(previous_last.get(column_name, 0) or 0)
        change = current_value - previous_value
        if previous_value == 0:
            pct_change = 100.0 if current_value > 0 else 0.0
        else:
            pct_change = (change / previous_value) * 100.0
        direction = "Increased" if change > 0 else ("Decreased" if change < 0 else "No Change")
        rows.append(
            {
                "Tag": label,
                f"{month_value.strftime('%b %Y')}": int(current_value),
                f"{previous_month.strftime('%b %Y')}": int(previous_value),
                "Change": int(change),
                "Change %": round(pct_change, 2),
                "Direction": direction,
            }
        )
    return pd.DataFrame(rows)


def build_tag_change_summary_from_values(
    current_values: Dict[str, float],
    previous_values: Dict[str, float],
    current_label: str,
    previous_label: str,
) -> pd.DataFrame:
    rows = []
    for key, label in [
        ("1000", "1000 Tag"),
        ("2000", "2000 Tag"),
        ("5000", "5000 Tag"),
        ("10000", "10000 Tag"),
    ]:
        current_value = float(current_values.get(key, 0) or 0)
        previous_value = float(previous_values.get(key, 0) or 0)
        change = current_value - previous_value
        if previous_value == 0:
            pct_change = 100.0 if current_value > 0 else 0.0
        else:
            pct_change = (change / previous_value) * 100.0
        rows.append(
            {
                "Tag": label,
                current_label: int(current_value),
                previous_label: int(previous_value),
                "Change": int(change),
                "Change %": round(pct_change, 2),
                "Direction": "Increased" if change > 0 else ("Decreased" if change < 0 else "No Change"),
            }
        )
    return pd.DataFrame(rows)


def render_tag_change_summary(summary_df: pd.DataFrame, current_label: str, previous_label: str) -> None:
    if summary_df.empty:
        st.info("No tag change summary found.")
        return

    change_metric_cols = st.columns(4)
    for column, row in zip(change_metric_cols, summary_df.to_dict("records")):
        delta_prefix = "+" if row["Change %"] > 0 else ""
        delta_text = f"{delta_prefix}{row['Change %']:.2f}%"
        column.metric(
            row["Tag"],
            f"{row[current_label]:,} vs {row[previous_label]:,}",
            delta_text,
        )
    st.dataframe(summary_df, use_container_width=True, hide_index=True)


def style_count_matrix(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame.style.background_gradient(cmap="Greens").format(precision=0)


def style_currency_matrix(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame.style.background_gradient(cmap="Greens").format("{:,.2f}")


def style_percentage_matrix(frame: pd.DataFrame):
    if frame.empty:
        return frame
    return frame.style.background_gradient(cmap="Greens").format("{:.0%}")


def start_of_week(day_value: date) -> date:
    return day_value - timedelta(days=day_value.weekday())


def previous_month_window(day_value: date) -> Tuple[date, date]:
    first_this_month = day_value.replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    first_prev_month = last_prev_month.replace(day=1)
    return first_prev_month, last_prev_month


def add_months(day_value: date, months: int) -> date:
    year = day_value.year + ((day_value.month - 1 + months) // 12)
    month = ((day_value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def apply_quick_range(choice: str, today_value: date) -> Tuple[date, date]:
    if choice == "Today":
        return today_value, today_value
    if choice == "Yesterday":
        day_value = today_value - timedelta(days=1)
        return day_value, day_value
    if choice == "This Week":
        return start_of_week(today_value), today_value
    if choice == "This Month":
        return today_value.replace(day=1), today_value
    if choice == "Last Month":
        return previous_month_window(today_value)
    if choice == "Last 3 Months":
        return add_months(today_value.replace(day=1), -2), today_value
    return today_value, today_value


st.set_page_config(page_title="MIS Reporting Dashboard", layout="wide")
st.title("MIS Reporting Dashboard")
st.caption("Live historical + latest cleaned sales from Supabase")

refresh_col, rebuild_col, status_col = st.columns([0.18, 0.22, 0.60])
with refresh_col:
    if st.button("Refresh till now", use_container_width=True):
        with st.spinner("Refreshing latest data..."):
            refresh_args = ("--full-refresh",) if not local_db_has_sales_rows() else ()
            ok, message = run_pipeline_command(*refresh_args)
        st.cache_data.clear()
        if ok:
            st.success("Dashboard updated till now.")
        else:
            st.error("Refresh failed.")
        if message:
            st.code(message)

with rebuild_col:
    if st.button("Rebuild Yesterday", use_container_width=True):
        with st.spinner("Deleting and rebuilding yesterday data..."):
            ok, message = run_pipeline_command("--rebuild-yesterday")
        st.cache_data.clear()
        if ok:
            st.success("Yesterday data rebuilt.")
        else:
            st.error("Yesterday rebuild failed.")
        if message:
            st.code(message)

if not check_local_db_ready():
    st.info(
        "Supabase backup tables are not reachable yet. "
        "Run `python sales_pipeline.py --target-sync-from-local --truncate-target-first` and verify keys."
    )
    st.stop()

all_sales_df = load_sales_data()
if all_sales_df.empty:
    st.info("No rows found in Supabase historical data.")
    st.stop()

default_end = all_sales_df["sales_day"].max().date()
default_start = all_sales_df["sales_day"].min().date()
today_value = default_end

if "filter_from" not in st.session_state:
    st.session_state.filter_from = default_start
if "filter_to" not in st.session_state:
    st.session_state.filter_to = default_end
if "month_filter" not in st.session_state:
    st.session_state.month_filter = "custom"

quick_cols = st.columns(6)
for col, label in zip(
    quick_cols,
    ["Today", "Yesterday", "This Week", "This Month", "Last Month", "Last 3 Months"],
):
    with col:
        if st.button(label, key="quick_" + label.replace(" ", "_"), use_container_width=True):
            start_date, end_date = apply_quick_range(label, today_value)
            st.session_state.filter_from = start_date
            st.session_state.filter_to = end_date
            st.session_state.month_filter = "custom"

recent_months = (
    all_sales_df[["sales_month", "year_month_label"]]
    .drop_duplicates()
    .sort_values("sales_month", ascending=False)
    .head(12)
)
st.caption(
    "Months: " + "  ·  ".join(recent_months["year_month_label"].tolist())
)

filter_cols = st.columns([1.1, 1.2, 1.2, 1.3, 1.1])
month_options = ["custom"] + recent_months["year_month_label"].tolist()
selected_month_option = filter_cols[0].selectbox(
    "Month",
    month_options,
    index=month_options.index(st.session_state.month_filter) if st.session_state.month_filter in month_options else 0,
)
if selected_month_option != "custom":
    selected_month_row = recent_months[recent_months["year_month_label"] == selected_month_option].iloc[0]
    month_start = selected_month_row["sales_month"].date()
    month_end = (selected_month_row["sales_month"] + pd.offsets.MonthEnd(1)).date()
    st.session_state.filter_from = month_start
    st.session_state.filter_to = month_end
st.session_state.month_filter = selected_month_option

start_date = filter_cols[1].date_input(
    "From",
    value=st.session_state.filter_from,
    min_value=default_start,
    max_value=default_end,
    key="from_date_input",
)
end_date = filter_cols[2].date_input(
    "To",
    value=st.session_state.filter_to,
    min_value=default_start,
    max_value=default_end,
    key="to_date_input",
)
st.session_state.filter_from = start_date
st.session_state.filter_to = end_date

branches = ["All Branches"] + sorted(all_sales_df["branch_code"].dropna().unique().tolist())
selected_branch = filter_cols[3].selectbox("Branch", branches, index=0)
types = ["All Types", "walkin", "delivery", "Unspecified"]
selected_type = filter_cols[4].selectbox("Type", types, index=0)

filtered_df = all_sales_df[
    (all_sales_df["sales_day"].dt.date >= start_date)
    & (all_sales_df["sales_day"].dt.date <= end_date)
]
if selected_branch != "All Branches":
    filtered_df = filtered_df[filtered_df["branch_code"] == selected_branch]
    branch_filtered_df = all_sales_df[all_sales_df["branch_code"] == selected_branch].copy()
else:
    branch_filtered_df = all_sales_df.copy()

if selected_type != "All Types":
    type_value = "" if selected_type == "Unspecified" else selected_type
    filtered_df = filtered_df[filtered_df["order_type"] == type_value]
    branch_filtered_df = branch_filtered_df[branch_filtered_df["order_type"] == type_value]

st.markdown(
    """
    <style>
    div[role="radiogroup"] {
        flex-direction: row !important;
        gap: 0.75rem;
        flex-wrap: wrap;
    }
    div[role="radiogroup"] label {
        padding: 0.35rem 0.8rem;
        border-radius: 999px;
        border: 1px solid rgba(49, 51, 63, 0.2);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

page_options = [
    "Summary",
    "Repeated 30",
    "Repeated Customer45/90",
    "Repeat Customer L60",
    "Revenue Repeat",
    "Revenue vs Last Month",
    "Product Penetration",
    "Persona",
    "Churn Dashboard",
    "Monthly Wallet",
    "Customer Count Branch Wise",
    "Order Repeat",
    "Tags",
]
selected_page = st.radio("Header navigation", page_options, horizontal=True, label_visibility="collapsed")

if selected_page == "Summary":
    measures = build_measure_cards(filtered_df)
    metric_columns = st.columns(5)
    for column, (label, value) in zip(metric_columns, measures.items()):
        if isinstance(value, float):
            column.metric(label, f"{value:,.2f}")
        else:
            column.metric(label, f"{value:,}")

    if filtered_df.empty:
        st.info("No rows found for the selected filters.")
        st.stop()

    top_left, top_mid, top_right = st.columns([1.1, 1.2, 1.2])

    with top_left:
        st.subheader("MOM Summary")
        st.dataframe(monthly_summary(filtered_df), use_container_width=True, hide_index=True)

    user_distribution, channel_distribution = current_month_distributions(branch_filtered_df)

    with top_mid:
        st.subheader("Current Month User Distribution")
        user_chart = px.pie(user_distribution, names="segment", values="count")
        st.plotly_chart(user_chart, use_container_width=True)

    with top_right:
        st.subheader("Current Online/Offline Revenue Distribution")
        channel_chart = px.pie(channel_distribution, names="channel", values="revenue")
        st.plotly_chart(channel_chart, use_container_width=True)

    bottom_left, bottom_mid, bottom_right, bottom_far = st.columns(4)

    with bottom_left:
        st.subheader("Repeated Customer")
        repeated_table = segment_monthly_table(filtered_df, "repeated")
        st.dataframe(repeated_table, use_container_width=True, hide_index=True)

    with bottom_mid:
        st.subheader("New Customer")
        new_table = segment_monthly_table(filtered_df, "new")
        st.dataframe(new_table, use_container_width=True, hide_index=True)

    with bottom_right:
        st.subheader("Walkin Customer")
        walkin_table = segment_monthly_table(filtered_df, "walkin")
        st.dataframe(walkin_table, use_container_width=True, hide_index=True)

    with bottom_far:
        st.subheader("B2B Sales")
        b2b_table = segment_monthly_table(filtered_df, "b2b")
        st.dataframe(b2b_table, use_container_width=True, hide_index=True)

if selected_page == "Repeated 30":
    cohort_source = branch_filtered_df.copy()
    mom_counts, mom_percentages = build_cohort_count_matrix(cohort_source, "cohort_index", 1)
    repeat_30_counts, repeat_30_percentages = build_cohort_count_matrix(cohort_source, "bin_value_30", 0)

    top_left, top_right = st.columns(2)
    bottom_left, bottom_right = st.columns(2)

    with top_left:
        st.subheader("MOM Repeat Cohort")
        if mom_counts.empty:
            st.info("No cohort rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(mom_counts), use_container_width=True)

    with top_right:
        st.subheader("L0-30 Repeat")
        if repeat_30_counts.empty:
            st.info("No 30-day repeat rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(repeat_30_counts), use_container_width=True)

    with bottom_left:
        st.subheader("MOM Repeat Cohort %")
        if mom_percentages.empty:
            st.info("No cohort percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(mom_percentages), use_container_width=True)

    with bottom_right:
        st.subheader("L0-30 Repeat %")
        if repeat_30_percentages.empty:
            st.info("No 30-day repeat percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(repeat_30_percentages), use_container_width=True)

if selected_page == "Repeated Customer45/90":
    cohort_source = branch_filtered_df.copy()
    repeat_45_counts, repeat_45_percentages = build_cohort_count_matrix(cohort_source, "bin_value_45", 0)
    repeat_90_counts, repeat_90_percentages = build_cohort_count_matrix(cohort_source, "bin_value_90", 0)

    top_left, top_right = st.columns(2)
    bottom_left, bottom_right = st.columns(2)

    with top_left:
        st.subheader("L45 CS Repeat %")
        if repeat_45_percentages.empty:
            st.info("No 45-day repeat percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(repeat_45_percentages), use_container_width=True)

    with top_right:
        st.subheader("L90 CS Repeat")
        if repeat_90_counts.empty:
            st.info("No 90-day repeat rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(repeat_90_counts), use_container_width=True)

    with bottom_left:
        st.subheader("L45 CS Repeat")
        if repeat_45_counts.empty:
            st.info("No 45-day repeat rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(repeat_45_counts), use_container_width=True)

    with bottom_right:
        st.subheader("L90 CS Repeat %")
        if repeat_90_percentages.empty:
            st.info("No 90-day repeat percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(repeat_90_percentages), use_container_width=True)

if selected_page == "Repeat Customer L60":
    cohort_source = branch_filtered_df.copy()
    repeat_60_counts, repeat_60_percentages = build_cohort_count_matrix(cohort_source, "bin_value_60", 0)
    binrange_60_counts, binrange_60_percentages = build_binrange_matrix(cohort_source, "bin_value_60", 0)

    top_left, top_right = st.columns(2)
    bottom_left, bottom_right = st.columns(2)

    with top_left:
        st.subheader("L60 Repeat")
        if repeat_60_counts.empty:
            st.info("No 60-day repeat rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(repeat_60_counts), use_container_width=True)

    with top_right:
        st.subheader("L60 by Bin Range")
        if binrange_60_counts.empty:
            st.info("No 60-day bin range rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(binrange_60_counts), use_container_width=True)

    with bottom_left:
        st.subheader("L60 Repeat %")
        if repeat_60_percentages.empty:
            st.info("No 60-day repeat percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(repeat_60_percentages), use_container_width=True)

    with bottom_right:
        st.subheader("L60 by Bin Range %")
        if binrange_60_percentages.empty:
            st.info("No 60-day bin range percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(binrange_60_percentages), use_container_width=True)

if selected_page == "Revenue Repeat":
    revenue_source = branch_filtered_df.copy()
    revenue_counts, revenue_percentages = build_revenue_repeat_matrices(revenue_source)
    revenue_cards = build_revenue_repeat_cards(revenue_source)
    monthwise_split = build_revenue_monthwise_split(revenue_source)

    top_left, top_right = st.columns([2.2, 1.3])

    with top_left:
        st.subheader("MOM Revenue Repeat")
        if revenue_counts.empty:
            st.info("No revenue repeat rows found for the selected branches.")
        else:
            st.dataframe(style_count_matrix(revenue_counts), use_container_width=True)

    with top_right:
        card_cols = st.columns(2)
        card_items = list(revenue_cards.items())
        for idx, (label, value) in enumerate(card_items):
            target = card_cols[idx % 2]
            target.metric(label, f"{value:,.2f}")

    bottom_left, bottom_right = st.columns([2.2, 1.3])

    with bottom_left:
        st.subheader("MOM% Revenue")
        if revenue_percentages.empty:
            st.info("No revenue percentage rows found for the selected branches.")
        else:
            st.dataframe(style_percentage_matrix(revenue_percentages), use_container_width=True)

    with bottom_right:
        st.subheader("Sales by month wise")
        if monthwise_split.empty:
            st.info("No monthwise revenue rows found for the selected branches.")
        else:
            split_chart = px.bar(
                monthwise_split,
                x="order_month_label",
                y="share",
                color="segment",
                barmode="stack",
                color_discrete_map={"Repeat": "#69a83b", "New": "#2f3a75"},
            )
            split_chart.update_layout(yaxis_tickformat=".0%", xaxis_title=None, yaxis_title=None)
            st.plotly_chart(split_chart, use_container_width=True)

if selected_page == "Revenue vs Last Month":
    current_revenue_frame = filtered_df.copy()
    previous_start = shift_date_one_month(start_date)
    previous_end = shift_date_one_month(end_date)
    previous_revenue_frame = branch_filtered_df[
        (branch_filtered_df["sales_day"].dt.date >= previous_start)
        & (branch_filtered_df["sales_day"].dt.date <= previous_end)
    ].copy()

    revenue_metrics = build_last_month_revenue_metrics(current_revenue_frame, previous_revenue_frame)
    revenue_cards = build_last_month_revenue_cards(revenue_metrics)

    st.subheader("Revenue vs Last Month")
    st.caption(
        f"Current period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} | "
        f"Last month period: {previous_start.strftime('%Y-%m-%d')} to {previous_end.strftime('%Y-%m-%d')}"
    )
    build_last_month_revenue_description()

    card_cols = st.columns(4)
    for column, (label, value) in zip(card_cols, revenue_cards.items()):
        column.metric(label, value)

    top_left, top_right = st.columns([1.4, 1.1])
    with top_left:
        st.markdown("**Metric Comparison Table**")
        st.dataframe(style_revenue_comparison_table(revenue_metrics), use_container_width=True, hide_index=True)

    with top_right:
        st.markdown("**Biggest Changes**")
        chart_source = revenue_metrics.copy()
        biggest_chart = px.bar(
            chart_source.sort_values("Change"),
            x="Change",
            y="Metric",
            orientation="h",
            color="Change",
            color_continuous_scale="RdYlGn",
        )
        biggest_chart.update_layout(xaxis_title="Change", yaxis_title=None, coloraxis_showscale=False)
        st.plotly_chart(biggest_chart, use_container_width=True)

    bottom_left, bottom_right = st.columns(2)
    with bottom_left:
        st.markdown("**Current vs Last Month Revenue Mix**")
        mix_source = pd.DataFrame(
            {
                "Period": ["Current", "Current", "Last Month", "Last Month"],
                "Channel": ["Online", "Offline", "Online", "Offline"],
                "Revenue": [
                    float(current_revenue_frame.loc[current_revenue_frame["is_online"], "amount"].sum()),
                    float(current_revenue_frame.loc[~current_revenue_frame["is_online"], "amount"].sum()),
                    float(previous_revenue_frame.loc[previous_revenue_frame["is_online"], "amount"].sum()),
                    float(previous_revenue_frame.loc[~previous_revenue_frame["is_online"], "amount"].sum()),
                ],
            }
        )
        mix_chart = px.bar(
            mix_source,
            x="Period",
            y="Revenue",
            color="Channel",
            barmode="group",
            color_discrete_map={"Online": "#1f77b4", "Offline": "#8ec9ff"},
        )
        mix_chart.update_layout(xaxis_title=None, yaxis_title=None)
        st.plotly_chart(mix_chart, use_container_width=True)

    with bottom_right:
        st.markdown("**Customer Mix Comparison**")
        customer_mix = pd.DataFrame(
            {
                "Segment": ["Repeat", "New", "Walkin", "Repeat", "New", "Walkin"],
                "Count": [
                    int(current_revenue_frame.loc[current_revenue_frame["repeat_customer_flag"], "phone"].nunique()),
                    int(current_revenue_frame.loc[current_revenue_frame["new_customer_flag"], "phone"].nunique()),
                    int(current_revenue_frame.loc[current_revenue_frame["walkin_customer_flag"], "phone"].count()),
                    int(previous_revenue_frame.loc[previous_revenue_frame["repeat_customer_flag"], "phone"].nunique()),
                    int(previous_revenue_frame.loc[previous_revenue_frame["new_customer_flag"], "phone"].nunique()),
                    int(previous_revenue_frame.loc[previous_revenue_frame["walkin_customer_flag"], "phone"].count()),
                ],
                "Period": ["Current", "Current", "Current", "Last Month", "Last Month", "Last Month"],
            }
        )
        customer_chart = px.bar(
            customer_mix,
            x="Segment",
            y="Count",
            color="Period",
            barmode="group",
            color_discrete_map={"Current": "#69a83b", "Last Month": "#2f3a75"},
        )
        customer_chart.update_layout(xaxis_title=None, yaxis_title=None)
        st.plotly_chart(customer_chart, use_container_width=True)

if selected_page == "Product Penetration":
    product_query_start = min(start_date, shift_date_one_month(start_date)).strftime("%Y-%m-%d 00:00:00")
    product_query_end = max(end_date, shift_date_one_month(end_date)).strftime("%Y-%m-%d 23:59:59")
    product_df = load_product_penetration_data(
        product_query_start,
        product_query_end,
        selected_branch,
        selected_type,
    )
    if product_df.empty:
        st.info("No joined product rows found.")
    else:
        current_start = start_date
        current_end = end_date
        previous_start = shift_date_one_month(current_start)
        previous_end = shift_date_one_month(current_end)

        product_filtered = product_df[
            (product_df["sales_day"].dt.date >= current_start)
            & (product_df["sales_day"].dt.date <= current_end)
        ].copy()
        product_previous = product_df[
            (product_df["sales_day"].dt.date >= previous_start)
            & (product_df["sales_day"].dt.date <= previous_end)
        ].copy()

        st.subheader("Product Penetration")
        st.caption(
            "Current period: "
            f"{current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')} | "
            "Last month comparison: "
            f"{previous_start.strftime('%Y-%m-%d')} to {previous_end.strftime('%Y-%m-%d')}"
        )
        st.caption(
            "This means if your filter is April 1 to April 15, it compares exactly against March 1 to March 15."
        )
        customer_filter_options = [
            "All Customers",
            "Exclude New Customers",
            "Exclude One-Time Customers",
        ]
        selected_customer_filter = st.selectbox(
            "Customer filter",
            customer_filter_options,
            index=0,
            key="penetration_customer_filter",
        )

        if selected_customer_filter == "Exclude New Customers":
            product_filtered = product_filtered[~product_filtered["is_new_customer"]].copy()
            product_previous = product_previous[~product_previous["is_new_customer"]].copy()
        elif selected_customer_filter == "Exclude One-Time Customers":
            product_filtered = product_filtered[~product_filtered["is_one_time_customer"]].copy()
            product_previous = product_previous[~product_previous["is_one_time_customer"]].copy()

        product_tab, category_tab = st.tabs(["Product Level", "Category Level"])

        with product_tab:
            product_current = product_filtered.copy()
            product_last_month = product_previous.copy()
            product_options = ["All Products"] + sorted(
                set(product_current["product_name"].dropna().astype(str).tolist())
                | set(product_last_month["product_name"].dropna().astype(str).tolist())
            )
            selected_product = st.selectbox(
                "Product Name",
                product_options,
                index=0,
                key="product_penetration_filter",
            )
            if selected_product != "All Products":
                product_current = product_current[product_current["product_name"] == selected_product]
                product_last_month = product_last_month[product_last_month["product_name"] == selected_product]

            product_comparison = compare_product_summary(product_current, product_last_month)
            product_cards = build_product_penetration_cards(product_comparison)
            product_change_summary = build_product_change_summary(product_comparison)

            card_cols = st.columns(4)
            for column, (label, value) in zip(card_cols, product_cards.items()):
                column.metric(label, value)

            if product_comparison.empty:
                st.info("No product rows found for the selected filters.")
            else:
                summary_cols = st.columns(4)
                for column, (label, value) in zip(summary_cols, product_change_summary.items()):
                    column.metric(label, f"{value:,}")

                sort_labels = {
                    "Revenue Increase": "revenue_change",
                    "Revenue Decrease": "revenue_change",
                    "Qty Increase": "qty_change",
                    "Penetration Increase": "order_penetration_change",
                }
                selected_sort = st.selectbox(
                    "Highlight movement by",
                    list(sort_labels.keys()),
                    index=0,
                    key="product_penetration_sort",
                )
                st.caption(
                    "Revenue Increase: shows products that added the most revenue vs last month at the top.\n"
                    "Revenue Decrease: shows products that lost the most revenue vs last month at the top.\n"
                    "Qty Increase: shows products whose sold quantity increased the most vs last month.\n"
                    "Penetration Increase: shows products whose penetration improved the most."
                )
                sort_column = sort_labels[selected_sort]
                ascending = selected_sort == "Revenue Decrease"
                display_comparison = product_comparison.sort_values(sort_column, ascending=ascending).copy()

                order_current = int(product_current["sales_no"].replace("", pd.NA).nunique())
                order_previous = int(product_last_month["sales_no"].replace("", pd.NA).nunique())
                order_cols = st.columns(2)
                order_cols[0].metric("Current Distinct Order Count", f"{order_current:,}")
                order_cols[1].metric("Previous Distinct Order Count", f"{order_previous:,}")

                top_left, top_right = st.columns(2)
                with top_left:
                    st.markdown("**Top Revenue Movers**")
                    revenue_chart_source = product_comparison.reindex(
                        product_comparison["revenue_change"].abs().sort_values(ascending=False).index
                    ).head(12)
                    revenue_chart = px.bar(
                        revenue_chart_source.sort_values("revenue_change"),
                        x="revenue_change",
                        y="product_name",
                        orientation="h",
                        color="revenue_change",
                        color_continuous_scale="RdYlGn",
                    )
                    revenue_chart.update_layout(xaxis_title="Revenue Change", yaxis_title=None, coloraxis_showscale=False)
                    st.plotly_chart(revenue_chart, use_container_width=True)

                with top_right:
                    st.markdown("**Top Quantity Movers**")
                    qty_chart_source = product_comparison.reindex(
                        product_comparison["qty_change"].abs().sort_values(ascending=False).index
                    ).head(12)
                    qty_chart = px.bar(
                        qty_chart_source.sort_values("qty_change"),
                        x="qty_change",
                        y="product_name",
                        orientation="h",
                        color="qty_change",
                        color_continuous_scale="RdYlGn",
                    )
                    qty_chart.update_layout(xaxis_title="Qty Change", yaxis_title=None, coloraxis_showscale=False)
                    st.plotly_chart(qty_chart, use_container_width=True)

                lower_left, lower_right = st.columns([1.4, 1.0])
                with lower_left:
                    st.markdown("**Product Comparison Table**")
                    table_view = display_comparison.rename(
                        columns={
                            "product_name": "Product Name",
                            "qty_current": "Current Qty",
                            "qty_previous": "Previous Qty",
                            "qty_change": "Qty Change",
                            "qty_change_pct": "Qty Change %",
                            "revenue_current": "Current Revenue",
                            "revenue_previous": "Previous Revenue",
                            "revenue_change": "Revenue Change",
                            "revenue_change_pct": "Revenue Change %",
                            "orders_current": "Current Orders",
                            "orders_previous": "Previous Orders",
                            "order_penetration_current": "Current Order Penetration %",
                            "order_penetration_previous": "Previous Order Penetration %",
                            "order_penetration_change": "Order Penetration Change",
                        }
                    )
                    table_view = table_view[
                        [
                            "Product Name",
                            "Current Qty",
                            "Previous Qty",
                            "Qty Change",
                            "Qty Change %",
                            "Current Revenue",
                            "Previous Revenue",
                            "Revenue Change",
                            "Revenue Change %",
                            "Current Orders",
                            "Previous Orders",
                            "Current Order Penetration %",
                            "Previous Order Penetration %",
                            "Order Penetration Change",
                        ]
                    ].head(50).set_index("Product Name")
                    st.dataframe(
                        style_product_comparison_table(
                            table_view
                        ),
                        use_container_width=True,
                        hide_index=False,
                    )

                with lower_right:
                    st.markdown("**Revenue vs Penetration Change**")
                    scatter_chart = px.scatter(
                        display_comparison.head(60),
                        x="order_penetration_change",
                        y="revenue_change",
                        size="qty_current",
                        hover_name="product_name",
                        color="revenue_change",
                        color_continuous_scale="RdYlGn",
                    )
                    scatter_chart.update_layout(
                        xaxis_title="Order Penetration Change (pts)",
                        yaxis_title="Revenue Change",
                        coloraxis_showscale=False,
                    )
                    st.plotly_chart(scatter_chart, use_container_width=True)

        with category_tab:
            category_current = product_filtered[product_filtered["category_name"] != ""].copy()
            category_previous = product_previous[product_previous["category_name"] != ""].copy()

            category_options = ["All Categories"] + sorted(
                set(category_current["category_name"].dropna().astype(str).tolist())
                | set(category_previous["category_name"].dropna().astype(str).tolist())
            )
            selected_category = st.selectbox(
                "Category Name",
                category_options,
                index=0,
                key="category_penetration_filter",
            )
            if selected_category != "All Categories":
                category_current = category_current[category_current["category_name"] == selected_category]
                category_previous = category_previous[category_previous["category_name"] == selected_category]

            category_comparison = compare_category_summary(category_current, category_previous)
            category_cards = build_category_penetration_cards(category_comparison)
            category_change_summary = build_category_change_summary(category_comparison)

            st.caption(
                "Category-level penetration is calculated using distinct phone numbers from clean data. "
                "Walk-ins are excluded from the penetration base."
            )

            card_cols = st.columns(4)
            for column, (label, value) in zip(card_cols, category_cards.items()):
                column.metric(label, value)

            if category_comparison.empty:
                st.info("No category rows found for the selected filters.")
            else:
                summary_cols = st.columns(4)
                for column, (label, value) in zip(summary_cols, category_change_summary.items()):
                    column.metric(label, f"{value:,}")

                sort_labels = {
                    "Customer Increase": "customers_change",
                    "Customer Decrease": "customers_change",
                    "Revenue Increase": "revenue_change",
                    "Penetration Increase": "customer_penetration_change",
                }
                selected_sort = st.selectbox(
                    "Highlight category movement by",
                    list(sort_labels.keys()),
                    index=0,
                    key="category_penetration_sort",
                )
                sort_column = sort_labels[selected_sort]
                ascending = selected_sort == "Customer Decrease"
                display_comparison = category_comparison.sort_values(sort_column, ascending=ascending).copy()

                customer_current = int(category_current.loc[category_current["mob_no"].ne(WALKIN_PLACEHOLDER), "mob_no"].nunique())
                customer_previous = int(category_previous.loc[category_previous["mob_no"].ne(WALKIN_PLACEHOLDER), "mob_no"].nunique())
                customer_cols = st.columns(2)
                customer_cols[0].metric("Current Distinct Phone Count", f"{customer_current:,}")
                customer_cols[1].metric("Previous Distinct Phone Count", f"{customer_previous:,}")

                top_left, top_right = st.columns(2)
                with top_left:
                    st.markdown("**Top Category Customer Movers**")
                    customer_chart_source = category_comparison.reindex(
                        category_comparison["customers_change"].abs().sort_values(ascending=False).index
                    ).head(12)
                    customer_chart = px.bar(
                        customer_chart_source.sort_values("customers_change"),
                        x="customers_change",
                        y="category_name",
                        orientation="h",
                        color="customers_change",
                        color_continuous_scale="RdYlGn",
                    )
                    customer_chart.update_layout(xaxis_title="Customer Change", yaxis_title=None, coloraxis_showscale=False)
                    st.plotly_chart(customer_chart, use_container_width=True)

                with top_right:
                    st.markdown("**Top Category Penetration Gainers**")
                    penetration_chart_source = category_comparison.reindex(
                        category_comparison["customer_penetration_change"].abs().sort_values(ascending=False).index
                    ).head(12)
                    penetration_chart = px.bar(
                        penetration_chart_source.sort_values("customer_penetration_change"),
                        x="customer_penetration_change",
                        y="category_name",
                        orientation="h",
                        color="customer_penetration_change",
                        color_continuous_scale="RdYlGn",
                    )
                    penetration_chart.update_layout(
                        xaxis_title="Penetration Change (pts)",
                        yaxis_title=None,
                        coloraxis_showscale=False,
                    )
                    st.plotly_chart(penetration_chart, use_container_width=True)

                lower_left, lower_right = st.columns([1.4, 1.0])
                with lower_left:
                    st.markdown("**Category Comparison Table**")
                    table_view = display_comparison.rename(
                        columns={
                            "category_name": "Category Name",
                            "qty_current": "Current Qty",
                            "qty_previous": "Previous Qty",
                            "qty_change": "Qty Change",
                            "qty_change_pct": "Qty Change %",
                            "revenue_current": "Current Revenue",
                            "revenue_previous": "Previous Revenue",
                            "revenue_change": "Revenue Change",
                            "revenue_change_pct": "Revenue Change %",
                            "orders_current": "Current Orders",
                            "orders_previous": "Previous Orders",
                            "customers_current": "Current Customers",
                            "customers_previous": "Previous Customers",
                            "customers_change": "Customers Change",
                            "customer_penetration_current": "Current Customer Penetration %",
                            "customer_penetration_previous": "Previous Customer Penetration %",
                            "customer_penetration_change": "Customer Penetration Change",
                        }
                    )
                    table_view = table_view[
                        [
                            "Category Name",
                            "Current Qty",
                            "Previous Qty",
                            "Qty Change",
                            "Qty Change %",
                            "Current Revenue",
                            "Previous Revenue",
                            "Revenue Change",
                            "Revenue Change %",
                            "Current Orders",
                            "Previous Orders",
                            "Current Customers",
                            "Previous Customers",
                            "Customers Change",
                            "Current Customer Penetration %",
                            "Previous Customer Penetration %",
                            "Customer Penetration Change",
                        ]
                    ].head(50).set_index("Category Name")
                    st.dataframe(
                        style_category_comparison_table(
                            table_view
                        ),
                        use_container_width=True,
                        hide_index=False,
                    )

                with lower_right:
                    st.markdown("**Revenue vs Penetration Change**")
                    scatter_chart = px.scatter(
                        display_comparison.head(60),
                        x="customer_penetration_change",
                        y="revenue_change",
                        size="customers_current",
                        hover_name="category_name",
                        color="customers_change",
                        color_continuous_scale="RdYlGn",
                    )
                    scatter_chart.update_layout(
                        xaxis_title="Customer Penetration Change (pts)",
                        yaxis_title="Revenue Change",
                        coloraxis_showscale=False,
                    )
                    st.plotly_chart(scatter_chart, use_container_width=True)

if selected_page == "Monthly Wallet":
    wallet_source = branch_filtered_df.copy()
    monthly_wallet = build_monthly_wallet_matrix(wallet_source)

    st.subheader("Monthly Wallet")
    if monthly_wallet.empty:
        st.info("No monthly wallet rows found for the selected filters.")
    else:
        st.dataframe(style_currency_matrix(monthly_wallet), use_container_width=True)

if selected_page == "Persona":
    persona_source = load_persona_item_data(selected_branch, selected_type)

    st.subheader("Persona")
    st.caption(
        "Enter a phone number to see the customer's product mix, repeat buying gap by product, "
        "and personalized product follow-up suggestions."
    )

    if persona_source.empty:
        st.info("No item-level customer rows found for the selected branch/type filters.")
    else:
        input_col, number_col = st.columns([1.2, 0.6])
        with input_col:
            persona_phone = st.text_input("Phone Number", placeholder="Enter customer phone number")
        with number_col:
            recommendation_limit = st.number_input(
                "Products to show",
                min_value=1,
                max_value=20,
                value=5,
                step=1,
            )

        normalized_phone = str(persona_phone).strip()
        if not normalized_phone:
            st.info("Enter a phone number to generate personalization.")
        else:
            customer_frame = persona_source[persona_source["mob_no"] == normalized_phone].copy()
            if customer_frame.empty:
                st.warning("No customer history found for that phone number in the filtered data.")
            else:
                product_mix = build_persona_product_mix(customer_frame)
                repeat_profile = build_persona_repeat_profile(customer_frame)
                recommendations = build_persona_recommendations(customer_frame, int(recommendation_limit))

                first_purchase = customer_frame["sales_day"].min()
                last_purchase = customer_frame["sales_day"].max()
                total_orders = int(customer_frame["sales_no"].replace("", pd.NA).nunique())
                total_spend = float(customer_frame["net_amount"].sum())
                unique_products = int(customer_frame["product_name"].nunique())

                metric_cols = st.columns(5)
                metric_cols[0].metric("Phone", normalized_phone)
                metric_cols[1].metric("Orders", f"{total_orders:,}")
                metric_cols[2].metric("Spend", f"{total_spend:,.2f}")
                metric_cols[3].metric("Unique Products", f"{unique_products:,}")
                metric_cols[4].metric(
                    "Active Window",
                    f"{int((last_purchase - first_purchase).days):,} days",
                )

                top_left, top_right = st.columns([1.3, 1.0])
                with top_left:
                    st.markdown("**Product Wise Product Mix**")
                    mix_view = product_mix.rename(
                        columns={
                            "product_name": "Product",
                            "category_name": "Category",
                            "qty": "Qty",
                            "revenue": "Revenue",
                            "orders": "Orders",
                            "revenue_mix_pct": "Revenue Mix %",
                            "last_bought": "Last Bought",
                        }
                    )
                    st.dataframe(mix_view.head(int(recommendation_limit)), use_container_width=True, hide_index=True)

                with top_right:
                    st.markdown("**Revenue Mix Chart**")
                    mix_chart_source = product_mix.head(int(recommendation_limit)).copy()
                    mix_chart = px.pie(
                        mix_chart_source,
                        names="product_name",
                        values="revenue",
                    )
                    st.plotly_chart(mix_chart, use_container_width=True)

                bottom_left, bottom_right = st.columns([1.3, 1.0])
                with bottom_left:
                    st.markdown("**Average Days To Buy Again By Product**")
                    if repeat_profile.empty:
                        st.info("No repeat-product pattern found for this customer yet.")
                    else:
                        repeat_view = repeat_profile.rename(
                            columns={
                                "product_name": "Product",
                                "purchase_days": "Purchase Days",
                                "repeat_cycles": "Repeat Cycles",
                                "avg_repeat_days": "Avg Repeat Days",
                                "last_bought": "Last Bought",
                                "days_since_last_purchase": "Days Since Last Purchase",
                                "reorder_signal": "Signal",
                            }
                        )
                        st.dataframe(repeat_view.head(int(recommendation_limit)), use_container_width=True, hide_index=True)

                with bottom_right:
                    st.markdown("**Personalization Suggestions**")
                    if recommendations.empty:
                        st.info("No repeat-driven suggestions available yet for this customer.")
                    else:
                        recommendation_view = recommendations.rename(
                            columns={
                                "product_name": "Product",
                                "purchase_days": "Purchase Days",
                                "avg_repeat_days": "Avg Repeat Days",
                                "days_since_last_purchase": "Days Since Last Purchase",
                                "gap_vs_average": "Gap vs Average",
                                "reorder_signal": "Signal",
                            }
                        )
                        st.dataframe(
                            recommendation_view[
                                [
                                    "Product",
                                    "Purchase Days",
                                    "Avg Repeat Days",
                                    "Days Since Last Purchase",
                                    "Gap vs Average",
                                    "Signal",
                                ]
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )

if selected_page == "Churn Dashboard":
    churn_item_source = load_persona_item_data(selected_branch, selected_type)
    churn_limit_col, churn_rule_col = st.columns([0.7, 0.7])
    with churn_limit_col:
        churn_list_limit = st.number_input(
            "Customers to show",
            min_value=5,
            max_value=100,
            value=25,
            step=5,
            key="churn_list_limit",
        )
    with churn_rule_col:
        churn_threshold_days = st.number_input(
            "Churn threshold (days)",
            min_value=30,
            max_value=180,
            value=60,
            step=5,
            key="churn_threshold_days",
        )

    churn_profile = build_customer_churn_profile(
        branch_filtered_df,
        churn_item_source,
        reference_date=end_date,
        churn_days=int(churn_threshold_days),
    )

    st.subheader("Churn Dashboard")
    st.caption(
        f"Customers with more than {int(churn_threshold_days)} days since last purchase are marked as churned. "
        "Status is evaluated as of the selected end date."
    )
    render_churn_definitions(int(churn_threshold_days))

    if churn_profile.empty:
        st.info("No non-walkin customer history found for the selected branch/type filters.")
    else:
        churn_cards = build_churn_summary_cards(churn_profile)
        card_cols = st.columns(5)
        for column, (label, value) in zip(card_cols, churn_cards.items()):
            if isinstance(value, float):
                column.metric(label, f"{value:,.2f}")
            else:
                column.metric(label, f"{value:,}")

        status_mix = build_churn_status_mix(churn_profile)
        segment_summary = build_churn_segment_summary(churn_profile)
        monthly_trend = build_monthly_churn_trend(churn_profile)
        action_list = build_churn_action_list(churn_profile, int(churn_list_limit))
        reason_summary, churned_detail, category_risk, churn_reason_metrics = build_churn_reason_analysis(churn_profile)

        top_left, top_mid, top_right = st.columns([0.9, 1.2, 1.5])

        with top_left:
            st.markdown("**Status Mix**")
            status_chart = px.pie(status_mix, names="status", values="customers")
            st.plotly_chart(status_chart, use_container_width=True)

        with top_mid:
            st.markdown("**Monthly Status Trend**")
            if monthly_trend.empty:
                st.info("No monthly trend found.")
            else:
                trend_chart = px.bar(
                    monthly_trend,
                    x="month_label",
                    y="customers",
                    color="status",
                    barmode="group",
                    color_discrete_map={"Active": "#69a83b", "At Risk": "#f59e0b", "Churned": "#c0392b"},
                )
                trend_chart.update_layout(xaxis_title=None, yaxis_title=None)
                st.plotly_chart(trend_chart, use_container_width=True)

        with top_right:
            st.markdown("**Segment x Status Summary**")
            if segment_summary.empty:
                st.info("No segment summary found.")
            else:
                segment_view = segment_summary.rename(
                    columns={
                        "customer_segment": "Customer Segment",
                        "status": "Status",
                        "customers": "Customers",
                        "revenue": "Revenue",
                    }
                )
                selection_event = st.dataframe(
                    segment_view,
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="churn_segment_status_table",
                )

        selected_rows = selection_event.get("selection", {}).get("rows", []) if "selection_event" in locals() else []
        if selected_rows:
            selected_index = int(selected_rows[0])
            selected_segment = str(segment_view.iloc[selected_index]["Customer Segment"])
            selected_status = str(segment_view.iloc[selected_index]["Status"])
            selected_customers = churn_profile[
                (churn_profile["customer_segment"] == selected_segment)
                & (churn_profile["status"] == selected_status)
            ].copy()
            selected_customers = selected_customers.rename(
                columns={
                    "phone": "Phone",
                    "total_spend": "Total Value",
                    "total_orders": "Total Orders",
                    "days_since_last_purchase": "Days Since Last Purchase",
                    "favorite_product": "Favorite Product",
                    "favorite_category": "Favorite Category",
                }
            )
            st.markdown(f"**Selected Details: {selected_segment} + {selected_status}**")
            st.dataframe(
                selected_customers[
                    [
                        "Phone",
                        "Total Value",
                        "Total Orders",
                        "Days Since Last Purchase",
                        "Favorite Product",
                        "Favorite Category",
                    ]
                ].sort_values("Total Value", ascending=False),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("Select any row in `Segment x Status Summary` to view customer-level details.")

        bottom_left, bottom_right = st.columns([1.6, 1.0])

        with bottom_left:
            st.markdown("**At-Risk / Churned Customer Action List**")
            if action_list.empty:
                st.info("No at-risk or churned customers found for the selected filters.")
            else:
                action_view = action_list.rename(
                    columns={
                        "phone": "Phone",
                        "status": "Status",
                        "days_since_last_purchase": "Days Since Last Purchase",
                        "avg_reorder_days": "Avg Reorder Days",
                        "days_over_repeat_gap": "Days Over Repeat Gap",
                        "total_orders": "Total Orders",
                        "total_spend": "Total Spend",
                        "avg_order_value": "AOV",
                        "favorite_category": "Favorite Category",
                        "favorite_product": "Favorite Product",
                        "repeat_anchor_product": "Repeat Product",
                        "orders_last_30": "Orders L30",
                        "orders_last_60": "Orders L60",
                        "orders_last_90": "Orders L90",
                        "priority_score": "Priority Score",
                        "action_hint": "Action Hint",
                    }
                )
                st.dataframe(
                    action_view[
                        [
                            "Phone",
                            "Status",
                            "Days Since Last Purchase",
                            "Avg Reorder Days",
                            "Days Over Repeat Gap",
                            "Total Orders",
                            "Total Spend",
                            "AOV",
                            "Favorite Category",
                            "Favorite Product",
                            "Repeat Product",
                            "Orders L30",
                            "Orders L60",
                            "Orders L90",
                            "Priority Score",
                            "Action Hint",
                        ]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )

        with bottom_right:
            st.markdown("**Improvement Ideas**")
            active_count = int((churn_profile["status"] == "Active").sum())
            at_risk_count = int((churn_profile["status"] == "At Risk").sum())
            churned_count = int((churn_profile["status"] == "Churned").sum())
            avg_gap = float(churn_profile["avg_reorder_days"].dropna().mean()) if churn_profile["avg_reorder_days"].notna().any() else 0.0
            top_category = (
                churn_profile["favorite_category"].dropna().mode().iat[0]
                if churn_profile["favorite_category"].dropna().any()
                else "core staples"
            )
            st.metric("Average Reorder Gap", f"{avg_gap:,.1f} days")
            st.metric("At-Risk Customers", f"{at_risk_count:,}")
            st.metric("Churned Customers", f"{churned_count:,}")
            st.metric("Active Customers", f"{active_count:,}")
            st.markdown(
                "\n".join(
                    [
                        f"- Send reminder campaigns before day {max(int(churn_threshold_days) - 10, 1)}.",
                        f"- Build win-back offers around `{top_category}` because it is the strongest repeat affinity.",
                        "- Prioritize customers with high spend and zero orders in the last 30 days.",
                        "- Use the `Action Hint` field to decide whether to remind, win-back, or upsell.",
                    ]
                )
            )

        st.markdown("**Churn Reason Analysis**")
        if reason_summary.empty:
            st.info("No churned customers found for reason analysis.")
        else:
            reason_card_cols = st.columns(4)
            reason_card_cols[0].metric("Churned Customers", f"{int(churn_reason_metrics['churned_customers']):,}")
            reason_card_cols[1].metric("One-Time Buyer %", f"{churn_reason_metrics['one_time_pct']:.2f}%")
            reason_card_cols[2].metric("No Orders L90 %", f"{churn_reason_metrics['no_orders_l90_pct']:.2f}%")
            reason_card_cols[3].metric("Cycle Break %", f"{churn_reason_metrics['cycle_break_pct']:.2f}%")
            st.caption(
                "`Cycle Break %` means churned customers where `Days Since Last Purchase` is greater than "
                "`1.5 x Avg Reorder Days` (customers with a valid reorder cycle only)."
            )
            cycle_break_click_col, _ = st.columns([0.45, 0.55])
            with cycle_break_click_col:
                show_cycle_break_only = st.button(
                    "Show Cycle Break Customers",
                    key="show_cycle_break_customers",
                    use_container_width=True,
                )

            reason_left, reason_right = st.columns([1.3, 1.0])
            with reason_left:
                reason_chart = px.bar(
                    reason_summary.sort_values("customers"),
                    x="customers",
                    y="primary_churn_reason",
                    orientation="h",
                    color="customers",
                    color_continuous_scale="OrRd",
                )
                reason_chart.update_layout(xaxis_title="Customers", yaxis_title=None, coloraxis_showscale=False)
                st.plotly_chart(reason_chart, use_container_width=True)

            with reason_right:
                st.markdown("**High Churn Categories**")
                if category_risk.empty:
                    st.info("No category-wise churn pattern found.")
                else:
                    st.dataframe(
                        category_risk.rename(
                            columns={
                                "favorite_category": "Category",
                                "churned_customers": "Churned Customers",
                                "churned_revenue": "Churned Revenue",
                            }
                        ).head(12),
                        use_container_width=True,
                        hide_index=True,
                    )

            reason_select_view = reason_summary.rename(
                columns={
                    "primary_churn_reason": "Primary Churn Reason",
                    "customers": "Customers",
                    "revenue_lost": "Revenue Lost",
                    "avg_days_since_last": "Avg Days Since Last",
                }
            )
            st.markdown("**Reason Summary (Click Any Row For Customer Details)**")
            reason_selection = st.dataframe(
                reason_select_view,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="churn_reason_summary_table",
            )

            selected_reason = None
            if show_cycle_break_only:
                selected_reason = "Missed expected reorder cycle"
            selected_reason_rows = reason_selection.get("selection", {}).get("rows", [])
            if selected_reason_rows:
                selected_reason = str(reason_select_view.iloc[int(selected_reason_rows[0])]["Primary Churn Reason"])

            st.markdown("**Churned Customer Reason Details**")
            churn_reason_view = churned_detail.rename(
                columns={
                    "phone": "Phone",
                    "primary_churn_reason": "Primary Churn Reason",
                    "days_since_last_purchase": "Days Since Last Purchase",
                    "avg_reorder_days": "Avg Reorder Days",
                    "total_orders": "Total Orders",
                    "total_spend": "Total Value",
                    "orders_last_90": "Orders L90",
                    "favorite_category": "Favorite Category",
                    "favorite_product": "Favorite Product",
                    "action_hint": "Action Hint",
                }
            )
            reason_filtered_view = churn_reason_view.copy()
            if selected_reason:
                reason_filtered_view = reason_filtered_view[
                    reason_filtered_view["Primary Churn Reason"] == selected_reason
                ].copy()
                st.caption(f"Showing details for: `{selected_reason}`")
            else:
                st.caption("Tip: click a row in `Reason Summary` to see matching customer details only.")
            reason_detail_columns = [
                "Phone",
                "Primary Churn Reason",
                "Days Since Last Purchase",
                "Avg Reorder Days",
                "Total Orders",
                "Total Value",
                "Orders L90",
                "Favorite Category",
                "Favorite Product",
                "Action Hint",
            ]
            reason_detail_display = reason_filtered_view[reason_detail_columns].sort_values(
                ["Days Since Last Purchase", "Total Value"],
                ascending=[False, False],
            )
            if not selected_reason:
                reason_detail_display = reason_detail_display.head(int(churn_list_limit))
            st.dataframe(
                reason_detail_display,
                use_container_width=True,
                hide_index=True,
            )

if selected_page == "Customer Count Branch Wise":
    branch_source = branch_filtered_df.copy()
    branch_summary = build_branchwise_summary(branch_source)
    month_summary = build_month_customer_summary(branch_source)

    top_left, top_right = st.columns([2.4, 1.0])
    with top_left:
        st.subheader("Customer Count, Total Sales & AOV Branch Wise")
        if branch_summary.empty:
            st.info("No branch-wise rows found for the selected branches.")
        else:
            st.dataframe(branch_summary, use_container_width=True, hide_index=True)

    with top_right:
        st.subheader("Month Summary")
        if month_summary.empty:
            st.info("No month summary rows found.")
        else:
            st.dataframe(month_summary, use_container_width=True, hide_index=True)

    available_months = (
        branch_source[["sales_month", "year_month_label"]]
        .drop_duplicates()
        .sort_values("sales_month")
    )
    month_options = available_months["year_month_label"].tolist()
    selected_month_label = st.selectbox(
        "Select month to show month-only records",
        month_options,
        index=len(month_options) - 1 if month_options else 0,
    )
    selected_month_value = available_months.loc[
        available_months["year_month_label"] == selected_month_label, "sales_month"
    ].iloc[0]

    month_day_summary = build_month_day_summary(branch_source, selected_month_value)
    order_rank_summary = build_order_rank_summary(branch_source, selected_month_value)

    bottom_left, bottom_mid, bottom_right = st.columns([1.6, 1.0, 1.2])

    with bottom_left:
        st.subheader("Month / Day Customer Summary")
        if month_day_summary.empty:
            st.info("No day-wise rows found for the selected month.")
        else:
            st.dataframe(month_day_summary, use_container_width=True, hide_index=True)

    with bottom_mid:
        st.subheader("Users at N- and Users at N+1")
        if order_rank_summary.empty:
            st.info("No order-rank rows found for the selected month.")
        else:
            st.dataframe(order_rank_summary, use_container_width=True, hide_index=True)

    with bottom_right:
        st.subheader("Users at N- and Users at N+1 by Value")
        if order_rank_summary.empty:
            st.info("No chart rows found for the selected month.")
        else:
            chart_source = order_rank_summary[order_rank_summary["Value"] != "Total"].copy()
            chart_long = chart_source.melt(
                id_vars="Value",
                value_vars=["Users at N-", "Users at N+1"],
                var_name="Series",
                value_name="Users",
            )
            rank_chart = px.bar(
                chart_long,
                x="Value",
                y="Users",
                color="Series",
                barmode="group",
                color_discrete_map={"Users at N-": "#69a83b", "Users at N+1": "#2f3a75"},
            )
            st.plotly_chart(rank_chart, use_container_width=True)

if selected_page == "Order Repeat":
    order_source = branch_filtered_df[~branch_filtered_df["is_b2b"]].copy()
    mom_order_counts, mom_order_percentages = build_order_count_matrix(order_source, "cohort_month1", "cohort_index", base_column=1)
    aov_distribution = build_order_aov_distribution(order_source)
    l45_order_counts, _ = build_order_count_matrix(order_source[order_source["bin_value_45"] >= 0], "order_month_label", "bin_value_45", base_column=0)

    top_left, top_right = st.columns(2)
    bottom_left, bottom_right = st.columns(2)

    with top_left:
        st.subheader("MoM Order Count")
        if mom_order_counts.empty:
            st.info("No order cohort rows found.")
        else:
            st.dataframe(style_count_matrix(mom_order_counts), use_container_width=True)

    with top_right:
        st.subheader("Order Count AOV Distribution")
        if aov_distribution.empty:
            st.info("No AOV distribution rows found.")
        else:
            st.dataframe(style_count_matrix(aov_distribution), use_container_width=True)

    with bottom_left:
        st.subheader("MoM Order Count %")
        if mom_order_percentages.empty:
            st.info("No MoM order percentage rows found.")
        else:
            st.dataframe(style_percentage_matrix(mom_order_percentages), use_container_width=True)

    with bottom_right:
        st.subheader("L45 Repeat Orders")
        if l45_order_counts.empty:
            st.info("No L45 repeat order rows found.")
        else:
            st.dataframe(style_count_matrix(l45_order_counts), use_container_width=True)

if selected_page == "Tags":
    tags_source = branch_filtered_df.copy()
    available_tag_months = (
        tags_source[["sales_month", "year_month_label"]]
        .drop_duplicates()
        .sort_values("sales_month")
    )
    if available_tag_months.empty:
        st.info("No rows found for Tags.")
    else:
        selected_tag_month_label = st.selectbox(
            "Tags month",
            available_tag_months["year_month_label"].tolist(),
            index=len(available_tag_months) - 1,
        )
        selected_tag_month = available_tag_months.loc[
            available_tag_months["year_month_label"] == selected_tag_month_label, "sales_month"
        ].iloc[0]
        latest_sales_day = tags_source["sales_day"].max()
        yesterday_cutoff = latest_sales_day.normalize() - pd.Timedelta(days=1)
        selected_month_end = selected_tag_month + pd.offsets.MonthEnd(0)
        selected_day_cutoff = None
        if selected_tag_month.year == yesterday_cutoff.year and selected_tag_month.month == yesterday_cutoff.month:
            selected_day_cutoff = yesterday_cutoff
        elif selected_month_end.normalize() > latest_sales_day.normalize():
            selected_day_cutoff = latest_sales_day.normalize()

        previous_tag_month = selected_tag_month - pd.offsets.MonthBegin(1)
        previous_day_cutoff = None
        if selected_day_cutoff is not None:
            previous_day_number = min(
                selected_day_cutoff.day,
                (previous_tag_month + pd.offsets.MonthEnd(0)).day,
            )
            previous_day_cutoff = previous_tag_month + pd.Timedelta(days=previous_day_number - 1)

        tags_dashboard, tags_chart, tags_below, tags_inventory, tags_daily, tags_comparison = st.tabs(
            ["Dashboard", "Chart", "Below1000", "Inventory", "Daily Tags", "Comparison"]
        )

        tag_spend = build_tags_customer_spend(tags_source, selected_tag_month, cutoff_day=selected_day_cutoff)
        tag_inventory = build_tags_inventory(tags_source, selected_tag_month, cutoff_day=selected_day_cutoff)
        tag_below = build_tags_below_1000(tags_source, selected_tag_month, cutoff_day=selected_day_cutoff)
        tag_chart_source = build_tags_chart_source(tags_source, selected_tag_month, cutoff_day=selected_day_cutoff)
        tag_daily = build_daily_tag_counts(tags_source, selected_tag_month, cutoff_day=selected_day_cutoff)
        tag_day_compare = build_tag_day_comparison(
            tags_source,
            selected_tag_month,
            current_cutoff_day=selected_day_cutoff,
            previous_cutoff_day=previous_day_cutoff,
        )
        tag_week_compare = build_tag_week_comparison(
            tags_source,
            selected_tag_month,
            current_cutoff_day=selected_day_cutoff,
            previous_cutoff_day=previous_day_cutoff,
        )
        tag_daytype_compare = build_tag_daytype_comparison(
            tags_source,
            selected_tag_month,
            current_cutoff_day=selected_day_cutoff,
            previous_cutoff_day=previous_day_cutoff,
        )
        tag_change_summary = build_tag_change_summary(
            tags_source,
            selected_tag_month,
            current_cutoff_day=selected_day_cutoff,
            previous_cutoff_day=previous_day_cutoff,
        )
        current_tag_increments = build_daily_tag_increments(
            tags_source,
            selected_tag_month,
            cutoff_day=selected_day_cutoff,
        )
        previous_tag_increments = build_daily_tag_increments(
            tags_source,
            previous_tag_month,
            cutoff_day=previous_day_cutoff,
        )

        with tags_dashboard:
            st.subheader("Customer Spend Tags Dashboard")
            if tag_spend.empty:
                st.info("No tag rows found for the selected month.")
            else:
                metric_cols = st.columns(4)
                metric_cols[0].metric("Tagged Customers", f"{len(tag_spend):,}")
                metric_cols[1].metric("1000+ Customers", f"{int((tag_spend['Spend'] >= 1000).sum()):,}")
                metric_cols[2].metric("5000+ Customers", f"{int((tag_spend['Spend'] >= 5000).sum()):,}")
                metric_cols[3].metric("10000+ Customers", f"{int((tag_spend['Spend'] >= 10000).sum()):,}")
                st.dataframe(tag_spend, use_container_width=True, hide_index=True)

        with tags_chart:
            top_left, top_right = st.columns(2)
            with top_left:
                st.subheader("Gift Unlock Count")
                if tag_inventory.empty:
                    st.info("No inventory rows found for the selected month.")
                else:
                    gift_chart = px.bar(
                        tag_inventory,
                        x="Gift Name",
                        y="Unlocked Count",
                        color="Gift Name",
                    )
                    gift_chart.update_layout(showlegend=False, xaxis_title=None, yaxis_title=None)
                    st.plotly_chart(gift_chart, use_container_width=True)
            with top_right:
                st.subheader("Customer Spend Split")
                split_source = tag_chart_source[tag_chart_source["chart"] == "Customer Split"].copy()
                if split_source.empty:
                    st.info("No spend split rows found for the selected month.")
                else:
                    split_chart = px.pie(split_source, names="label", values="value")
                    st.plotly_chart(split_chart, use_container_width=True)

        with tags_below:
            st.subheader("Below 1000")
            if tag_below.empty:
                st.info("No customers below 1000 for the selected month.")
            else:
                st.dataframe(tag_below, use_container_width=True, hide_index=True)

        with tags_inventory:
            st.subheader("Inventory")
            if tag_inventory.empty:
                st.info("No inventory rows found for the selected month.")
            else:
                st.dataframe(tag_inventory, use_container_width=True, hide_index=True)

        with tags_daily:
            st.subheader("Daily Tag Counts")
            if tag_daily.empty:
                st.info("No daily tag rows found for the selected month.")
            else:
                st.dataframe(tag_daily, use_container_width=True, hide_index=True)

        with tags_comparison:
            st.subheader(
                f"Tag Comparison: {selected_tag_month.strftime('%B %Y')} vs {previous_tag_month.strftime('%B %Y')}"
            )
            if selected_day_cutoff is not None:
                st.caption(
                    f"Comparison limited till {selected_day_cutoff.strftime('%Y-%m-%d')} and matched to the same elapsed days in {previous_tag_month.strftime('%B %Y')}."
                )

            comparison_view = st.selectbox(
                "Comparison option",
                ["Overall", "Last Day", "Last Week", "Weekday Total", "Weekend Total"],
                key="tags_comparison_option",
            )

            current_month_label = selected_tag_month.strftime("%b %Y")
            previous_month_label = previous_tag_month.strftime("%b %Y")
            summary_title = "Increase / Decrease %"
            summary_df = tag_change_summary

            if comparison_view == "Last Day" and not tag_day_compare.empty:
                current_last_day = current_tag_increments.sort_values("sales_day").iloc[-1] if not current_tag_increments.empty else None
                previous_last_day = previous_tag_increments.sort_values("sales_day").iloc[-1] if not previous_tag_increments.empty else None
                last_day_number = int(pd.to_datetime(current_last_day["sales_day"]).day) if current_last_day is not None else 0
                summary_title = f"Last Day Comparison: Day {last_day_number}"
                summary_df = build_tag_change_summary_from_values(
                    {
                        "1000": 0 if current_last_day is None else current_last_day.get("1000_tag contains", 0),
                        "2000": 0 if current_last_day is None else current_last_day.get("2000_tag contains", 0),
                        "5000": 0 if current_last_day is None else current_last_day.get("5000_tag contains", 0),
                        "10000": 0 if current_last_day is None else current_last_day.get("10000_tag contains", 0),
                    },
                    {
                        "1000": 0 if previous_last_day is None else previous_last_day.get("1000_tag contains", 0),
                        "2000": 0 if previous_last_day is None else previous_last_day.get("2000_tag contains", 0),
                        "5000": 0 if previous_last_day is None else previous_last_day.get("5000_tag contains", 0),
                        "10000": 0 if previous_last_day is None else previous_last_day.get("10000_tag contains", 0),
                    },
                    current_month_label,
                    previous_month_label,
                )
            elif comparison_view == "Last Week" and not current_tag_increments.empty:
                current_week = current_tag_increments.copy()
                current_week["Week"] = ((pd.to_datetime(current_week["sales_day"]).dt.day - 1) // 7) + 1
                previous_week = previous_tag_increments.copy()
                if not previous_week.empty:
                    previous_week["Week"] = ((pd.to_datetime(previous_week["sales_day"]).dt.day - 1) // 7) + 1
                last_week_number = int(current_week["Week"].max())
                current_week_row = current_week[current_week["Week"] == last_week_number][
                    ["1000_tag contains", "2000_tag contains", "5000_tag contains", "10000_tag contains"]
                ].sum()
                previous_week_row = (
                    previous_week[previous_week["Week"] == last_week_number][
                        ["1000_tag contains", "2000_tag contains", "5000_tag contains", "10000_tag contains"]
                    ].sum()
                    if not previous_week.empty
                    else pd.Series(dtype="float64")
                )
                summary_title = f"Last Week Comparison: Week {last_week_number}"
                summary_df = build_tag_change_summary_from_values(
                    {
                        "1000": current_week_row.get("1000_tag contains", 0),
                        "2000": current_week_row.get("2000_tag contains", 0),
                        "5000": current_week_row.get("5000_tag contains", 0),
                        "10000": current_week_row.get("10000_tag contains", 0),
                    },
                    {
                        "1000": previous_week_row.get("1000_tag contains", 0),
                        "2000": previous_week_row.get("2000_tag contains", 0),
                        "5000": previous_week_row.get("5000_tag contains", 0),
                        "10000": previous_week_row.get("10000_tag contains", 0),
                    },
                    current_month_label,
                    previous_month_label,
                )
            elif comparison_view in {"Weekday Total", "Weekend Total"} and not current_tag_increments.empty:
                segment_name = "Weekday" if comparison_view == "Weekday Total" else "Weekend"
                current_segment = current_tag_increments.copy()
                current_segment["Segment"] = pd.to_datetime(current_segment["sales_day"]).dt.dayofweek.map(
                    lambda value: "Weekend" if value >= 5 else "Weekday"
                )
                previous_segment = previous_tag_increments.copy()
                if not previous_segment.empty:
                    previous_segment["Segment"] = pd.to_datetime(previous_segment["sales_day"]).dt.dayofweek.map(
                        lambda value: "Weekend" if value >= 5 else "Weekday"
                    )
                current_segment_row = current_segment[current_segment["Segment"] == segment_name][
                    ["1000_tag contains", "2000_tag contains", "5000_tag contains", "10000_tag contains"]
                ].sum()
                previous_segment_row = (
                    previous_segment[previous_segment["Segment"] == segment_name][
                        ["1000_tag contains", "2000_tag contains", "5000_tag contains", "10000_tag contains"]
                    ].sum()
                    if not previous_segment.empty
                    else pd.Series(dtype="float64")
                )
                if not current_segment_row.empty:
                    summary_title = f"{segment_name} Total Comparison"
                    summary_df = build_tag_change_summary_from_values(
                        {
                            "1000": current_segment_row.get("1000_tag contains", 0),
                            "2000": current_segment_row.get("2000_tag contains", 0),
                            "5000": current_segment_row.get("5000_tag contains", 0),
                            "10000": current_segment_row.get("10000_tag contains", 0),
                        },
                        {
                            "1000": previous_segment_row.get("1000_tag contains", 0),
                            "2000": previous_segment_row.get("2000_tag contains", 0),
                            "5000": previous_segment_row.get("5000_tag contains", 0),
                            "10000": previous_segment_row.get("10000_tag contains", 0),
                        },
                        current_month_label,
                        previous_month_label,
                    )

            st.markdown(f"**{summary_title}**")
            render_tag_change_summary(summary_df, current_month_label, previous_month_label)

            top_left, top_right = st.columns(2)
            with top_left:
                st.markdown("**Day By Day**")
                if tag_day_compare.empty:
                    st.info("No day-by-day comparison rows found.")
                else:
                    st.dataframe(tag_day_compare, use_container_width=True, hide_index=True)
            with top_right:
                st.markdown("**Week By Week**")
                if tag_week_compare.empty:
                    st.info("No week-by-week comparison rows found.")
                else:
                    st.dataframe(tag_week_compare, use_container_width=True, hide_index=True)

            bottom_left, bottom_right = st.columns([1.1, 1.4])
            with bottom_left:
                st.markdown("**Weekday vs Weekend**")
                if tag_daytype_compare.empty:
                    st.info("No weekday/weekend comparison rows found.")
                else:
                    st.dataframe(tag_daytype_compare, use_container_width=True, hide_index=True)
            with bottom_right:
                if tag_day_compare.empty:
                    st.info("No comparison chart rows found.")
                else:
                    current_col = f"{selected_tag_month.strftime('%b %Y')} 1000"
                    previous_col = f"{previous_tag_month.strftime('%b %Y')} 1000"
                    day_chart_source = tag_day_compare[["Day", current_col, previous_col]].melt(
                        id_vars="Day",
                        var_name="Month",
                        value_name="Customers",
                    )
                    compare_chart = px.line(
                        day_chart_source,
                        x="Day",
                        y="Customers",
                        color="Month",
                        markers=True,
                    )
                    compare_chart.update_layout(xaxis_title=None, yaxis_title=None)
                    st.plotly_chart(compare_chart, use_container_width=True)
