import pandas as pd
import sqlite3

# -------- FILE PATHS -------- #
excel_file = "oct_november.xlsx"   # your excel file
db_file = "crm_brain.db"

# -------- READ EXCEL -------- #
df = pd.read_excel(excel_file)

# Rename columns to safe SQL names
df.columns = [
    "sales_date",
    "sales_no",
    "mob_no",
    "net_amount",
    "branch_code",
    "order_type"
]

# -------- CONNECT SQLITE -------- #
conn = sqlite3.connect(db_file)
cursor = conn.cursor()

# -------- CREATE TABLE -------- #
cursor.execute("""
CREATE TABLE IF NOT EXISTS sales_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sales_date TEXT,
    sales_no TEXT,
    mob_no TEXT,
    net_amount float,
    branch_code TEXT,
    order_type TEXT
)
""")

# -------- INSERT DATA -------- #
df.to_sql("sales_raw", conn, if_exists="append", index=False)

conn.commit()
conn.close()

print("Data successfully loaded into sales_raw table")