"""
migrate_to_db.py — move sources & keywords OUT of code and INTO the database
=============================================================================
Run this ONCE to populate the database from your existing collector + Excel.
After this, sources and keywords live in the database and are managed through
the app (admin panel), not by editing code.

What it does:
  1. Reads the SOURCES list from collector_logic.py  -> inserts into 'sources'
  2. Reads keywords from Insight_Sources.xlsx         -> inserts into 'keywords'
  3. Creates a default admin user so you can log in later

Safe to re-run: it clears and re-imports, so it always matches your sources.
"""

import json
from pathlib import Path

from database import get_connection, init_db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KEYWORDS_XLSX = DATA_DIR / "Insight_Sources.xlsx"


def migrate_sources():
    """Copy the SOURCES list from collector_logic.py into the 'sources' table.
    Each source's method-specific keys (url, urls, combine, fallbacks...) are
    bundled into the JSON 'config' column."""
    # load the SOURCES list from the collector without running it
    code = open(Path(__file__).parent / "collector_logic.py").read()
    ns = {}
    exec(code.split("if __name__")[0], ns)
    SOURCES = ns["SOURCES"]

    conn = get_connection()
    conn.execute("DELETE FROM sources")  # clear, so re-running stays in sync

    for s in SOURCES:
        name = s["name"]
        method = s["method"]
        # everything except name/method goes into config as JSON
        config = {k: v for k, v in s.items() if k not in ("name", "method")}
        # guess a language tag (purely informational for now)
        lang = "en" if name in (
            "CNN", "Al Jazeera", "Daily Star", "Dhaka Tribune",
            "The Business Standard", "BD News 24", "Prothom Alo English", "BSS"
        ) else "bn"
        conn.execute(
            "INSERT INTO sources (name, method, config, language, enabled) "
            "VALUES (?,?,?,?,1)",
            (name, method, json.dumps(config), lang)
        )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    conn.close()
    print(f"  Migrated {n} sources")


def migrate_keywords():
    """Copy keywords from the Excel bilingual sheet into the 'keywords' table.
    Each row becomes: section, English term, Bengali term(s)."""
    import openpyxl
    conn = get_connection()
    conn.execute("DELETE FROM keywords")

    wb = openpyxl.load_workbook(KEYWORDS_XLSX, read_only=True, data_only=True)
    sheet = "Key Words Bilingual" if "Key Words Bilingual" in wb.sheetnames else "Key Words"
    ws = wb[sheet]
    bilingual = (sheet == "Key Words Bilingual")

    count = 0
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c is not None]
        if len(cells) < 2:
            continue
        if bilingual:
            section = str(cells[0]).strip()
            term_en = str(cells[1]).strip() if len(cells) > 1 else ""
            term_bn = str(cells[2]).strip() if len(cells) > 2 else ""
            if section.lower().startswith("report") or term_en.lower() == "keyword (english)":
                continue
        else:
            section = str(cells[-2]).strip()
            term_en = str(cells[-1]).strip()
            term_bn = ""
            if section.lower().startswith("report") or term_en.lower() == "keywords":
                continue
        if not section or (not term_en and not term_bn):
            continue
        conn.execute(
            "INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (?,?,?,1)",
            (section, term_en, term_bn)
        )
        count += 1
    conn.commit()
    conn.close()
    print(f"  Migrated {count} keywords")


def create_default_admin():
    """Create a default admin login if no users exist yet.
    Username: admin   Password: changeme   (CHANGE THIS after first login!)
    Password hashing is done by the auth module; we import it here."""
    from auth import hash_password
    conn = get_connection()
    existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("admin", hash_password("changeme"), "admin")
        )
        conn.commit()
        print("  Created default admin (username: admin, password: changeme)")
        print("  *** CHANGE THIS PASSWORD after first login ***")
    else:
        print(f"  Users already exist ({existing}) — skipping admin creation")
    conn.close()


if __name__ == "__main__":
    print("Setting up database...")
    init_db()
    print("Migrating data from code/Excel into the database:")
    migrate_sources()
    migrate_keywords()
    create_default_admin()
    print("\nMigration complete. Sources and keywords now live in the database.")
