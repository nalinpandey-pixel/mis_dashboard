CREATE TABLE IF NOT EXISTS public.sales_cleaned_local_backup (
    record_id text PRIMARY KEY,
    sales_date date NOT NULL,
    sales_date_utc timestamptz NOT NULL,
    order_id text,
    phone text,
    amount numeric(18, 2) NOT NULL,
    branch_code text,
    order_type text,
    source_table text NOT NULL,
    synced_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_backup_sales_date ON public.sales_cleaned_local_backup (sales_date);
CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_backup_sales_date_utc ON public.sales_cleaned_local_backup (sales_date_utc);
CREATE INDEX IF NOT EXISTS idx_sales_cleaned_local_backup_phone ON public.sales_cleaned_local_backup (phone);

CREATE TABLE IF NOT EXISTS public.sales_removed_local_backup (
    removed_id text PRIMARY KEY,
    sales_date_utc timestamptz,
    order_id text,
    phone text,
    amount numeric(18, 2),
    removal_reason text NOT NULL,
    source_table text NOT NULL,
    logged_at timestamptz NOT NULL,
    raw_payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_removed_local_backup_sales_date_utc ON public.sales_removed_local_backup (sales_date_utc);
CREATE INDEX IF NOT EXISTS idx_sales_removed_local_backup_phone ON public.sales_removed_local_backup (phone);

CREATE TABLE IF NOT EXISTS public.sales_raw_backup (
    id bigint,
    sales_date timestamptz,
    sales_no text,
    mob_no text,
    net_amount numeric(18, 2),
    branch_code text,
    order_type text
);
CREATE INDEX IF NOT EXISTS idx_sales_raw_backup_sales_date ON public.sales_raw_backup (sales_date);
CREATE INDEX IF NOT EXISTS idx_sales_raw_backup_sales_no ON public.sales_raw_backup (sales_no);
CREATE INDEX IF NOT EXISTS idx_sales_raw_backup_mob_no ON public.sales_raw_backup (mob_no);

CREATE TABLE IF NOT EXISTS public.sales_items_history_backup (
    _row_hash text PRIMARY KEY,
    _synced_at timestamptz NOT NULL,
    _source_table text NOT NULL,
    id bigint,
    sales_no text,
    item_code text,
    batch_no text,
    branch_id bigint,
    branch_name text,
    sales_date timestamptz,
    type text,
    order_type text,
    receipt_data text,
    total numeric(18, 3),
    mrp_amount numeric(18, 3),
    tcs_amount numeric(18, 3),
    tax_included boolean,
    customer_name text,
    mob_no text,
    billing_gst_in text,
    address text,
    product_name text,
    product_type text,
    hsn_code text,
    measurement_code text,
    category_name text,
    sub_category_name text,
    brand_name text,
    sub_brand_name text,
    department_name text,
    product_description text,
    mrp numeric(18, 3),
    tax_exclusive_mrp numeric(18, 3),
    price numeric(18, 3),
    selling_price numeric(18, 3),
    purchase_price numeric(18, 3),
    landing_cost numeric(18, 3),
    qty numeric(18, 3),
    discount numeric(18, 3),
    total_discount numeric(18, 3),
    flat_discount numeric(18, 3),
    bill_discount numeric(18, 3),
    item_flat_discount numeric(18, 3),
    item_bill_discount numeric(18, 3),
    other_discount numeric(18, 3),
    flat_discount_type text,
    bill_discount_type text,
    tax_rate numeric(18, 3),
    tax_amount numeric(18, 3),
    cgst numeric(18, 3),
    igst numeric(18, 3),
    cess_rate numeric(18, 3),
    cess_amount numeric(18, 3),
    basic_value numeric(18, 3),
    net_amount numeric(18, 3),
    profit numeric(18, 3),
    employee_name text,
    created_by text,
    sale_day date,
    customer_type text,
    is_excluded boolean,
    exclusion_reason text,
    excluded_at timestamptz,
    excluded_by text,
    ingested_at timestamptz,
    updated_at timestamptz,
    lmd_pushed boolean,
    lmd_pushed_at timestamptz,
    lmd_skip_reason text,
    order_status text
);
CREATE INDEX IF NOT EXISTS idx_sales_items_history_backup_sales_date ON public.sales_items_history_backup (sales_date);
CREATE INDEX IF NOT EXISTS idx_sales_items_history_backup_sales_no ON public.sales_items_history_backup (sales_no);
CREATE INDEX IF NOT EXISTS idx_sales_items_history_backup_mob_no ON public.sales_items_history_backup (mob_no);
CREATE INDEX IF NOT EXISTS idx_sales_items_history_backup_product_name ON public.sales_items_history_backup (product_name);
