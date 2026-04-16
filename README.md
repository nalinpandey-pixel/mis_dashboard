# MIS Reporting Dashboard

This project has two Python entry points:

- `sales_pipeline.py`: reads raw rows from Supabase `sales_items`, cleans and groups them, and stores them in local SQLite.
- `dashboard_app.py`: Streamlit dashboard for the MIS reporting pages.

## Local setup

1. Create `.env` in the project root.
2. Add:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-key
```

3. Run:

```powershell
python sales_pipeline.py
python -m streamlit run dashboard_app.py
```

## Free web deployment with Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. Do not commit:
   - `.env`
   - `.streamlit/secrets.toml`
   - `crm_brain.db`
3. Open [Streamlit Community Cloud](https://share.streamlit.io/).
4. Create a new app from your GitHub repository.
5. Set the main file to:

```text
dashboard_app.py
```

6. In Streamlit app settings, add secrets:

```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-key"
```

You can also use:

```toml
SUPABASE_SERVICE_ROLE_KEY = "your-service-role-key"
```

## How refresh works after deployment

- `Refresh till now` runs `sales_pipeline.py`
- the pipeline reads from Supabase in read-only mode
- local SQLite tables are updated inside the deployed app environment
- the dashboard then reloads from those local tables

## Tables used locally

- `sales_raw`: cleaned historical + incremental cleaned sales rows
- `sales_cleaned_local`: cleaned grouped rows used for sync auditing
- `sales_removed_local`: removed rows from cleaning logic
- `sales_items_history`: raw Supabase mirror
- `sales_items_joined`: raw history joined back to cleaned sales using `sales_no`

## Notes

- The app never writes back to Supabase.
- For free hosting, the safest pattern is to use `Refresh till now` instead of depending on a permanent always-on 5-minute background worker.
- The dashboard supports backdated analysis using the date filters.
