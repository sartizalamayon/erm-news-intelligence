"""
clean_titles.py — one-time cleanup of malformed stored titles
==============================================================
Some sources (e.g. Daily Star) were fetched with HTML wrapped around the
headline (a raw <a href> tag), which renders as an ugly blue link and exports
to Excel wrong. This script strips HTML from all stored article titles.

Safe to run anytime; only rewrites titles that contain markup.

    python clean_titles.py
"""

import re
from database import get_connection
import collector_logic as clog

conn = get_connection()
rows = conn.execute("SELECT id, title FROM articles").fetchall()
fixed = 0
for r in rows:
    original = r["title"] or ""
    cleaned = clog.clean_title(original)
    if cleaned != original:
        conn.execute("UPDATE articles SET title=? WHERE id=?", (cleaned, r["id"]))
        fixed += 1
conn.commit()
conn.close()
print(f"Cleaned {fixed} titles out of {len(rows)} articles.")
