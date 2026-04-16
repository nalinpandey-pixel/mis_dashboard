create extension if not exists pgcrypto;

create table if not exists public.sales_cleaned (
  record_id text primary key,
  sales_date date not null,
  sales_date_utc timestamptz,
  order_id text,
  phone text,
  amount numeric(14, 2) not null,
  branch_code text,
  order_type text,
  source_table text,
  synced_at timestamptz default now()
);

create index if not exists sales_cleaned_sales_date_idx on public.sales_cleaned (sales_date);
create index if not exists sales_cleaned_sales_date_utc_idx on public.sales_cleaned (sales_date_utc);
create index if not exists sales_cleaned_phone_idx on public.sales_cleaned (phone);
create index if not exists sales_cleaned_branch_idx on public.sales_cleaned (branch_code);

create table if not exists public.sales_removed_rows (
  removed_id text primary key,
  source_table text not null,
  removal_reason text,
  raw_payload jsonb not null,
  logged_at timestamptz default now()
);

create or replace view public.sales_daily_summary as
select
  sales_date,
  sum(amount) as net_sales,
  count(*) as orders,
  count(distinct nullif(phone, '(walkin with no details)')) as identified_customers,
  round(sum(amount) / nullif(count(*), 0), 2) as average_order_value
from public.sales_cleaned
group by sales_date
order by sales_date;

create or replace view public.sales_repeat_customer_summary as
with customer_orders as (
  select
    phone,
    count(*) as order_count,
    sum(amount) as total_sales
  from public.sales_cleaned
  where nullif(phone, '(walkin with no details)') is not null
  group by phone
)
select
  phone,
  order_count,
  total_sales
from customer_orders
where order_count > 1
order by total_sales desc;
