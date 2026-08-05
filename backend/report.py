"""
report.py — generate the Daily Risk Insights Word document from approved articles.

Runs on the user's machine via python-docx. The LLM drafts each approved article
into the house "**Bold hook** – interpretation" bullet style (the user polishes
before circulating). Articles are grouped into fixed report sections, a manual
fuel-price line sits under Macroeconomic, and the result is a .docx the user
downloads.
"""
from datetime import date
from io import BytesIO

# fixed section order + emoji, matching the user's manual brief
SECTION_ORDER = [
    ("Geopolitical", "\U0001F30D"),            # 🌍
    ("Macroeconomic Outlook", "\U0001F4C8"),    # 📈
    ("Trade & Business", "\U0001F4BC"),         # 💼
    ("Telecom", "\U0001F4F1"),                  # 📱
    ("Budget", "\U0001F9FE"),                   # 🧾
    ("Political", "\U0001F3DB"),                # 🏛
    ("Weather & Environmental", "\U0001F329"),  # 🌩
]

# map the keyword-sheet 'Report Section' values + llm sections → report buckets
SECTION_MAP = {
    "geopolitical": "Geopolitical",
    "macroeconomy": "Macroeconomic Outlook",
    "macroeconomic": "Macroeconomic Outlook",
    "macro": "Macroeconomic Outlook",
    "trade & business": "Trade & Business",
    "trade": "Trade & Business",
    "business": "Trade & Business",
    "telco/regulatory": "Telecom",
    "telco": "Telecom",
    "telecom": "Telecom",
    "budget": "Budget",
    "political": "Political",
    "others": "Trade & Business",   # default bucket for misc
    "weather": "Weather & Environmental",
    "environment": "Weather & Environmental",
}


def _bucket_for(sections_field):
    """Pick the report bucket from an article's stored 'sections' string."""
    if not sections_field:
        return "Trade & Business"
    first = sections_field.split(",")[0].strip().lower()
    return SECTION_MAP.get(first, "Trade & Business")


def draft_bullets(articles, provider, progress=None):
    """Ask the LLM to rewrite approved articles into house-style bullets,
    using the FULL ARTICLE TEXT (fetched fresh for each approved article) so
    bullets can cite real numbers/figures — not just the headline, which a
    prior version relied on. Falls back to headline-only for any article
    whose fetch/extraction fails, so one bad source never blocks the report —
    but that article's title+source+url is recorded in `failed` so the report
    can tell the user which ones it couldn't fetch.
    `progress(i, total)` is called after each article, if provided.
    Returns (drafted, failed) where drafted = {report_section: [bullet, ...]}
    and failed = [{"title","source","url"}, ...] (fetch failures only)."""
    import article_fetch
    from collections import defaultdict
    buckets = defaultdict(list)
    for a in articles:
        buckets[_bucket_for(a.get("sections", ""))].append(a)

    total = len(articles)
    done = 0
    drafted = defaultdict(list)
    failed = []
    for section, items in buckets.items():
        for a in items:
            title = a.get("title", "")
            url = a.get("url", "")
            # prefer stored body text from DB; only fetch live if missing
            body = a.get("content_text", "") or ""
            if not body:
                body = article_fetch.fetch_article_text(url)  # "" on any failure
            if not body:
                failed.append({"title": title, "source": a.get("source", ""), "url": url})
            bullet = None
            if provider and getattr(provider, "enabled", False) \
                    and hasattr(provider, "draft_bullet"):
                try:
                    bullet = provider.draft_bullet(title, body, section)
                except Exception:
                    bullet = None
            if not bullet:
                # fallback: no LLM, or fetch+LLM both failed — headline only
                bullet = title
            drafted[section].append(bullet)
            done += 1
            if progress:
                try:
                    progress(done, total)
                except Exception:
                    pass
    return drafted, failed


def build_docx(drafted, fuel_line="", report_date=None, failed=None):
    """Create the .docx in memory and return its bytes.
    `failed` (optional): list of {"title","source","url"} for approved
    articles whose full text couldn't be fetched — those bullets were drafted
    from the headline only, and this list is noted at the end of the report
    so the user knows which ones to double-check/fill in by hand."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    GP_NAVY = RGBColor(0x0A, 0x25, 0x40)
    GP_CYAN = RGBColor(0x00, 0x8C, 0xBA)
    WARN = RGBColor(0xB0, 0x5A, 0x2A)

    rd = report_date or date.today().strftime("%d %B %Y")
    doc = Document()

    # base font
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # title
    h = doc.add_paragraph()
    run = h.add_run(f"Daily Risk Insights | {rd}")
    run.bold = True
    run.font.size = Pt(18)
    run.font.color.rgb = GP_NAVY

    # divider
    hr = doc.add_paragraph()
    hr_run = hr.add_run("_" * 60)
    hr_run.font.color.rgb = GP_CYAN

    for section, emoji in SECTION_ORDER:
        bullets = drafted.get(section, [])
        # always show Macroeconomic (for the fuel line); skip other empty sections
        if not bullets and section != "Macroeconomic Outlook":
            continue
        # section header
        sh = doc.add_paragraph()
        sh.space_before = Pt(10)
        sh_run = sh.add_run(f"{emoji} {section}")
        sh_run.bold = True
        sh_run.font.size = Pt(14)
        sh_run.font.color.rgb = GP_NAVY

        # fuel-price standing line under Macroeconomic
        if section == "Macroeconomic Outlook" and fuel_line:
            fp = doc.add_paragraph()
            fp_run = fp.add_run(fuel_line)
            fp_run.bold = True
            fp_run.font.color.rgb = GP_CYAN

        for b in bullets:
            p = doc.add_paragraph(style="List Bullet")
            # if the bullet has a "Hook – detail" shape, bold the hook
            if " \u2013 " in b:
                hook, _, rest = b.partition(" \u2013 ")
                r1 = p.add_run(hook)
                r1.bold = True
                p.add_run(" \u2013 " + rest)
            elif " - " in b and b.index(" - ") < 60:
                hook, _, rest = b.partition(" - ")
                r1 = p.add_run(hook)
                r1.bold = True
                p.add_run(" \u2013 " + rest)
            else:
                p.add_run(b)

    # note which approved articles couldn't be fetched — those bullets were
    # drafted from the headline alone and may lack specific figures
    if failed:
        doc.add_paragraph()
        note = doc.add_paragraph()
        note_run = note.add_run(
            f"\u26A0 Could not fetch full article text for {len(failed)} "
            f"approved item(s) below — their bullets were drafted from the "
            f"headline only and may be missing specific figures:")
        note_run.bold = True
        note_run.font.size = Pt(9.5)
        note_run.font.color.rgb = WARN
        for f in failed:
            fp = doc.add_paragraph(style="List Bullet")
            fr = fp.add_run(f"{f['title']} ({f['source']})")
            fr.font.size = Pt(9.5)
            fr.font.color.rgb = WARN

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def generate_from_drafted(drafted, fuel_line="", report_date=None, failed=None):
    """Build the docx from an ALREADY-drafted bullets dict (e.g. reused from a
    prior preview, so download matches what the user saw — no re-drafting,
    no extra LLM calls, and no risk of the two diverging)."""
    data = build_docx(drafted, fuel_line=fuel_line, report_date=report_date, failed=failed)
    fname = f"Daily_Risk_Insights_{date.today().isoformat()}.docx"
    return data, fname


def generate(articles, provider, fuel_line="", report_date=None, progress=None):
    """Top-level: draft bullets (fetching full article text per approved
    article) + build the docx. Returns (bytes, filename)."""
    drafted, failed = draft_bullets(articles, provider, progress=progress)
    return generate_from_drafted(drafted, fuel_line=fuel_line, report_date=report_date,
                                  failed=failed)


def preview(articles, provider, fuel_line="", report_date=None, progress=None):
    """Draft bullets WITHOUT building the docx — for rendering an in-page
    preview before the user downloads. Returns (display_dict, drafted_raw,
    failed) — drafted_raw+failed are cached by the caller so a later download
    reuses them instead of re-drafting (avoids double LLM calls and
    preview/download drift). `failed` = approved articles whose full text
    couldn't be fetched (bullets for these were drafted from headline only)."""
    drafted, failed = draft_bullets(articles, provider, progress=progress)
    out = []
    for section, emoji in SECTION_ORDER:
        bullets = drafted.get(section, [])
        if not bullets and section != "Macroeconomic Outlook":
            continue
        out.append({
            "section": section,
            "emoji": emoji,
            "fuel_line": fuel_line if section == "Macroeconomic Outlook" else "",
            "bullets": bullets,
        })
    rd = report_date or date.today().strftime("%d %B %Y")
    return {"date": rd, "sections": out, "failed": failed}, drafted, failed
