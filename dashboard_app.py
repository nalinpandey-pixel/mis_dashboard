import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import date, timedelta
from typing import Dict, List, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st

from sales_pipeline import DEFAULT_DB_FILE, WALKIN_PLACEHOLDER, parse_utc_timestamp


APP_DIR = Path(__file__).resolve().parent


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


@st.cache_data(ttl=30)
def load_sales_data() -> pd.DataFrame:
    connection = sqlite3.connect(str(DEFAULT_DB_FILE))
    try:
        frame = pd.read_sql_query(
            """
            SELECT
                sales_date AS salesDate,
                sales_no AS order_id,
                mob_no AS phone,
                net_amount AS amount,
                branch_code,
                order_type
            FROM sales_raw
            ORDER BY sales_date
            """,
            connection,
        )
    finally:
        connection.close()

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


@st.cache_data(ttl=30)
def load_product_penetration_data() -> pd.DataFrame:
    connection = sqlite3.connect(str(DEFAULT_DB_FILE))
    try:
        frame = pd.read_sql_query(
            """
            SELECT
                sales_no,
                sales_date,
                branch_name,
                order_type,
                product_name,
                qty,
                net_amount,
                mob_no,
                cleaned_sales_no,
                cleaned_mob_no,
                cleaned_sales_date,
                cleaned_branch_code,
                cleaned_order_type
            FROM sales_items_joined
            WHERE cleaned_sales_no IS NOT NULL
            """,
            connection,
        )
    finally:
        connection.close()

    if frame.empty:
        return frame

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
    frame = frame[frame["product_name"] != ""].copy()
    frame["qty"] = pd.to_numeric(frame["qty"], errors="coerce").fillna(0.0)
    frame["net_amount"] = pd.to_numeric(frame["net_amount"], errors="coerce").fillna(0.0)
    return frame


def check_local_db_ready() -> bool:
    try:
        connection = sqlite3.connect(str(DEFAULT_DB_FILE))
        try:
            exists = pd.read_sql_query(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'sales_raw'
                """,
                connection,
            )
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return not exists.empty


def local_db_has_sales_rows() -> bool:
    if not check_local_db_ready():
        return False
    try:
        connection = sqlite3.connect(str(DEFAULT_DB_FILE))
        try:
            count_df = pd.read_sql_query("SELECT COUNT(*) AS row_count FROM sales_raw", connection)
        finally:
            connection.close()
    except Exception:
        return False
    return bool(int(count_df["row_count"].iloc[0]) > 0)


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
        .applymap(change_bg, subset=["Qty Change", "Revenue Change", "Order Penetration Change"])
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
        .applymap(color_change, subset=["Change", "Change %"])
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
st.caption("Live historical + latest cleaned sales from local SQLite")

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
    st.info("Run `python sales_pipeline.py` first so the local SQLite tables are created.")
    st.stop()

all_sales_df = load_sales_data()
if all_sales_df.empty:
    st.info("No rows found in local historical data.")
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
    product_df = load_product_penetration_data()
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

        if selected_branch != "All Branches":
            product_filtered = product_filtered[product_filtered["branch_code"] == selected_branch]
            product_previous = product_previous[product_previous["branch_code"] == selected_branch]
        if selected_type != "All Types":
            type_value = "" if selected_type == "Unspecified" else selected_type
            product_filtered = product_filtered[product_filtered["order_type"] == type_value]
            product_previous = product_previous[product_previous["order_type"] == type_value]

        product_options = ["All Products"] + sorted(
            set(product_filtered["product_name"].dropna().astype(str).tolist())
            | set(product_previous["product_name"].dropna().astype(str).tolist())
        )
        selected_product = st.selectbox(
            "Product Name",
            product_options,
            index=0,
        )
        if selected_product != "All Products":
            product_filtered = product_filtered[product_filtered["product_name"] == selected_product]
            product_previous = product_previous[product_previous["product_name"] == selected_product]

        product_comparison = compare_product_summary(product_filtered, product_previous)
        product_cards = build_product_penetration_cards(product_comparison)
        product_change_summary = build_product_change_summary(product_comparison)

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

            order_current = int(product_filtered["sales_no"].replace("", pd.NA).nunique())
            order_previous = int(product_previous["sales_no"].replace("", pd.NA).nunique())
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
                st.dataframe(
                    style_product_comparison_table(
                        table_view[
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
                        ].head(50)
                    ),
                    use_container_width=True,
                    hide_index=True,
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

if selected_page == "Monthly Wallet":
    wallet_source = branch_filtered_df.copy()
    monthly_wallet = build_monthly_wallet_matrix(wallet_source)

    st.subheader("Monthly Wallet")
    if monthly_wallet.empty:
        st.info("No monthly wallet rows found for the selected filters.")
    else:
        st.dataframe(style_currency_matrix(monthly_wallet), use_container_width=True)

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
