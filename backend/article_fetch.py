"""
article_fetch.py — fetch and extract readable article body text.

Used ONLY by the report generator (report.py), and only for the small number
of APPROVED articles going into a report (not during collection, which stays
headline-only and unchanged). Fetching ~10-30 pages at report time is a much
smaller, more contained cost than fetching hundreds during collection.

Extraction is best-effort: strips nav/ads/scripts/boilerplate using simple
heuristics (largest cluster of <p> text). Site structures vary, so quality
varies by source — this is expected. On any failure, returns "" and the
caller falls back to headline-only for that one article; a report is never
blocked by one bad fetch.
"""
import re

try:
    import requests
    from bs4 import BeautifulSoup
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}
TIMEOUT = 12
MAX_CHARS = 4000   # cap what we feed the LLM per article — plenty for a bullet


def fetch_article_text(url: str) -> str:
    """Fetch a URL and return its main readable text, best-effort.
    Returns "" on ANY failure (network, parsing, blocked, paywall, etc.) —
    callers must handle the empty-string fallback themselves."""
    if not _AVAILABLE or not url:
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200 or not r.text:
            return ""
        soup = BeautifulSoup(r.text, "lxml")

        # drop obviously non-article elements before extracting text
        for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                         "form", "iframe", "noscript"]):
            tag.decompose()

        # heuristic: the article body is usually the container with the most
        # cumulative <p> text. Try common article wrappers first, then fall
        # back to scanning all containers.
        candidates = soup.select(
            "article, [class*=article-body], [class*=article-content], "
            "[class*=story-content], [class*=post-content], [itemprop=articleBody]")
        best_text = ""
        for c in candidates:
            paras = c.find_all("p")
            text = " ".join(p.get_text(" ", strip=True) for p in paras)
            if len(text) > len(best_text):
                best_text = text

        if not best_text:
            # fallback: every <p> on the page, filtered for short junk lines
            paras = soup.find_all("p")
            lines = [p.get_text(" ", strip=True) for p in paras]
            lines = [l for l in lines if len(l) > 40]  # drop nav/caption fragments
            best_text = " ".join(lines)

        best_text = re.sub(r"\s+", " ", best_text).strip()
        return best_text[:MAX_CHARS]
    except Exception:
        return ""
