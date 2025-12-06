# migrate_add_lines_columns.py
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("Please set DATABASE_URL environment variable (same one used by your app) and re-run.")
    raise SystemExit(1)

print("Connecting to database...")
conn = psycopg2.connect(DATABASE_URL)
try:
    with conn.cursor() as c:
        print("Adding columns if not present...")
        c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS lines_added INTEGER DEFAULT 0;")
        c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS lines_removed INTEGER DEFAULT 0;")
        conn.commit()
        print("✅ Migration applied. lines_added and lines_removed are present (or already were).")
except Exception as e:
    print("Migration failed:", e)
    raise
finally:
    conn.close()
