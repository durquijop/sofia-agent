"""Analiza la estructura completa de Supabase."""
import httpx
import json

SUPABASE_URL = "https://vecspltvmyopwbjzerow.supabase.co"
SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZlY3NwbHR2bXlvcHdianplcm93Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0NDA1ODk3OSwiZXhwIjoyMDU5NjM0OTc5fQ.ufyhBSe09pvA7232vdGAdRve5n-izUqXvHlCXjBHKu0"

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
}


def get_swagger():
    r = httpx.get(f"{SUPABASE_URL}/rest/v1/", headers=HEADERS, timeout=30)
    return r.json()


def count_rows(table_name):
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/{table_name}?select=*",
        headers={**HEADERS, "Range": "0-0", "Prefer": "count=exact"},
        timeout=15,
    )
    content_range = r.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.split("/")[1]
        return int(total) if total != "*" else "?"
    return 0


def main():
    swagger = get_swagger()
    definitions = swagger.get("definitions", {})
    paths = swagger.get("paths", {})

    # Extract tables (skip views prefixed with vw_ and vista_)
    tables = {}
    views = {}
    for path_key in paths:
        if path_key == "/":
            continue
        name = path_key.lstrip("/")
        if name.startswith("vw_") or name.startswith("vista_"):
            views[name] = definitions.get(name, {})
        else:
            tables[name] = definitions.get(name, {})

    print(f"TOTAL: {len(tables)} tablas + {len(views)} vistas")
    print("=" * 80)

    # Get row counts and columns for each table
    print("\n## TABLAS (con conteo de filas)\n")
    for name in sorted(tables.keys()):
        defn = tables[name]
        desc = defn.get("description", "")
        props = defn.get("properties", {})
        cols = list(props.keys())
        try:
            count = count_rows(name)
        except Exception:
            count = "err"
        print(f"### {name} ({count} rows) - {desc}")
        for col in cols:
            col_info = props[col]
            col_type = col_info.get("type", col_info.get("format", "?"))
            col_desc = col_info.get("description", "")
            desc_str = f" -- {col_desc}" if col_desc else ""
            print(f"  {col}: {col_type}{desc_str}")
        print()

    print("\n## VISTAS\n")
    for name in sorted(views.keys()):
        defn = views[name]
        props = defn.get("properties", {})
        cols = list(props.keys())
        print(f"### {name} ({len(cols)} cols)")


if __name__ == "__main__":
    main()
