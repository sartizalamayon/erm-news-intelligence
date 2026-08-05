"""
ERM Daily Risk Intelligence — News Collector v4
================================================
30 sources using the best available method per site: JSON API where the site
exposes one (Prothom Alo Quintype, Financial Express), WordPress REST API
(Nagorik, Times of Bangladesh), per-date sitemaps (Al Jazeera, Bonik Barta,
Naya Diganta), sitemap-index merging (Rupali BD), numeric-ID enumeration
(Daily Star, Sangbad, New Age, Arthosuchak), BBC sitemap-index scoping,
and news-sitemaps/RSS elsewhere.

USAGE
-----
    python news_collector.py              # fetch + filter → risk_intel_<date>.xlsx
    python news_collector.py --report     # rebuild today's Excel from DB only
    python news_collector.py --verify     # test every source URL, print counts

Keep Insight_Sources.xlsx in the same folder.
Database (news.db) accumulates across runs — run several times a day.
"""

import argparse, re, sqlite3, time
from datetime import datetime, date, timezone, timedelta
from urllib.parse import urlparse

import feedparser, requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from email.utils import parsedate_to_datetime


def clean_title(raw):
    """Normalise a headline: strip any HTML tags/entities and collapse
    whitespace. Some feeds (e.g. Daily Star) wrap titles in HTML or CDATA,
    which would otherwise render as raw <a> links in the UI. Always returns
    plain text."""
    if not raw:
        return ""
    text = str(raw)
    if "<" in text and ">" in text:
        # contains markup — extract just the text
        try:
            text = BeautifulSoup(text, "html.parser").get_text(" ")
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    # unescape common entities and collapse whitespace
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#039;", "'").replace("&quot;", '"').replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", text).strip()

# ── CONFIG ────────────────────────────────────────────────────────────────────
KEYWORDS_XLSX  = "Insight_Sources.xlsx"
KEYWORDS_SHEET = "Key Words Bilingual"   # English + Bengali; falls back to "Key Words"
DB_FILE        = "news.db"
HEADERS        = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
    "Connection": "keep-alive",
}
TIMEOUT        = 10     # per-request seconds (was 15)
DHAKA          = timezone(timedelta(hours=6))
WINDOW         = timedelta(hours=30)    # rolling "today" window
NOW            = datetime.now(DHAKA)

# ── SOURCES ───────────────────────────────────────────────────────────────────
# Two groups:
#   "sitemap" → news-sitemap XML (more complete; has <news:title> + date)
#   "rss"     → RSS feed (confirmed today-count ≥ homepage link count)
#
# For the 5 sitemap sources marked [*], the probe confirmed a sitemap exists
# but the exact URL was reconstructed from standard patterns. If any of these
# return 0 articles on first run, use --verify to see what's available.
#
SOURCES = [

    # ── news-sitemap: probe confirmed URL directly ───────────────────────────

    {"name": "Dhaka Post",
     "method": "sitemap",
     "url":   "https://www.dhakapost.com/sitemaps/news-sitemap.xml"},

    {"name": "Dhaka Mail",
     "method": "sitemap",
     "url":   "https://dhakamail.com/sitemaps/news-sitemap.xml"},

    # Rupali BD: the single news-sitemap misses some sections. Merging all 29
    # per-section sitemaps from the sitemap index gives complete coverage.
    # Falls back to the news-sitemap if the index fails.
    {"name": "Rupali BD",
     "method": "sitemap_index_merge",
     "url":   "https://www.rupalibangladesh.com/sitemap.xml",
     "fallback_sitemaps": ["https://www.rupalibangladesh.com/sitemaps/news-sitemap.xml"]},

    {"name": "BD News 24",
     "method": "sitemap",
     "url":   "https://bdnews24.com/news_sitemap.xml",
     # confirmed working RSS (from OPML export) as fallback
     "fallback_rss": "https://bdnews24.com/?widgetName=rssfeed&widgetId=1150&getXmlFeed=true"},

    # CNN: /sitemap/news.xml is a curated ~88/day top-stories set. The full
    # union of per-section sitemaps needs a custom index parser. news.xml is
    # reliable and covers the major stories relevant to ERM.
    {"name": "CNN",
     "method": "sitemap",
     "url":   "https://www.cnn.com/sitemap/news.xml"},

    {"name": "Channels",
     "method": "sitemap",
     "url":   "https://channels.com.bd/sitemap-news.xml"},

    # ── Prothom Alo: Quintype internal API is strictly better than the news
    #    sitemap — returns full article body inline (no second fetch), reliable
    #    UTC epoch timestamps, and offset pagination up to 10k items. The sitemap
    #    is a partial view. API: /api/v1/advanced-search?sort=latest-published
    {"name": "Prothom Alo",
     "method": "api",
     "url":   "https://www.prothomalo.com/api/v1/advanced-search?sort=latest-published&limit=200&fields=headline,slug,url,published-at,first-published-at",
     "items_path": "items",
     "title_field": "headline",
     "url_field": "url",
     "date_field": "published-at",
     "base_url": "https://www.prothomalo.com",
     "paginate": "offset",
     "offset_step": 200,
     "max_pages": 20,
     "fallback_rss": "https://www.prothomalo.com/feed"},

    # English edition of Prothom Alo — same Quintype API, different host.
    {"name": "Prothom Alo English",
     "method": "api",
     "url":   "https://en.prothomalo.com/api/v1/advanced-search?sort=latest-published&limit=200&fields=headline,slug,url,published-at,first-published-at",
     "items_path": "items",
     "title_field": "headline",
     "url_field": "url",
     "date_field": "published-at",
     "base_url": "https://en.prothomalo.com",
     "paginate": "offset",
     "offset_step": 200,
     "max_pages": 20,
     "fallback_rss": "https://en.prothomalo.com/feed"},

    {"name": "The Business Standard",
     "method": "sitemap",
     "url":   "https://www.tbsnews.net/sitemap-news.xml",
     "force_gnews": True},
     # RSS is empty for TBS; iter_sitemap auto-resolves the real sitemap path
     # via robots.txt + common patterns (probe confirmed ~216 articles/day).

    {"name": "Desh Rupantor",
     "method": "sitemap",
     "url":   "https://www.deshrupantor.com/sitemaps/news-sitemap.xml",
     "fallback_rss": "https://www.deshrupantor.com/feed/"},

    {"name": "Ajker Patrika",
     "method": "sitemap",
     "url":   "https://www.ajkerpatrika.com/sitemaps/news-sitemap.xml",
     "fallback_rss": "https://www.ajkerpatrika.com/feed"},

    # Al Jazeera: per-date sitemaps are COMPLETE for the target day and don't
    # depend on a 2-day rolling window. /sitemaps/article-new/{DD-MM-YYYY}.xml
    # verified identical to the rolling news.xml (54==54 for one day).
    {"name": "Al Jazeera",
     "method": "aljazeera_dated",
     "url":   "https://www.aljazeera.com/sitemaps/article-new/",
     "fallback_rss": "https://www.aljazeera.com/xml/rss/all.xml"},

    # ── RSS: today-count = total AND exceeds homepage link count ─────────────
    # ── BD Pratidin: its own RSS feed is now CAPTCHA-protected (bot-blocked),
    #    so we read it via Google News instead — a legitimate, dated subset.
    {"name": "BD Pratidin",
     "method": "gnews",
     "url":   "bd-pratidin.com"},

    # Sangbad: RSS at /rss.xml is stale. The working method is numeric-ID enum:
    # articles at /news/{id} with sequential publish-ordered IDs. JSON-LD
    # datePublished gives the date. Canonical URL pointing to homepage = not an
    # article. datePublished is Dhaka wall clock written as +00:00 (bogus offset).
    {"name": "Sangbad",
     "method": "id_enum",
     "site_url": "https://sangbad.net",
     "article_pattern": "/news/{id}",
     "id_source": "homepage",
     "id_regex": r'/news/(\d+)',
     "date_source": "jsonld",
     "date_tz_override": "dhaka",
     "not_article_check": "canonical_is_homepage",
     "fallback_rss": "https://sangbad.net/rss.xml"},


    {"name": "Dhaka Tribune",
     "method": "rss",
     "url":   "https://www.dhakatribune.com/feed/"},

    {"name": "Independent TV",
     "method": "rss",
     "url":   "https://www.itvbd.com/feed/"},

    {"name": "Ekattor TV",
     "method": "rss",
     "url":   "https://ekattor.tv/feed/"},

    # ── BSS (Bangladesh Sangbad Sangstha) — state news agency, English.
    #    Its sitemap has 1045 URLs but NO usable dates, so a direct sitemap
    #    pull can't filter to today. Routed via Google News RSS, which supplies
    #    proper dates and recent items. Coverage is a subset, not the full wire.
    # ── BSS (state news agency, English). Sitemap lists ~600 URLs but with NO
    #    dates (confirmed: URLs are numeric IDs like /js-session/396257). To get
    #    full dated coverage we fetch each article page for its date, walking the
    #    sitemap newest-first and stopping early once we pass the time window.
    #    Slow (one fetch per article) but complete. Falls back to Google News.
    {"name": "BSS",
     "method": "page_dates",
     "url":   "https://www.bssnews.net/sitemap-news.xml",
     "fallback_gnews": "bssnews.net"},

    # ── BBC News: sitemap index at /sitemaps/https-index-com-news.xml points
    #    to 3 sub-sitemaps covering the recent ~2.5 day news window across all
    #    BBC services. Scoped to /news/ articles only (excludes /sport/, /hindi/,
    #    /arabic/ etc). Verified 100% coverage for English BBC News.
    {"name": "BBC News",
     "method": "bbc_sitemaps",
     "url":   "https://www.bbc.com/sitemaps/https-index-com-news.xml"},

    # ── New Age (newagebd.net): English BD daily. Custom CMS with no usable
    #    feed/sitemap. ID-enum needs live page testing to tune the date parser.
    #    Using Google News for reliable dated coverage until then.
    {"name": "New Age",
     "method": "gnews",
     "url":   "newagebd.net"},

    # ── Borderline: RSS 15 today / 21 homepage (71%) — worth including ────────
    {"name": "Shomoyer Alo",
     "method": "rss",
     "url":   "https://www.shomoyeralo.com/rss"},

    # ── NDTV: India's largest English news. Cloudflare blocks sitemaps/RSS on
    #    the main domain, but FeedBurner mirrors are open. 4 feeds merged give
    #    broad coverage. Recent-window only (FeedBurner has no archive).
    {"name": "NDTV",
     "method": "rss_multi",
     "urls": [
         "https://feeds.feedburner.com/ndtvnews-latest",
         "https://feeds.feedburner.com/ndtvnews-top-stories",
         "https://feeds.feedburner.com/ndtvnews-india-news",
         "https://feeds.feedburner.com/ndtvnews-world-news",
     ]},

    # ── Bonik Barta (bonikbarta.com): Bengali business/economy daily.
    #    Dated sitemaps need cursor pagination + per-article fetch for dates.
    #    Using Google News for reliable dated coverage.
    {"name": "Bonik Barta",
     "method": "gnews",
     "url":   "bonikbarta.com"},

    # ── Naya Diganta (dailynayadiganta.com): Bengali opposition daily.
    #    Sitemap index lists dated daily files at /sitemap/YYYY-MM-DD.xml.
    #    Each dated file is the full article set for that day.
    {"name": "Naya Diganta",
     "method": "dated_sitemap",
     "url":   "https://dailynayadiganta.com/sitemap/{date}.xml",
     "date_format": "%Y-%m-%d"},

    # ── Daily Inqilab (dailyinqilab.com): Bengali conservative daily.
    #    Google News sitemap at /news-sitemap.xml, standard format.
    {"name": "Daily Inqilab",
     "method": "sitemap",
     "url":   "https://dailyinqilab.com/news-sitemap.xml"},

    # ── Arthosuchak (arthosuchak.com): Bengali finance/economy.
    #    WP REST is locked (401). Articles at /archives/{id}/ with sequential
    #    IDs. JSON-LD datePublished in Dhaka wall clock. Some IDs 301-redirect
    #    to a nearby canonical (duplicates) — don't follow redirects.
    {"name": "Arthosuchak",
     "method": "id_enum",
     "site_url": "https://www.arthosuchak.com",
     "article_pattern": "/archives/{id}/",
     "id_source": "homepage",
     "id_regex": r'/archives/(\d+)',
     "date_source": "jsonld",
     "date_tz_override": "dhaka",
     "not_article_check": "no_time_element",
     "no_follow_redirects": True},

    # ── Nagorik (nagorik.com): Bengali general daily. WordPress REST API open.
    {"name": "Nagorik",
     "method": "wp_rest",
     "site_url": "https://nagorik.com"},

    # ── Times of Bangladesh (tob.news): English daily. WordPress REST API open.
    {"name": "Times of Bangladesh",
     "method": "wp_rest",
     "site_url": "https://tob.news"},

    # ── Economy Post (economipost.com): Bengali business/economy.
    #    WP REST is WAF-blocked, RSS /feed/ gives only 10 items (no pagination
    #    in iter_rss). Using Google News for broader daily coverage.
    {"name": "Economy Post",
     "method": "gnews",
     "url":   "economipost.com"},

    # ══════════════════════════════════════════════════════════════════════
    # ADDITIONAL SOURCES (batch 2) — ERM-relevant + tech + local dailies.
    # WP REST where the site exposes it; Google News for sites whose own
    # feed/sitemap is proxy-walled, AJAX-based, or absent. All run exhaustively
    # (their method + sitemap + RSS + Google News, deduped).
    # ══════════════════════════════════════════════════════════════════════

    # ── ERM-relevant ──
    {"name": "Bangladesh Post", "method": "wp_rest",
     "site_url": "https://bangladeshpost.net", "fallback_gnews": "bangladeshpost.net"},
    {"name": "News24 BD", "method": "gnews", "url": "news24bd.tv"},
    {"name": "Channel i Online", "method": "wp_rest",
     "site_url": "https://www.channelionline.com"},
    # ShareBiz / ShareBusiness24: POST AJAX archive (no API/sitemap/gnews presence)
    {"name": "ShareBiz", "method": "ajax_archive",
     "site_url": "https://www.sharebiz.net",
     "ajax_path": "/ajax_archive_load_more.php", "art_prefix": "/daily"},
    {"name": "ShareBusiness24", "method": "ajax_archive",
     "site_url": "https://www.sharebusiness24.com",
     "ajax_path": "/ajax_archive_load_more.php", "art_prefix": "/stock-market"},
    {"name": "Deshersangbad", "method": "wp_rest",
     "site_url": "https://deshersangbad.com"},

    # ── Tech / ICT ──
    {"name": "Bangla Tech 24", "method": "wp_rest",
     "site_url": "https://banglatech24.com"},
    # Daily ICT News: custom CMS, articles at bare /{id}. Date is BENGALI text
    # ("● ১৮ জুন ২০২৬") — no JSON-LD. Non-articles have no Bengali date, so the
    # date-parse failure itself rejects them.
    {"name": "Daily ICT News", "method": "id_enum",
     "site_url": "https://www.dailyictnews.com",
     "article_pattern": "/{id}",
     "id_source": "homepage",
     "id_regex": r'(?:dailyictnews\.com)?/(\d{4,})',
     "date_source": "bengali_date",
     "not_article_check": "none"},
    # InfoTech: id-enum, section-agnostic /news/news/{id}, time-first JSON-LD date
    {"name": "InfoTech", "method": "id_enum",
     "site_url": "https://www.infotechinsight.com",
     "article_pattern": "/news/news/{id}",
     "id_source": "homepage",
     "id_regex": r'/news/(\d+)',
     "date_source": "jsonld_timefirst",
     "not_article_check": "no_jsonld"},
    {"name": "Tech Shohor", "method": "gnews", "url": "techshohor.com"},
    {"name": "TechWorld", "method": "gnews", "url": "techworldbd.com"},
    # DigiBangla: REST blocked, and the sitemap is unusable (undated, unordered,
    # littered with tag/junk pages). gnews only; probes disabled so the junk
    # sitemap can't pollute the pool. Zero on quiet days is expected.
    {"name": "DigiBangla", "method": "gnews", "url": "digibanglatech.news",
     "disable_methods": ["sitemap", "rss"]},

    # ── Local / general dailies ──
    {"name": "Desh Protikhon", "method": "wp_rest",
     "site_url": "https://deshprotikhon.com"},
    # Deshpatrika: id-enum, /details.php?news_id={id}, DD-MM-YYYY calendar date
    {"name": "Deshpatrika", "method": "id_enum",
     "site_url": "https://deshpatrika24.com",
     "article_pattern": "/details.php?news_id={id}",
     "id_source": "homepage",
     "id_regex": r'details\.php\?news_id=(\d+)',
     "date_source": "calendar_dmy",
     "not_article_check": "none"},
    {"name": "Amarbarta", "method": "amarbarta_archive",
     "site_url": "https://www.amarbarta.com",
     "ajax_path": "/templates/web-view/archive_page/more_archive_ajax.php"},
    # Daily Adin: Next.js site, no feed, no gnews presence. Articles reachable
    # via the /news/{id} shell which carries a real-UTC JSON-LD datePublished
    # ("...Z"). IDs are sequential and section-agnostic.
    {"name": "Daily Adin", "method": "id_enum",
     "site_url": "https://dailyadin.com",
     "article_pattern": "/news/{id}",
     "id_source": "homepage",
     "id_regex": r'/(\d{4,})(?:["/?#]|$)',
     "date_source": "jsonld",
     "canonical_hop": True,
     "not_article_check": "none"},
    # Somoy Journal: id-enum, section-agnostic /news-post/{id}/- (redirects to
    # the slugged URL). Date is BENGALI text in div.mb-3 — no JSON-LD.
    {"name": "Somoy Journal", "method": "id_enum",
     "site_url": "https://somoyjournal.com",
     "article_pattern": "/news-post/{id}/-",
     "id_source": "homepage",
     "id_regex": r'/news-post/(\d+)',
     "date_source": "bengali_date",
     "not_article_check": "none"},

    # ══ Cloudflare-TLS sources (fetched via system curl — no proxy needed) ══
    # Python's bundled SSL can't complete the TLS 1.3 handshake these Cloudflare
    # edges require (403), but system curl and real browsers can. curl_sitemap is
    # a pure transport workaround, not challenge-solving.
    {"name": "Kalbela", "method": "curl_sitemap",
     "url": "https://www.kalbela.com/sitemap/news-sitemap.xml"},
    {"name": "Daily Sun", "method": "curl_sitemap",
     "url": "https://www.daily-sun.com/news_sitemap.xml"},

    # Daily Sangram: sitemap index lists one dated file per day (/sitemap/YYYY-MM-DD.xml),
    # keyed by PUBLISH date. Reachable with plain requests.
    {"name": "Daily Sangram", "method": "dated_sitemap",
     "url": "https://dailysangram.com/sitemap/{date}.xml",
     "date_format": "%Y-%m-%d"},

    # ── Daily Star: Drupal site with NO usable feed (sitemap is 83 pages of
    #    priority-ordered chaos, RSS caps at 10 per section, /todays-news is a
    #    curated ~43-item subset). Solution: descending node-ID walk. Every node
    #    is at /node/{id} (302→article). IDs are dense (~1 article per 5 ids).
    #    Walk is complete by construction — no section can be missed.
    {"name": "Daily Star",
     "method": "daily_star_nodes",
     "site_url": "https://www.thedailystar.net",
     "fallback_gnews": "thedailystar.net",
     "force_gnews": True},
]

# ── KEYWORDS ──────────────────────────────────────────────────────────────────
def load_keywords():
    """Load keywords grouped by section. Reads the bilingual sheet if present
    (English + Bengali columns), otherwise the plain 'Key Words' sheet.

    Returns: {section: [(term, label), ...]} where `term` is what we search for
    (English or Bengali) and `label` is the English keyword shown in reports —
    so a Bengali match still displays a readable English keyword.
    """
    import openpyxl
    try:
        wb = openpyxl.load_workbook(KEYWORDS_XLSX, read_only=True, data_only=True)
    except FileNotFoundError:
        print(f"[WARN] {KEYWORDS_XLSX} not found — using fallback keywords.")
        return _fallback_keywords()
    except Exception as e:
        print(f"[WARN] keyword load failed: {e}")
        return _fallback_keywords()

    sheet = KEYWORDS_SHEET if KEYWORDS_SHEET in wb.sheetnames else "Key Words"
    bilingual = (sheet == "Key Words Bilingual")
    ws = wb[sheet]
    groups = {}
    en_count = bn_count = 0
    for row in ws.iter_rows(values_only=True):
        cells = [c for c in row if c is not None]
        if len(cells) < 2:
            continue

        if bilingual:
            # columns: Section | English | Bengali(comma-separated)
            section = str(cells[0]).strip()
            english = str(cells[1]).strip() if len(cells) > 1 else ""
            bengali = str(cells[2]).strip() if len(cells) > 2 else ""
            if section.lower().startswith("report") or english.lower() == "keyword (english)":
                continue
            if not section or not english:
                continue
            # English term → label is itself
            groups.setdefault(section, []).append((english, english))
            en_count += 1
            # each Bengali synonym → label is the English keyword
            for bn in (b.strip() for b in bengali.split(",")):
                if bn:
                    groups[section].append((bn, english))
                    bn_count += 1
        else:
            section, keyword = str(cells[-2]).strip(), str(cells[-1]).strip()
            if section.lower().startswith("report") or keyword.lower() == "keywords":
                continue
            if section and keyword:
                groups.setdefault(section, []).append((keyword, keyword))
                en_count += 1

    if groups:
        if bilingual:
            print(f"  (keywords: {en_count} English + {bn_count} Bengali terms)")
        return groups
    return _fallback_keywords()

def _fallback_keywords():
    return {
        "Telco/regulatory": [("Grameenphone", "Grameenphone"), ("BTRC", "BTRC"),
                             ("spectrum", "spectrum"), ("telecom", "telecom")],
        "Macroeconomy":     [("inflation", "inflation"), ("remittance", "remittance"),
                             ("imf", "imf"), ("forex", "forex")],
    }

def _is_bengali(s):
    """True if the string contains Bengali script characters."""
    return any("\u0980" <= ch <= "\u09FF" for ch in s)

def build_patterns(groups):
    """Compile one regex per term. English/ASCII terms use \\b word boundaries;
    Bengali terms use plain substring match (Bengali has no ASCII word breaks)."""
    patterns = []
    for sec, terms in groups.items():
        for term, label in terms:
            if not term.strip():
                continue
            if _is_bengali(term):
                # Bengali: substring match, case-insensitivity irrelevant
                pat = re.compile(re.escape(term))
            else:
                # English: whole-word, case-insensitive
                pat = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
            patterns.append((sec, label, pat))
    return patterns

def match_keywords(text, patterns):
    """Return (sorted-sections-str, sorted-keywords-str) or ('', '').
    Keywords are reported by their English label even when a Bengali term hit."""
    secs, kws = set(), set()
    for sec, label, pat in patterns:
        if pat.search(text):
            secs.add(sec); kws.add(label)
    return ", ".join(sorted(secs)), ", ".join(sorted(kws))

# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            url        TEXT PRIMARY KEY,
            source     TEXT,
            title      TEXT,
            published  TEXT,
            fetched_at TEXT,
            day        TEXT,
            sections   TEXT,
            keywords   TEXT,
            is_match   INTEGER
        )""")
    conn.commit()
    return conn

def is_seen(conn, url):
    return conn.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone() is not None

def save(conn, url, source, title, published, sections, keywords):
    is_match = 1 if sections else 0
    conn.execute(
        "INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?)",
        (url, source, title, published, NOW.isoformat(),
         date.today().isoformat(), sections, keywords, is_match))

# ── DATE HANDLING ─────────────────────────────────────────────────────────────
def parse_dt(s):
    if not s:
        return None
    # numeric epoch timestamps (some APIs, e.g. Quintype/Prothom Alo, use these)
    if isinstance(s, (int, float)) or (isinstance(s, str) and s.strip().isdigit()):
        try:
            n = int(s)
            # >1e12 means milliseconds; otherwise seconds
            if n > 1_000_000_000_000:
                n = n / 1000.0
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None
    s = s.strip()
    for fn in [
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        lambda x: parsedate_to_datetime(x),
    ]:
        try:
            return fn(s)
        except Exception:
            pass
    return None

def date_from_url(url):
    """Extract a publication date from a URL path when the sitemap lacks one.
    Handles common news URL patterns like /2026/06/16/ or /2026-06-16/.
    Returns None if the URL has no recognizable date (e.g. numeric-ID URLs)."""
    if not url:
        return None
    # /YYYY/MM/DD/  or  /YYYY-MM-DD/
    m = re.search(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)", url)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None

def is_today(dt):
    """True if dt falls within the rolling WINDOW, None if dt is unknown."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (NOW - dt.astimezone(DHAKA)) <= WINDOW

# ── FETCHERS ──────────────────────────────────────────────────────────────────
LAST_ERROR = {}  # url -> human-readable reason for the most recent failure

def get(url, retries=1, timeout=None, extra_headers=None):
    """Fetch a URL with one retry. Records the failure reason in LAST_ERROR so
    verify mode can tell timeout vs block (403) vs not-found (404) apart.
    Kept deliberately short: a slow/dead source must fail fast, not hang the
    whole run (the sitemap resolver may try several URLs per source).
    `extra_headers` (optional) merges over the default browser headers — used by
    APIs that require Origin/Referer/X-Requested-With etc."""
    import time
    to = timeout or TIMEOUT
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=to)
            if r.status_code == 200:
                LAST_ERROR.pop(url, None)
                return r
            # 429/503 = rate-limited or busy; one short retry only
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5)
                continue
            reason = {403: "403 blocked (bot detection)",
                      404: "404 not found (wrong URL)",
                      429: "429 rate-limited",
                      401: "401 unauthorized"}.get(r.status_code,
                                                   f"HTTP {r.status_code}")
            LAST_ERROR[url] = reason
            return r
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(1.5); continue
            LAST_ERROR[url] = f"timeout (>{to}s)"
            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5); continue
            LAST_ERROR[url] = f"{type(e).__name__}"
            return None
    return None

def iter_api(src):
    """Yield (title, url, datetime|None) from a generic JSON news API.

    Configured via the source's config JSON. Flexible field mapping so it works
    with many APIs without code changes:
      {
        "url": "https://api.example.com/news?token=...",   # the endpoint
        "items_path": "articles",        # key holding the list (optional;
                                         # supports dotted path like "data.items")
        "title_field": "title",          # field with the headline (default 'title')
        "url_field": "url",              # field with the link (default 'url')
        "date_field": "publishedAt",     # field with the date (default 'publishedAt')
        "base_url": "https://example.com",  # prepended to relative url/slug paths
        "headers": {"Origin": "...", "Referer": "..."},  # extra request headers
        "next_field": "nextPageUrl",     # key holding the next-page URL (cursor)
        "max_pages": 10                  # safety cap on pages to follow
      }
    If items_path is omitted, the response is assumed to be a list itself.
    Pagination: if next_field is set, follow the cursor across pages and keep
    fetching until articles fall outside today's window (or max_pages is hit) —
    so a daily brief gets ALL of today's articles, not just the first page.
    """
    cfg = src
    url = cfg.get("url", "")
    if not url:
        return
    headers = cfg.get("headers")
    items_path = cfg.get("items_path", "")
    title_field = cfg.get("title_field", "title")
    url_field = cfg.get("url_field", "url")
    date_field = cfg.get("date_field", "publishedAt")
    base_url = cfg.get("base_url", "").rstrip("/")
    next_field = cfg.get("next_field", "nextPageUrl")
    max_pages = int(cfg.get("max_pages", 10))
    # pagination mode: "cursor" (follow next_field URL) or "offset" (step an
    # offset= query param). offset mode reads page_size from the url's limit.
    paginate = cfg.get("paginate", "cursor")
    offset_step = int(cfg.get("offset_step", 100))

    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    def _with_offset(base, off):
        p = urlparse(base)
        q = parse_qs(p.query)
        q["offset"] = [str(off)]
        return urlunparse((p.scheme, p.netloc, p.path, p.params,
                           urlencode(q, doseq=True), p.fragment))

    page = 0
    offset = 0
    while url and page < max_pages:
        page += 1
        fetch_url = _with_offset(url, offset) if paginate == "offset" else url
        r = get(fetch_url, extra_headers=headers)
        if not r:
            return
        try:
            payload = r.json()
        except Exception:
            return

        # navigate to the list of items
        data = payload
        if items_path:
            for key in items_path.split("."):
                if isinstance(data, dict):
                    data = data.get(key, [])
        if not isinstance(data, list) or not data:
            return

        page_had_today = False
        for item in data:
            if not isinstance(item, dict):
                continue
            title = clean_title(item.get(title_field, ""))
            link = str(item.get(url_field, "")).strip()
            if not title or not link:
                continue
            if base_url and link.startswith("/"):
                link = base_url + link
            dt = None
            raw_date = item.get(date_field, "")
            if raw_date:
                dt = parse_dt(str(raw_date))
            if is_today(dt) is not False:   # today or undated
                page_had_today = True
            yield (title, link, dt)

        # stop paginating once a whole page has no today's articles (we've
        # scrolled past today — newest-first APIs put older items on later pages)
        if not page_had_today:
            return
        # advance to the next page
        if paginate == "offset":
            offset += offset_step
            # url stays the same; offset increments
        else:
            nxt = payload.get(next_field) if isinstance(payload, dict) else None
            url = nxt if nxt else None


def iter_rss(url):
    """Yield (title, url, datetime|None) from an RSS/Atom feed."""
    r = get(url)
    if not r:
        return
    feed = feedparser.parse(r.content)
    for e in feed.entries:
        link = e.get("link", "")
        if not link:
            continue
        title = clean_title(e.get("title", ""))
        tp = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime(*tp[:6], tzinfo=timezone.utc) if tp else None
        yield title, link, dt

def iter_rss_multi(urls):
    """Pull several section feeds and merge them, deduping by article URL.
    Used for sites whose master feed is stale but section feeds are live."""
    seen = set()
    for feed_url in urls:
        for title, link, dt in iter_rss(feed_url):
            if link in seen:
                continue
            seen.add(link)
            yield title, link, dt

def iter_combined(src):
    """Combine MULTIPLE collection methods for one source and merge the results,
    deduping by URL. Use when a single method is known to be PARTIAL — e.g. Daily
    Star, whose section feeds only cover certain sections while its sitemap covers
    all sections. Combining captures articles either method alone would miss.

    Reads src["combine"], a list of method specs, e.g.:
        "combine": [
            {"method": "sitemap",   "url": "...sitemap.xml"},
            {"method": "rss_multi", "urls": ["...rss.xml", ...]},
        ]
    Reports how many UNIQUE articles each method contributed.
    Yields (title, url, dt).
    """
    seen = set()
    contributions = []
    for spec in src.get("combine", []):
        m = spec["method"]
        if m == "sitemap":
            stream = iter_sitemap(spec["url"])
        elif m == "rss_multi":
            stream = iter_rss_multi(spec["urls"])
        elif m == "rss":
            stream = iter_rss(spec["url"])
        elif m == "gnews":
            stream = iter_gnews(spec["url"])
        else:
            continue
        unique = 0
        for title, url, dt in stream:
            if url in seen:
                continue
            seen.add(url)
            unique += 1
            yield title, url, dt
        contributions.append((m, unique))
    # report what each method uniquely added (useful for tuning)
    parts = ", ".join(f"{m}:+{u}" for m, u in contributions)
    print(f"    combined unique contributions -> {parts}")

def iter_gnews(domain):
    """Pull a site's recent articles via Google News RSS search.
    Used for sources whose own sitemap/RSS has no usable dates (e.g. BSS).
    Google News supplies proper publication dates and limits to recent items,
    so the today-window filter works. Coverage is a curated subset, not 100%.

    A short throttle spaces out gnews calls: when many sources fall back to
    gnews in one run, Google News rate-limits and returns empty. The delay
    keeps requests under the throttle threshold."""
    import time as _t
    _t.sleep(1.0)
    from urllib.parse import quote
    q = quote(f"site:{domain} when:2d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-BD&gl=BD&ceid=BD:en"
    yield from iter_rss(url)


# ── NEW COLLECTION METHODS ────────────────────────────────────────────────────

def iter_aljazeera_dated(base_url):
    """Al Jazeera publishes per-date sitemaps at /sitemaps/article-new/DD-MM-YYYY.xml.
    Each is the COMPLETE set of articles first-published on that UTC day. We fetch
    today's and yesterday's to cover the rolling window."""
    for days_back in range(2):
        d = (NOW - timedelta(days=days_back)).astimezone(timezone.utc)
        date_str = d.strftime("%d-%m-%Y")
        url = f"{base_url.rstrip('/')}/{date_str}.xml"
        yield from _sitemap_from_url(url, cap=300)


def iter_dated_sitemap(src):
    """For sites with per-date sitemap files (e.g. Bonik Barta at
    /sitemaps/sitemap-news-YYYY-MM-DD.xml, Naya Diganta at /sitemap/YYYY-MM-DD.xml).
    Fetches today's and yesterday's dated file. The URL template uses {date} as
    placeholder, formatted per src["date_format"]."""
    url_template = src["url"]
    date_fmt = src.get("date_format", "%Y-%m-%d")
    for days_back in range(2):
        d = (NOW - timedelta(days=days_back)).astimezone(DHAKA)
        date_str = d.strftime(date_fmt)
        url = url_template.replace("{date}", date_str)
        yield from _sitemap_from_url(url, cap=500)


def iter_wp_rest(src):
    """WordPress REST API walker. Fetches /wp-json/wp/v2/posts newest-first,
    100 per page, with _embed for categories/author. Stops when articles
    fall outside today's window or max pages reached.

    WP REST returns a bare JSON array. A 400 response means "past the last
    page" (normal end), not a failure. Transient 429/5xx get one retry."""
    site_url = src["site_url"].rstrip("/")
    api_base = f"{site_url}/wp-json/wp/v2/posts"
    max_pages = 20

    for page in range(1, max_pages + 1):
        params = {"orderby": "date", "order": "desc", "per_page": 100,
                  "page": page, "_embed": "1"}
        posts = None
        for attempt in range(2):
            try:
                r = requests.get(api_base, headers={**HEADERS,
                                                    "Accept": "application/json"},
                                 params=params, timeout=20)
            except Exception:
                time.sleep(1.5)
                continue
            if r.status_code == 200:
                try:
                    posts = r.json()
                except Exception:
                    posts = None
                break
            if r.status_code == 400:
                return          # past the last page — normal end
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5)
                continue
            return              # 403/404/etc — API not open
        if not isinstance(posts, list) or not posts:
            return

        page_had_today = False
        for p in posts:
            title_obj = p.get("title", {})
            title = clean_title(title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj))
            link = p.get("link", "")
            date_gmt = p.get("date_gmt", "")
            if not title or not link:
                continue
            dt = parse_dt(date_gmt) if date_gmt else None
            if is_today(dt) is not False:
                page_had_today = True
            yield title, link, dt

        if not page_had_today:
            return


def iter_sitemap_index_merge(index_url, cap_per_child=200):
    """Fetch a sitemap index and union child sitemaps that look news-related.
    Filters to children with 'news' or 'post' in the URL to avoid fetching
    dozens of category/page sitemaps that have no date data."""
    r = get(index_url, timeout=30)
    if not r or r.status_code != 200:
        return
    soup = BeautifulSoup(r.content, "xml")
    if not soup.find("sitemapindex"):
        # not an index, treat as a regular sitemap
        yield from _sitemap_from_url(index_url, cap=600)
        return
    seen = set()
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if not loc:
            continue
        child_url = loc.text.strip()
        # only fetch children that look like news/post sitemaps
        lower = child_url.lower()
        if not any(kw in lower for kw in ("news", "post", "article", "latest")):
            continue
        for title, url, dt in _sitemap_from_url(child_url, cap=cap_per_child, timeout=15):
            if url not in seen:
                seen.add(url)
                yield title, url, dt


def iter_bbc_sitemaps(index_url):
    """BBC News sitemap index points to 3 paginated sub-sitemaps. We fetch all
    sub-sitemaps, scope to /news/ articles (excluding /sport/, language editions,
    /videos/, /av/ audio-video), and yield newest-first."""
    r = get(index_url, timeout=30)
    if not r or r.status_code != 200:
        return
    # extract sub-sitemap URLs
    sub_urls = re.findall(
        r"<loc>(https://www\.bbc\.com/sitemaps/https-sitemap-com-news-\d+\.xml)</loc>",
        r.text)
    if not sub_urls:
        return
    seen = set()
    items = []
    for sub_url in sub_urls:
        for title, url, dt in _sitemap_from_url(sub_url, cap=500, timeout=30):
            # scope to BBC News English articles only
            path = url.split("bbc.com", 1)[-1] if "bbc.com" in url else ""
            if not re.match(r"^/news/(?!av/|videos/)", path):
                continue
            if url not in seen:
                seen.add(url)
                items.append((title, url, dt))
    # sort newest-first for consistent window filtering
    items.sort(key=lambda x: x[2] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    yield from items


def iter_id_enum(src):
    """Generic numeric-ID enumeration for sites where articles live at a
    sequential-ID URL (e.g. /news/{id}). Walks IDs descending from the current
    max, resolves each page's publication date, and yields articles within
    today's window.

    Config keys in src:
      site_url          — base URL (e.g. "https://sangbad.net")
      article_pattern   — URL pattern with {id} placeholder
      id_source         — "homepage" (scrape max ID from homepage links)
      id_regex          — regex to extract IDs from homepage links
      date_source       — "jsonld" or "time_element"
      date_tz_override  — "dhaka" to treat datePublished as Dhaka wall clock
      not_article_check — how to detect non-articles:
                          "canonical_is_homepage" or "no_time_element"
    """
    from concurrent.futures import ThreadPoolExecutor
    import json as _json

    site_url = src["site_url"]
    pattern = src["article_pattern"]
    id_regex = re.compile(src.get("id_regex", r"/(\d{4,})"))
    date_src = src.get("date_source", "jsonld")
    tz_override = src.get("date_tz_override")
    not_article = src.get("not_article_check", "canonical_is_homepage")
    follow_redirects = not src.get("no_follow_redirects", False)
    scan_days_back = 3
    workers = 8
    batch_size = 40

    # 1. Find the current max ID from the homepage
    r = get(site_url, timeout=15)
    if not r or r.status_code != 200:
        print(f"    [id_enum] homepage fetch failed for {site_url}")
        return
    all_ids = [int(m) for m in id_regex.findall(r.text)]
    if not all_ids:
        print(f"    [id_enum] no IDs found on homepage of {site_url}")
        return
    max_id = max(all_ids)
    print(f"    [id_enum] max_id={max_id}, scanning downward...")

    # 2. Resolve one ID: fetch the page, extract date, check if it's an article
    def resolve(nid):
        url = site_url + pattern.replace("{id}", str(nid))
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8,
                                allow_redirects=follow_redirects)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        html = resp.text

        # canonical hop: the probed URL is a shell — the real article data
        # (JSON-LD etc.) lives on the canonical URL. No canonical = not an
        # article (sites like Daily Adin return 200 shells for dead IDs).
        if src.get("canonical_hop"):
            cm = re.search(r'<link rel="canonical" href="(https?://[^"]+)"', html)
            if not cm:
                return None
            canon = cm.group(1)
            if canon.rstrip("/") == site_url.rstrip("/"):
                return None
            try:
                resp = requests.get(canon, headers=HEADERS, timeout=8)
            except Exception:
                return None
            if resp.status_code != 200:
                return None
            html = resp.text

        # not-article checks
        if not_article == "canonical_is_homepage":
            m = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]*)"', html)
            if m:
                canon = m.group(1).rstrip("/")
                if canon == site_url.rstrip("/") or "/news/" not in canon:
                    return None
        elif not_article == "no_time_element":
            if "<time" not in html:
                return None
        elif not_article == "no_jsonld":
            if '"datePublished"' not in html:
                return None
        # "none" → rely on date-parse-failure to reject non-articles

        # extract date
        dt = None
        title = ""
        _MONTHS_L = {"january": 1, "february": 2, "march": 3, "april": 4,
                     "may": 5, "june": 6, "july": 7, "august": 8,
                     "september": 9, "october": 10, "november": 11, "december": 12}
        if date_src == "jsonld":
            # tolerant: matches "2026-06-19T12:38:30Z", "2026-06-19 12:38:30",
            # "2026-06-19T12:38:30+06:00", and date-only "2026-06-19"
            m = re.search(
                r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})'
                r'(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?'
                r'(Z|[+-]\d{2}:?\d{2})?',
                html)
            if m:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hh = int(m.group(4)) if m.group(4) else 12
                mm = int(m.group(5)) if m.group(5) else 0
                ss = int(m.group(6)) if m.group(6) else 0
                offset = m.group(7)
                try:
                    if offset in ("Z", "+00:00", "+0000"):
                        # explicit UTC in the data
                        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
                    elif tz_override == "dhaka" or offset is None:
                        # no offset or forced Dhaka → treat as Dhaka wall clock
                        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=DHAKA)
                    else:
                        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
                except ValueError:
                    pass
            m2 = re.search(r'"headline"\s*:\s*"([^"]{5,})"', html)
            if m2:
                title = clean_title(m2.group(1))
        elif date_src == "jsonld_timefirst":
            # InfoTech: "datePublished":"HH:MM, DD Month YYYY" (Dhaka wall clock)
            m = re.search(
                r'"datePublished"\s*:\s*"(\d{1,2}):(\d{2}),\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})"',
                html)
            if m:
                hh, mm, d, mon_name, y = m.groups()
                mon = _MONTHS_L.get(mon_name.lower())
                if mon:
                    try:
                        dt = datetime(int(y), mon, int(d), int(hh), int(mm), tzinfo=DHAKA)
                    except ValueError:
                        pass
            m2 = re.search(r'"headline"\s*:\s*"([^"]{5,})"', html)
            if m2:
                title = clean_title(m2.group(1))
        elif date_src == "calendar_dmy":
            # Deshpatrika: DD-MM-YYYY somewhere in the page (usually next to a
            # calendar icon, but markup varies). No time → noon Dhaka.
            m = re.search(r'\b(\d{2})-(\d{2})-(\d{4})\b', html)
            if m:
                d, mo, y = map(int, m.groups())
                try:
                    dt = datetime(y, mo, d, 12, 0, tzinfo=DHAKA)
                except ValueError:
                    pass
        elif date_src == "nextdata":
            # Next.js sites: date lives in the __NEXT_DATA__ JSON blob, keys
            # like "created_at"/"published_at"/"date" with ISO values
            m = re.search(
                r'"(?:published_at|created_at|publishedAt|createdAt|date)"\s*:\s*"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})',
                html)
            if m:
                y, mo, d, hh, mm = map(int, m.groups())
                try:
                    tz = DHAKA if tz_override == "dhaka" else timezone.utc
                    dt = datetime(y, mo, d, hh, mm, tzinfo=tz)
                except ValueError:
                    pass
            m2 = re.search(r'"(?:headline|title)"\s*:\s*"([^"]{5,})"', html)
            if m2:
                title = clean_title(m2.group(1))
        elif date_src == "bengali_date":
            # Bengali digits + month name anywhere in page, e.g. "● ১৮ জুন ২০২৬"
            # or "১৮ জুন ২০২৬, ২৩:১৬". No time → noon Dhaka.
            _BN_D = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
            _BN_M = {"জানুয়ারি": 1, "ফেব্রুয়ারি": 2, "মার্চ": 3, "এপ্রিল": 4,
                     "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8,
                     "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12}
            m = re.search(r"([০-৯]{1,2})\s+(জানুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর)\s+([০-৯]{4})", html)
            if m:
                try:
                    day_n = int(m.group(1).translate(_BN_D))
                    mon_n = _BN_M[m.group(2)]
                    year_n = int(m.group(3).translate(_BN_D))
                    dt = datetime(year_n, mon_n, day_n, 12, 0, tzinfo=DHAKA)
                except (ValueError, KeyError):
                    pass
        elif date_src == "time_element":
            # parse <time> text like "DD Month, YYYY, HH:MM"
            m = re.search(r'<time[^>]*>([^<]+)</time>', html)
            if m:
                text = m.group(1).strip()
                dm = re.match(
                    r'(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4}),?\s*(\d{1,2}):(\d{2})',
                    text)
                if dm:
                    mon = _MONTHS_L.get(dm.group(2).lower())
                    if mon:
                        try:
                            dt = datetime(int(dm.group(3)), mon,
                                          int(dm.group(1)),
                                          int(dm.group(4)), int(dm.group(5)),
                                          tzinfo=DHAKA)
                        except ValueError:
                            pass

        if dt is None:
            return None

        # get title from og:title if not already found
        if not title:
            m3 = re.search(r'property="og:title"\s+content="([^"]+)"', html)
            if not m3:
                m3 = re.search(r'content="([^"]+)"\s+property="og:title"', html)
            if m3:
                title = clean_title(m3.group(1))

        return {"id": nid, "title": title, "url": resp.url, "dt": dt}

    # 3. Walk IDs descending in batches with concurrent resolution
    items = []
    newest_dt = None
    nid = max_id
    probed = 0           # total IDs probed
    consec_fail = 0      # consecutive IDs that failed to resolve a date
    MAX_PROBE = 500      # hard ceiling — never probe more than this many IDs
    MAX_CONSEC_FAIL = 60 # if this many in a row fail, the pattern/format is wrong
    TIME_BUDGET = 90     # seconds — hard wall-clock cap per source
    import time as _time
    start_t = _time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while nid > 0 and probed < MAX_PROBE:
            if _time.time() - start_t > TIME_BUDGET:
                print(f"    [id_enum] time budget ({TIME_BUDGET}s) hit, stopping")
                break
            batch = list(range(nid, max(0, nid - batch_size), -1))
            nid -= batch_size
            probed += len(batch)
            batch_had_hit = False
            for res in pool.map(resolve, batch):
                if not res:
                    continue
                batch_had_hit = True
                items.append(res)
                if newest_dt is None or res["dt"] > newest_dt:
                    newest_dt = res["dt"]
            # consecutive-failure breaker: if whole batches keep coming back
            # empty, the article_pattern or date format is wrong — bail fast
            if batch_had_hit:
                consec_fail = 0
            else:
                consec_fail += len(batch)
                if consec_fail >= MAX_CONSEC_FAIL:
                    print(f"    [id_enum] {consec_fail} consecutive misses — "
                          f"pattern/format likely wrong, stopping")
                    break
            # stop when we've accumulated enough old items
            if newest_dt is not None and items:
                cutoff = newest_dt - timedelta(days=scan_days_back)
                old_count = sum(1 for it in items if it["dt"] < cutoff)
                if old_count >= 5:
                    break
            if len(items) > 600:  # safety cap
                break

    print(f"    [id_enum] resolved {len(items)} articles from IDs "
          f"{max_id}→{nid} (probed {probed})")

    # 4. Sort by date descending, filter to today's window, yield
    items.sort(key=lambda x: x["dt"], reverse=True)
    for it in items:
        if is_today(it["dt"]) is not False:
            yield it["title"], it["url"], it["dt"]


def iter_ajax_archive(src):
    """ShareBiz / ShareBusiness24: POST-based AJAX archive with a date-range
    query. The endpoint requires the full payload — action, totalRecord (read
    from the archive page's JS), sDate, iCategoryID and a SQL fragment `q` —
    or it returns nothing. Yields every article of today + yesterday, pinned
    to the archive day at noon Dhaka."""
    site_url = src["site_url"].rstrip("/")
    ajax_url = site_url + src.get("ajax_path", "/ajax_archive_load_more.php")
    archive_url = site_url + "/archives/"
    art_prefix = src.get("art_prefix", "")
    news_re = re.compile(r"/news/(\d+)")
    total_re = re.compile(r"let\s+totalRecord\s*=\s*(\d+)")
    title_re = re.compile(r'<a[^>]+href="[^"]*?/news/(\d+)[^"]*"[^>]*>(.*?)</a>', re.S)
    per_page = 20
    max_pages = 30

    # read totalRecord from the archive page (required by the endpoint)
    total = 999_999_999
    r = get(archive_url, timeout=20)
    if r and r.status_code == 200:
        m = total_re.search(r.text)
        if m:
            total = int(m.group(1))

    for days_back in range(2):
        d = (NOW - timedelta(days=days_back)).astimezone(DHAKA)
        ymd = d.strftime("%Y-%m-%d")
        pin = datetime(d.year, d.month, d.day, 12, 0, tzinfo=DHAKA)
        q = (f" AND DATE(bc.DateTimeInserted) >= DATE('{ymd}') "
             f"AND DATE(bc.DateTimeInserted) <= DATE('{ymd}') ORDER BY bc.ContentID ")
        seen = set()
        for page in range(max_pages):
            data = {
                "action": "showContent",
                "newsCount": page * per_page,
                "totalRecord": total,
                "rowperpage": per_page,
                "iCategoryID": 0,
                "sDate": ymd,
                "q": q,
                "db_content": "bn_content",
            }
            resp = None
            for dbc in (src.get("db_content", "bn_content"), "content", "en_content"):
                data["db_content"] = dbc
                try:
                    resp = requests.post(ajax_url, headers=HEADERS, data=data,
                                         timeout=60)
                except Exception:
                    resp = None
                    break
                if resp.status_code == 200 and resp.text.strip():
                    break
            if resp is None or resp.status_code != 200 or not resp.text.strip():
                break
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = "utf-8"
            text = resp.text
            soup = BeautifulSoup(text, "html.parser")
            # multiple anchors per article (image + headline) — merge per ID,
            # keep the longest text as the title
            page_titles = {}
            for a in soup.find_all("a", href=True):
                m = news_re.search(a["href"])
                if not m:
                    continue
                cid = m.group(1)
                t = clean_title(a.get_text(" ", strip=True))
                if cid not in page_titles or len(t) > len(page_titles[cid]):
                    page_titles[cid] = t
            new_ids = [c for c in page_titles if c not in seen]
            if not new_ids:
                break
            for cid in new_ids:
                seen.add(cid)
                title = page_titles[cid]
                if not title or len(title) < 5:
                    continue
                url = f"{site_url}{art_prefix}/news/{cid}"
                yield title, url, pin
            time.sleep(0.15)


def iter_amarbarta_archive(src):
    """Amarbarta: GET AJAX archive. The dated archive page renders no links
    server-side (JS-driven), so we page the AJAX endpoint directly from page=0
    with the archive page as Referer. Articles at /{section}/{id}, pinned to
    the archive day at noon Dhaka."""
    site_url = src["site_url"].rstrip("/")
    ajax_url = site_url + src.get("ajax_path",
                                  "/templates/web-view/archive_page/more_archive_ajax.php")
    href_re = re.compile(r'^(?:https?://[^/]+)?(/[a-z0-9-]+/(\d{4,}))')
    max_pages = 25
    sess = requests.Session()
    sess.headers.update(HEADERS)

    for days_back in range(2):
        d = (NOW - timedelta(days=days_back)).astimezone(DHAKA)
        ymd = d.strftime("%Y-%m-%d")
        archive_page = f"{site_url}/archive/online-edition/{d.strftime('%Y/%m/%d')}"
        pin = datetime(d.year, d.month, d.day, 12, 0, tzinfo=DHAKA)
        try:      # prime session cookies
            sess.get(archive_page, timeout=15)
        except Exception:
            pass
        seen = set()
        for page in range(max_pages):     # page=0 is the first batch
            try:
                resp = sess.get(ajax_url,
                                headers={"Referer": archive_page,
                                         "X-Requested-With": "XMLHttpRequest"},
                                params={"page": page, "date": ymd}, timeout=20)
            except Exception:
                break
            if resp.status_code != 200 or not resp.text.strip():
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            # several anchors point at the same article (thumbnail + headline);
            # merge per ID and keep the longest text as the title
            page_items = {}
            for a in soup.find_all("a", href=True):
                m = href_re.match(a["href"].strip())
                if not m:
                    continue
                href, sid = m.group(1), m.group(2)
                t = clean_title(a.get_text(" ", strip=True))
                if sid not in page_items or len(t) > len(page_items[sid][1]):
                    page_items[sid] = (href, t)
            new_ids = [s for s in page_items if s not in seen]
            if not new_ids:
                break
            for sid in new_ids:
                seen.add(sid)
                href, title = page_items[sid]
                if not title or len(title) < 5:
                    continue
                yield title, site_url + href, pin

def _curl_get(url, timeout=30):
    """Fetch a URL via the system `curl` binary. Some hosts (Kalbela, Daily Sun)
    sit behind Cloudflare edges that negotiate TLS 1.3 in a way Python's bundled
    SSL stack can't match — they answer Python with HTTP 403 but serve curl (and
    real browsers) normally. This is purely a transport workaround: no proxy, no
    challenge-solving. Returns the body text, or "" on any failure.
    Windows 10+ ships curl at C:\\Windows\\System32\\curl.exe."""
    import subprocess
    try:
        p = subprocess.run(
            ["curl", "-sL", "--compressed", "--max-time", str(int(timeout)),
             "-A", HEADERS["User-Agent"],
             "-H", "Accept-Language: en-US,en;q=0.9,bn;q=0.8", url],
            capture_output=True, timeout=timeout + 10)
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        print(f"    [curl] {url} failed: {e}")
        return ""


def iter_curl_sitemap(src):
    """News-sitemap fetched over curl (see _curl_get). Same <news:publication_date>
    parsing as iter_sitemap, just a different transport."""
    xml = _curl_get(src["url"], timeout=30)
    if not xml.strip():
        return
    soup = BeautifulSoup(xml, "xml")
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        url = loc.text.strip()
        title = ""
        t = u.find("news:title") or u.find("title")
        if t:
            title = clean_title(t.text)
        dt = None
        d = u.find("news:publication_date") or u.find("lastmod")
        if d:
            dt = parse_dt(d.text.strip())
        if not title:
            # derive from the slug when the sitemap omits <news:title>
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            title = clean_title(slug.replace("-", " "))[:200]
        if title:
            yield title, url, dt


def iter_daily_star_nodes(src):
    """Daily Star node-ID walk. Every Drupal node is at /node/{id} which 302s to
    the canonical article URL. IDs are dense (~1 per 5), publication-ordered but
    NOT perfectly time-ordered at day boundaries. We resolve concurrently and
    buffer/sort to handle inversions.

    Date parsing: article meta block shows relative time ("X hours ago") for
    today, absolute "DD Month YYYY" for older days. We ONLY parse the publish
    portion (text before "UPDATED") to avoid mis-dating edited articles."""
    from concurrent.futures import ThreadPoolExecutor

    site_url = src["site_url"]
    scan_days_back = 3
    workers = 12
    batch_size = 96

    MONTHS = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)}

    # 1. Find max node ID from homepage
    r = get(site_url, timeout=15)
    if not r:
        return
    ids = [int(x) for x in re.findall(r'href="/[^"]*?-(\d{6,})"', r.text)]
    if not ids:
        print("    [daily_star] no node IDs found on homepage")
        return
    max_id = max(ids)
    print(f"    [daily_star] max_id={max_id}, scanning node-ID space...")

    # 2. Resolve one node ID
    def resolve(nid):
        url = f"{site_url}/node/{nid}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20,
                                allow_redirects=True)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        final = resp.url
        # still on /node/ path → not a published article
        if "/node/" in final.split("thedailystar.net", 1)[-1][:7]:
            return None
        html = resp.text

        # parse date from article-meta-block, BEFORE "UPDATED"
        m = re.search(r"block-article-meta-block(.*?)SHARE", html, re.S)
        if not m:
            return None
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        pub = re.split(r"\bUPDATED\b", text, maxsplit=1, flags=re.I)[0]

        dt = None
        # try absolute date: "DD Month YYYY"
        md = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", pub)
        if md:
            mon = MONTHS.get(md.group(2).lower())
            if mon:
                try:
                    d = date(int(md.group(3)), mon, int(md.group(1)))
                    # try to get time too
                    tm = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)", pub, re.I)
                    hh, mm = 12, 0
                    if tm:
                        hh = int(tm.group(1)) % 12
                        if tm.group(3).upper() == "PM":
                            hh += 12
                        mm = int(tm.group(2))
                    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=DHAKA)
                except ValueError:
                    pass
        # try relative: "X hour(s) ago" → today
        if dt is None and re.search(r"(hour|minute|second|just now|ago)", pub, re.I):
            today = NOW.astimezone(DHAKA).date()
            dt = datetime(today.year, today.month, today.day, 12, 0, tzinfo=DHAKA)

        if dt is None:
            return None

        # title
        title = ""
        tm = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if tm:
            title = clean_title(tm.group(1))
        if not title:
            og = re.search(r'property="og:title"\s+content="([^"]+)"', html)
            if og:
                title = clean_title(og.group(1))

        return {"id": nid, "title": title, "url": final, "dt": dt}

    # 3. Walk IDs descending, concurrent
    items = []
    newest_day = None
    nid = max_id
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while nid > 0:
            batch = list(range(nid, max(0, nid - batch_size), -1))
            nid -= batch_size
            for res in pool.map(resolve, batch):
                if not res:
                    continue
                items.append(res)
                d = res["dt"].astimezone(DHAKA).date()
                if newest_day is None or d > newest_day:
                    newest_day = d
            # stop when enough old items accumulated
            if newest_day is not None and items:
                old_count = sum(1 for it in items
                                if (newest_day - it["dt"].astimezone(DHAKA).date()).days
                                >= scan_days_back)
                if old_count >= 5:
                    break

    print(f"    [daily_star] resolved {len(items)} articles from IDs "
          f"{max_id}→{nid}")

    # 4. Sort by date descending, yield within window
    items.sort(key=lambda x: x["dt"], reverse=True)
    for it in items:
        if is_today(it["dt"]) is not False:
            yield it["title"], it["url"], it["dt"]


def _page_date_and_title(url):
    """Fetch one article page and read its publication date + title from HTML.
    Tries standard meta tags first, then site-specific fallbacks (date in the
    og:image path like /2026/06/15/, or visible 'DD Mon YYYY, HH:MM' text)."""
    r = get(url, timeout=10)
    if not r or r.status_code != 200:
        return "", None
    soup = BeautifulSoup(r.content, "lxml")
    dt = None
    # 1. standard meta tags
    for attrs in [{"property": "article:published_time"},
                  {"property": "article:modified_time"},
                  {"name": "publish-date"},
                  {"itemprop": "datePublished"},
                  {"property": "og:updated_time"}]:
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            dt = parse_dt(m["content"])
            if dt:
                break
    # 2. <time datetime=>
    if dt is None:
        t = soup.find("time")
        if t and t.get("datetime"):
            dt = parse_dt(t["datetime"])
    # 3. date embedded in og:image path: .../news_photos/2026/06/15/image-...
    if dt is None:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", og_img["content"])
            if m:
                y, mo, d = map(int, m.groups())
                try:
                    dt = datetime(y, mo, d, tzinfo=timezone.utc)
                except ValueError:
                    pass
    # 4. visible date text like "15 Jun 2026, 20:25"
    if dt is None:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,})\s+(20\d{2}),?\s+(\d{1,2}):(\d{2})", text)
        if m:
            try:
                dt = datetime.strptime(
                    f"{m.group(1)} {m.group(2)[:3]} {m.group(3)} {m.group(4)}:{m.group(5)}",
                    "%d %b %Y %H:%M").replace(tzinfo=DHAKA)
            except ValueError:
                pass
    # title
    title = ""
    ogt = soup.find("meta", property="og:title")
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()
    elif soup.title:
        title = soup.title.get_text(strip=True)
    return title, dt


def iter_sitemap_with_page_dates(sitemap_url, max_fetch=200, stop_after_old=15,
                                 workers=10, batch_size=20, reverse=False):
    """For sources whose sitemap lists URLs but NO dates (e.g. BSS): walk the
    sitemap in order and fetch article pages to read their real dates, keeping
    only today's. Sitemap is roughly newest-first, so we process in BATCHES and
    STOP EARLY once a batch is mostly out-of-window.

    Speed: pages are fetched in PARALLEL (workers threads) rather than one at a
    time — typically 5-8x faster than sequential. `max_fetch` caps total fetches.

    Yields (title, url, datetime).
    """
    from concurrent.futures import ThreadPoolExecutor

    if reverse:
        # giant full-history sitemaps (e.g. WordPress ordered by post ID
        # ascending) put the NEWEST articles at the END. iter_sitemap caps how
        # many URLs it reads from the top, which would never reach the tail —
        # so parse the raw file ourselves and take the LAST max_fetch URLs.
        r = get(sitemap_url, timeout=60)
        if not r or r.status_code != 200:
            return
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        # WordPress single-file sitemaps mix posts with tag/category/author
        # pages (usually clustered at the end) — keep article-like URLs only
        _junk = ("/tag/", "/category/", "/author/", "/page/", "/wp-content/",
                 "/feed", "/about", "/contact", "/privacy")
        locs = [u for u in locs
                if not any(j in u.lower() for j in _junk)
                and u.rstrip("/") != sitemap_url.rsplit("/", 1)[0]]
        tail = locs[-max_fetch:]
        tail.reverse()          # newest (file end) first
        candidates = []
        for u in tail:
            slug = u.rstrip("/").rsplit("/", 1)[-1]
            title = clean_title(slug.replace("-", " "))[:200]
            candidates.append((title, u, None))
    else:
        candidates = list(iter_sitemap(sitemap_url))
    n = min(max_fetch, len(candidates))
    print(f"    fetching dates for up to {n} articles in parallel "
          f"({workers} at a time)...")

    fetched = 0
    consecutive_old_batches = 0
    idx = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while idx < len(candidates) and fetched < max_fetch:
            batch = candidates[idx: idx + batch_size]
            idx += batch_size

            # which items in this batch need a page fetch (no date yet)
            to_fetch = [(i, c) for i, c in enumerate(batch) if c[2] is None]
            results = {}
            if to_fetch:
                urls = [c[1] for _, c in to_fetch]
                for (bi, _), (ptitle, pdt) in zip(
                        to_fetch, pool.map(_page_date_and_title, urls)):
                    results[bi] = (ptitle, pdt)
                fetched += len(to_fetch)

            batch_recent = 0
            for bi, (title, url, dt) in enumerate(batch):
                if dt is None and bi in results:
                    ptitle, dt = results[bi]
                    if ptitle and not title:
                        title = ptitle
                if dt is None:
                    continue
                if is_today(dt) is False:
                    continue
                batch_recent += 1
                yield title, url, dt

            # stop early: if a full batch yielded no recent articles, the rest
            # (older in the newest-first list) won't either
            if batch_recent == 0 and to_fetch:
                consecutive_old_batches += 1
                if consecutive_old_batches >= 1:
                    print(f"    stopped early after {fetched} fetches "
                          f"(batch had no recent articles)")
                    break
            else:
                consecutive_old_batches = 0


def _sitemap_from_url(url, cap=600, timeout=30):
    """Yield (title, url, datetime|None) from ONE news-sitemap XML URL.
    Follows one level of sitemap-index automatically."""
    r = get(url, timeout=timeout)
    if not r or r.status_code != 200:
        return
    content = r.content

    # If this is a sitemap index, follow the first news-related child
    if b"<sitemapindex" in content[:3000]:
        soup_idx = BeautifulSoup(content, "xml")
        child_url = None
        for sm in soup_idx.find_all("sitemap"):
            loc = sm.find("loc")
            if loc:
                candidate = loc.text.strip()
                if "news" in candidate.lower() or child_url is None:
                    child_url = candidate
                    if "news" in candidate.lower():
                        break
        if not child_url:
            return
        r = get(child_url, timeout=timeout)
        if not r or r.status_code != 200:
            return
        content = r.content

    soup = BeautifulSoup(content, "xml")
    count = 0
    for url_el in soup.find_all("url"):
        if count >= cap:
            break
        loc = url_el.find("loc")
        if not loc:
            continue
        article_url = loc.text.strip()

        # Title: <news:title> is the Google News standard; fall back to <title>
        t = url_el.find("news:title") or url_el.find("title")
        title = clean_title(t.text) if t else ""

        # Date: <news:publication_date> preferred, then <lastmod>
        d = (url_el.find("news:publication_date")
             or url_el.find("publication_date")
             or url_el.find("lastmod"))
        dt = parse_dt(d.text.strip() if d else "")

        # If the sitemap gave no date, try to read it from the URL path
        # (handles sites like BSS whose sitemap omits dates but uses dated URLs)
        if dt is None:
            dt = date_from_url(article_url)

        # Drop articles confidently outside today's window
        if is_today(dt) is False:
            continue

        count += 1
        yield title, article_url, dt


# Common news-sitemap path patterns to try when the exact URL is unknown
NEWS_SITEMAP_PATHS = [
    "/sitemap-news.xml", "/news-sitemap.xml", "/sitemap_news.xml",
    "/news.xml", "/google-news-sitemap.xml", "/sitemaps/news-sitemap.xml",
    "/sitemap/sitemap-news.xml", "/sitemap/news.xml", "/rss/news-sitemap.xml",
]

def _sitemap_candidates(primary_url):
    """Build an ordered list of sitemap URLs to try: the configured one first,
    then robots.txt-declared news sitemaps, then common path patterns."""
    candidates = [primary_url]
    parsed = urlparse(primary_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # robots.txt often declares the real sitemap location
    rob = get(root + "/robots.txt")
    if rob and rob.status_code == 200:
        for line in rob.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if "news" in sm_url.lower():
                    candidates.insert(1, sm_url)   # prioritise news sitemaps
                else:
                    candidates.append(sm_url)

    # standard path patterns as a fallback
    for p in NEWS_SITEMAP_PATHS:
        candidates.append(root + p)

    # de-dup while preserving order
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c); ordered.append(c)
    return ordered

def iter_sitemap(url, cap=600, max_candidates=5):
    """Resolve a working news-sitemap. Tries the configured URL first (with the
    normal sitemap timeout), then a LIMITED number of fallback candidates with a
    SHORT timeout so a dead source fails fast instead of grinding through ten
    retrying fetches. Yields from the first URL that returns articles."""
    candidates = _sitemap_candidates(url)[:max_candidates]
    for i, candidate in enumerate(candidates):
        # first candidate (the configured/robots URL) gets the full 30s budget;
        # speculative fallbacks get a short 8s so failures don't pile up
        to = 30 if i == 0 else 8
        items = list(_sitemap_from_url(candidate, cap=cap, timeout=to))
        if items:
            yield from items
            return

# ── EXCEL OUTPUT ──────────────────────────────────────────────────────────────
def write_excel(rows, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Risk Intel"

    # Header
    header_fill = PatternFill("solid", start_color="1F4E78")
    header_font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    cols = ["Section", "Source", "Headline", "Keywords matched", "Published",
            "Also reported by", "Link"]
    for i, col in enumerate(cols, 1):
        c = ws.cell(row=1, column=i, value=col)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(vertical="center", wrap_text=True)

    # Rows sorted by section then source
    rows.sort(key=lambda r: (r["sections"], r["source"]))

    # Alternating section colours for readability
    section_colors = ["EBF3FB", "FDFEFE"]
    section_order, sc_idx = {}, 0
    for r in rows:
        s = r["sections"]
        if s not in section_order:
            section_order[s] = sc_idx % 2
            sc_idx += 1

    for row_n, m in enumerate(rows, start=2):
        bg = section_colors[section_order.get(m["sections"], 0)]
        row_fill = PatternFill("solid", start_color=bg)
        also = m.get("also_in", "")
        # show a count badge if this story appeared in multiple outlets
        dup_count = m.get("dup_count", 1)
        also_display = f"+{dup_count - 1}: {also}" if also else ""
        for col_n, val in enumerate(
            [m["sections"], m["source"], m["title"], m["keywords"],
             m["published"], also_display], 1
        ):
            c = ws.cell(row=row_n, column=col_n, value=val)
            c.fill = row_fill
            c.font = Font(name="Calibri", size=10)
            c.alignment = Alignment(vertical="top", wrap_text=True)
        link_cell = ws.cell(row=row_n, column=7, value=m["url"])
        link_cell.hyperlink = m["url"]
        link_cell.font = Font(color="0563C1", underline="single", name="Calibri", size=10)
        link_cell.fill = row_fill

    for i, w in enumerate([18, 18, 60, 26, 18, 28, 45], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:G{len(rows) + 1}"
    wb.save(path)

# ── VERIFY MODE ───────────────────────────────────────────────────────────────
def verify():
    """Test every source URL and report article counts. Helps fix broken URLs."""
    print(f"\nVerifying {len(SOURCES)} sources (window: last {int(WINDOW.total_seconds()//3600)}h)\n")
    print(f"{'Source':26s} {'Method':8s} {'Today':>6s} {'Status'}")
    print("─" * 70)
    for src in SOURCES:
        name   = src["name"]
        method = src["method"]
        try:
            if method == "combine":
                # show each sub-method's unique contribution
                print(f"{name:26s} {method:8s} {'':>6s}  (combined methods below)")
                seen = set(); total = 0
                for spec in src.get("combine", []):
                    sm = spec["method"]
                    if sm == "sitemap":
                        items = list(iter_sitemap(spec["url"]))
                    elif sm == "rss_multi":
                        items = list(iter_rss_multi(spec["urls"]))
                    elif sm == "rss":
                        items = list(iter_rss(spec["url"]))
                    elif sm == "gnews":
                        items = list(iter_gnews(spec["url"]))
                    else:
                        items = []
                    uniq = 0
                    for _, u, dt in items:
                        if u in seen:
                            continue
                        seen.add(u)
                        if is_today(dt) is not False:
                            uniq += 1; total += 1
                    print(f"  {sm:32s} {'':8s} {uniq:>6d}  unique (+)")
                print(f"  {'→ combined total':32s} {'':8s} {total:>6d}")
                continue

            if method == "rss_multi":
                # Test each section feed individually so you can prune dead ones
                total_today = 0
                print(f"{name:26s} {method:8s} {'':>6s}  (per-feed below)")
                for feed_url in src["urls"]:
                    items = list(iter_rss(feed_url))
                    n = sum(1 for _, _, dt in items if is_today(dt) is not False)
                    total_today += n
                    short = feed_url.replace("https://www.thedailystar.net", "")
                    flag = "OK" if n > 0 else "⚠ 0 — prune"
                    print(f"  {short:32s} {'rss':8s} {n:>6d}  {flag}")
                print(f"  {'→ merged total':32s} {'':8s} {total_today:>6d}")
                continue

            url = src.get("url") or src.get("site_url", "")
            if method == "gnews":
                items = list(iter_gnews(url))
            elif method == "sitemap":
                items = list(iter_sitemap(url))
            elif method == "page_dates":
                items = list(iter_sitemap_with_page_dates(
                    url, reverse=src.get("reverse_sitemap", False)))
            elif method == "aljazeera_dated":
                items = list(iter_aljazeera_dated(url))
            elif method == "sitemap_index_merge":
                items = list(iter_sitemap_index_merge(url))
            elif method == "bbc_sitemaps":
                items = list(iter_bbc_sitemaps(url))
            elif method == "id_enum":
                items = list(iter_id_enum(src))
            elif method == "daily_star_nodes":
                items = list(iter_daily_star_nodes(src))
            elif method == "dated_sitemap":
                items = list(iter_dated_sitemap(src))
            elif method == "wp_rest":
                items = list(iter_wp_rest(src))
            elif method == "ajax_archive":
                items = list(iter_ajax_archive(src))
            elif method == "amarbarta_archive":
                items = list(iter_amarbarta_archive(src))
            elif method == "curl_sitemap":
                items = list(iter_curl_sitemap(src))
            elif method == "api":
                items = list(iter_api(src))
            else:
                items = list(iter_rss(url))
            today_n = sum(1 for _, _, dt in items if is_today(dt) is not False)
            dated_n = sum(1 for _, _, dt in items if dt is not None)
            undated_n = len(items) - dated_n
            if today_n > 0:
                # warn if most "today" articles are actually undated (date filter blind)
                if len(items) > 20 and undated_n > 0.5 * len(items):
                    status = (f"⚠ {undated_n}/{len(items)} UNDATED — "
                              f"date filter can't trust these")
                else:
                    status = "OK"
                print(f"{name:26s} {method:8s} {today_n:>6d}  {status}")
            else:
                # primary failed — show why, then try the full fallback chain
                reason = LAST_ERROR.get(url, "")
                status = f"⚠ 0 — {reason}" if reason else "⚠ 0 — no articles"
                print(f"{name:26s} {method:8s} {today_n:>6d}  {status}")
                if any(k in src for k in ("fallback_sitemaps", "fallback_rss_urls",
                                          "fallback_rss", "fallback_gnews")):
                    fb_articles, fb_method = fetch_articles(src)
                    fb_n = sum(1 for _, _, dt in fb_articles if is_today(dt) is not False)
                    if fb_n > 0:
                        print(f"  {'→ fallback worked':24s} {fb_method:14s} "
                              f"{fb_n:>6d}  OK — using this")
                    else:
                        print(f"  {'→ all fallbacks failed':24s} {'':14s} {'':>6s}  "
                              f"still 0")
        except Exception as e:
            print(f"{name:26s} {method:8s} {'ERR':>6s}  {e}")
    print()

# ── REPORT FROM DB ────────────────────────────────────────────────────────────
def rematch_all(conn, patterns):
    """Re-apply the current keyword list to EVERY article already stored in the
    database, updating each row's sections/keywords/is_match. Use this after
    editing keywords so existing articles benefit without being re-fetched."""
    rows = conn.execute("SELECT url, title FROM articles").fetchall()
    updated = newly_matched = 0
    for url, title in rows:
        sections, keywords = match_keywords(title or "", patterns)
        is_match = 1 if sections else 0
        # check whether this row's match status is changing
        prev = conn.execute("SELECT is_match FROM articles WHERE url=?", (url,)).fetchone()
        conn.execute(
            "UPDATE articles SET sections=?, keywords=?, is_match=? WHERE url=?",
            (sections, keywords, is_match, url))
        updated += 1
        if prev and prev[0] == 0 and is_match == 1:
            newly_matched += 1
    conn.commit()
    print(f"Re-matched {updated} articles against current keywords "
          f"({newly_matched} newly caught).")


def build_report(conn, dedup_enabled=True):
    today = date.today().isoformat()
    # Only include articles from the sources currently in SOURCES —
    # this prevents old runs (with a larger source list) polluting the report.
    active_sources = [src["name"] for src in SOURCES]
    placeholders   = ",".join("?" * len(active_sources))
    rows = conn.execute(
        f"""SELECT source, title, url, published, sections, keywords
            FROM articles
            WHERE is_match=1 AND day=? AND source IN ({placeholders})
            ORDER BY sections, source""",
        [today] + active_sources
    ).fetchall()
    if not rows:
        print("No matches in the database for today yet.")
        return
    out = [{"source": s, "title": t, "url": u,
            "published": p, "sections": se, "keywords": k}
           for s, t, u, p, se, k in rows]

    # ── Semantic deduplication (optional) ──
    # Groups the same story from multiple outlets. Only the representative of
    # each group is shown, with an "also reported by" note. Falls back cleanly
    # if sentence-transformers isn't installed.
    n_before = len(out)
    deduped_note = ""
    if dedup_enabled:
        try:
            import dedup as _dedup
            if _dedup.is_available():
                annotated = _dedup.deduplicate(out)
                out = _dedup.primary_only(annotated)
                merged = n_before - len(out)
                deduped_note = f" ({merged} duplicates merged)" if merged else ""
            else:
                print("  [dedup] sentence-transformers not installed — "
                      "showing all articles. Run: pip install sentence-transformers")
                for a in out:
                    a.setdefault("also_in", ""); a.setdefault("dup_count", 1)
        except Exception as e:
            print(f"  [dedup] skipped ({e})")
            for a in out:
                a.setdefault("also_in", ""); a.setdefault("dup_count", 1)
    else:
        for a in out:
            a.setdefault("also_in", ""); a.setdefault("dup_count", 1)

    path = f"risk_intel_{today}.xlsx"
    write_excel(out, path)
    # print a brief grouped summary
    from collections import Counter
    section_counts = Counter(r["sections"] for r in out)
    print(f"\n{'─'*50}")
    print(f"  {len(out)} unique stories in today's report{deduped_note}: {path}")
    print(f"{'─'*50}")
    for sec, n in sorted(section_counts.items()):
        print(f"  {sec:22s} {n:>3d} article{'s' if n!=1 else ''}")
    print(f"{'─'*50}\n")

# ── MAIN RUN ──────────────────────────────────────────────────────────────────
def _domain_of(url_or_domain):
    """Return the bare domain (e.g. 'thedailystar.net') from a URL or domain."""
    if not url_or_domain:
        return None
    if "://" in url_or_domain:
        return urlparse(url_or_domain).netloc.replace("www.", "")
    return url_or_domain.replace("www.", "")


def normalize_url(url):
    """Clean a URL so the SAME article fetched via different methods dedupes.
    - unwraps Google News redirect links to the real article
    - forces https, drops 'www.'
    - strips tracking query params (utm_*, fbclid, gclid, etc.)
    - removes trailing slash and #fragments
    Used ONLY for duplicate detection; the original display URL is kept separately."""
    if not url:
        return url
    try:
        from urllib.parse import parse_qsl, urlencode, urlunparse
        # unwrap google news redirects (?url=REAL)
        if "news.google." in url:
            q = dict(parse_qsl(urlparse(url).query))
            if q.get("url"):
                url = q["url"]
        p = urlparse(url)
        netloc = p.netloc.replace("www.", "").lower()
        path = p.path.rstrip("/")
        bad = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
               "igshid", "spm", "cmpid")
        kept = [(k, v) for k, v in parse_qsl(p.query)
                if not any(k.lower().startswith(b) or k.lower() == b for b in bad)]
        return urlunparse(("https", netloc, path, "", urlencode(kept), ""))
    except Exception:
        return url


def fetch_all_methods(src):
    """MAXIMUM-COVERAGE fetch: run sitemap AND rss AND domain-restricted gnews
    (whichever apply), union the results, and dedupe by normalized URL.

    - Each method runs independently; if one fails, the others still contribute
      (partial-failure tolerant).
    - gnews is ALWAYS domain-restricted (site:domain) — never broad — so it adds
      coverage without pulling in unrelated outlets.
    - A source can disable a method via config: "disable_methods": ["gnews"].

    Returns (articles_list, method_used_str) where method_used lists which
    methods actually contributed, e.g. "sitemap+rss+gnews".
    """
    name = src["name"]
    method = src["method"]
    disabled = set(src.get("disable_methods", []))
    domain = _domain_of(src.get("url", "") or src.get("site_url", "")) or ""
    is_real_domain = bool(domain and "." in domain)

    # methods where auto-fallback sitemap/rss probing doesn't apply
    _no_auto = ("sitemap", "page_dates", "combine", "sitemap_index_merge", "curl_sitemap",
                "bbc_sitemaps", "aljazeera_dated")

    seen = {}            # normalized_url -> (title, original_url, dt)
    contributed = []     # which methods returned anything

    def add(articles, label):
        added = 0
        for title, url, dt in articles:
            key = normalize_url(url)
            if key in seen:
                # keep the one with a date if the existing has none
                if seen[key][2] is None and dt is not None:
                    seen[key] = (title, url, dt)
                continue
            seen[key] = (title, url, dt)
            added += 1
        if added:
            contributed.append(f"{label}({added})")

    # ── the source's OWN configured method (most authoritative) ──
    try:
        primary, plabel = _fetch_one_method(src)
        if primary:
            add(primary, plabel)
    except Exception as e:
        print(f"    [{name}] primary method failed: {e}")

    # ── sitemap (auto, from domain) ──
    if is_real_domain and "sitemap" not in disabled and method not in _no_auto:
        try:
            arts = list(iter_sitemap(f"https://www.{domain}/sitemap.xml"))
            add(arts, "sitemap")
        except Exception as e:
            print(f"    [{name}] sitemap skipped: {e}")

    # ── rss (auto, from domain) ──
    if is_real_domain and "rss" not in disabled and method not in ("rss", "rss_multi"):
        for path in ("/rss.xml", "/feed", "/rss"):
            try:
                arts = list(iter_rss(f"https://www.{domain}{path}"))
                if arts:
                    add(arts, "rss")
                    break
            except Exception as e:
                print(f"    [{name}] rss {path} skipped: {e}")

    # ── domain-restricted gnews — FALLBACK ONLY ──
    # gnews is rate-limited by Google, so running it for every source in a big
    # exhaustive run gets the whole run throttled. We only use it when the other
    # methods came up short (few/no articles), or when it's the source's OWN
    # primary method. This keeps coverage for weak sources without hammering
    # Google News for sources that already have a good sitemap/API/RSS.
    GNEWS_FALLBACK_THRESHOLD = 5
    force_gnews = src.get("force_gnews", False)
    if (is_real_domain and "gnews" not in disabled and method != "gnews"
            and (force_gnews or len(seen) < GNEWS_FALLBACK_THRESHOLD)):
        try:
            arts = list(iter_gnews(domain))
            add(arts, "gnews")
        except Exception as e:
            print(f"    [{name}] gnews skipped: {e}")
    elif method == "gnews":
        # gnews IS the primary method — already ran above via _fetch_one_method
        pass

    articles = list(seen.values())
    # human-readable method names (no internal words like "combine")
    label = _humanize_methods(contributed) if contributed else "none"
    return articles, label


# maps internal method labels to clear display names
_METHOD_DISPLAY = {
    "api": "API",
    "sitemap": "sitemap",
    "sitemap_index_merge": "sitemap",
    "bbc_sitemaps": "sitemap",
    "aljazeera_dated": "sitemap",
    "dated_sitemap": "sitemap",
    "page_dates": "sitemap",
    "rss": "RSS",
    "rss_multi": "RSS",
    "gnews": "Google News",
    "id_enum": "ID scan",
    "daily_star_nodes": "ID scan",
    "wp_rest": "API",
    "ajax_archive": "archive",
    "amarbarta_archive": "archive",
    "curl_sitemap": "sitemap",
    "combine": "sitemap",   # legacy combine was sitemap-based
}


def _humanize_methods(contributed):
    """Turn internal labels like ['sitemap(42)', 'gnews(8)'] into a clean,
    de-duplicated, ordered display string like 'sitemap, Google News'."""
    order = ["API", "sitemap", "RSS", "Google News", "ID scan", "archive"]
    found = []
    for entry in contributed:
        # strip the "(count)" suffix
        base = entry.split("(")[0]
        display = _METHOD_DISPLAY.get(base, base)
        if display not in found:
            found.append(display)
    # sort by canonical order, unknowns last
    found.sort(key=lambda m: order.index(m) if m in order else 99)
    return ", ".join(found)


def _fetch_one_method(src):
    """Run ONLY the source's configured method (no fallback chain). Returns
    (articles, label). Used as the authoritative first pass in fetch_all_methods."""
    method = src["method"]
    if method == "combine":
        return list(iter_combined(src)), "combine"
    if method == "rss_multi":
        return list(iter_rss_multi(src["urls"])), "rss_multi"
    if method == "gnews":
        return list(iter_gnews(src["url"])), "gnews"
    if method == "page_dates":
        return list(iter_sitemap_with_page_dates(
            src["url"], reverse=src.get("reverse_sitemap", False))), "page_dates"
    if method == "sitemap":
        return list(iter_sitemap(src["url"])), "sitemap"
    if method == "api":
        return list(iter_api(src)), "api"
    if method == "aljazeera_dated":
        return list(iter_aljazeera_dated(src["url"])), "aljazeera_dated"
    if method == "sitemap_index_merge":
        return list(iter_sitemap_index_merge(src["url"])), "sitemap_index_merge"
    if method == "bbc_sitemaps":
        return list(iter_bbc_sitemaps(src["url"])), "bbc_sitemaps"
    if method == "id_enum":
        return list(iter_id_enum(src)), "id_enum"
    if method == "daily_star_nodes":
        return list(iter_daily_star_nodes(src)), "daily_star_nodes"
    if method == "dated_sitemap":
        return list(iter_dated_sitemap(src)), "dated_sitemap"
    if method == "wp_rest":
        return list(iter_wp_rest(src)), "wp_rest"
    if method == "ajax_archive":
        return list(iter_ajax_archive(src)), "ajax_archive"
    if method == "amarbarta_archive":
        return list(iter_amarbarta_archive(src)), "amarbarta_archive"
    if method == "curl_sitemap":
        return list(iter_curl_sitemap(src)), "curl_sitemap"
    return list(iter_rss(src["url"])), "rss"


def fetch_articles(src):
    """Fetch a source through a STANDARD fallback chain (legacy single-method):
         1. primary method (sitemap / rss / rss_multi / gnews / page_dates)
         2. configured fallbacks (fallback_sitemaps, fallback_rss*, etc.)
         3. AUTOMATIC chain: sitemap → RSS → Google News (derived from domain)
            unless the source opts out with "no_auto_fallback": True
    Stops at the first method that returns articles.
    Returns (articles_list, method_used_str).

    NOTE: collection now uses fetch_all_methods() for maximum coverage; this is
    kept for the Test button and any single-method needs.
    """
    name, method = src["name"], src["method"]

    def sitemap_ok(articles):
        """Reject a sitemap result that is mostly undated (would flood report)."""
        if not articles:
            return False
        dated = sum(1 for _, _, dt in articles if dt is not None)
        if len(articles) > 20 and dated < 0.3 * len(articles):
            print(f"    ⚠ sitemap has {len(articles)} articles but only "
                  f"{dated} dated — skipping to fallback")
            return False
        return True

    # ── 1. primary method ──
    if method == "combine":
        articles = list(iter_combined(src))
        if articles:
            return articles, "combine"
    elif method == "rss_multi":
        articles = list(iter_rss_multi(src["urls"]))
        if articles:
            return articles, "rss_multi"
    elif method == "gnews":
        articles = list(iter_gnews(src["url"]))
        if articles:
            return articles, "gnews"
    elif method == "page_dates":
        articles = list(iter_sitemap_with_page_dates(
            src["url"], reverse=src.get("reverse_sitemap", False)))
        if articles:
            return articles, "page_dates"
    elif method == "sitemap":
        articles = list(iter_sitemap(src["url"]))
        if sitemap_ok(articles):
            return articles, "sitemap"
    elif method == "api":
        articles = list(iter_api(src))
        if articles:
            return articles, "api"
    elif method == "aljazeera_dated":
        articles = list(iter_aljazeera_dated(src["url"]))
        if articles:
            return articles, "aljazeera_dated"
    elif method == "sitemap_index_merge":
        articles = list(iter_sitemap_index_merge(src["url"]))
        if articles:
            return articles, "sitemap_index_merge"
    elif method == "bbc_sitemaps":
        articles = list(iter_bbc_sitemaps(src["url"]))
        if articles:
            return articles, "bbc_sitemaps"
    elif method == "id_enum":
        articles = list(iter_id_enum(src))
        if articles:
            return articles, "id_enum"
    elif method == "daily_star_nodes":
        articles = list(iter_daily_star_nodes(src))
        if articles:
            return articles, "daily_star_nodes"
    elif method == "dated_sitemap":
        articles = list(iter_dated_sitemap(src))
        if articles:
            return articles, "dated_sitemap"
    elif method == "wp_rest":
        articles = list(iter_wp_rest(src))
        if articles:
            return articles, "wp_rest"
    elif method == "ajax_archive":
        articles = list(iter_ajax_archive(src))
        if articles:
            return articles, "ajax_archive"
    elif method == "amarbarta_archive":
        articles = list(iter_amarbarta_archive(src))
        if articles:
            return articles, "amarbarta_archive"
    elif method == "curl_sitemap":
        articles = list(iter_curl_sitemap(src))
        if articles:
            return articles, "curl_sitemap"
    else:  # rss
        articles = list(iter_rss(src["url"]))
        if articles:
            return articles, "rss"

    # ── 2. explicitly configured fallbacks ──
    for sm in src.get("fallback_sitemaps", []):
        articles = list(iter_sitemap(sm))
        if sitemap_ok(articles):
            return articles, "sitemap(alt)"
    for rurl in src.get("fallback_rss_urls", []):
        articles = list(iter_rss(rurl))
        if articles:
            return articles, "rss(alt)"
    if src.get("fallback_rss_multi"):
        articles = list(iter_rss_multi(src["fallback_rss_multi"]))
        if articles:
            return articles, "rss_multi(fallback)"
    if src.get("fallback_rss"):
        articles = list(iter_rss(src["fallback_rss"]))
        if articles:
            return articles, "rss(fallback)"

    # ── 3. AUTOMATIC chain: sitemap → RSS → Google News ──
    # Skipped if the source opts out, is gnews-primary (deliberately avoids the
    # direct site, e.g. CAPTCHA-blocked), or for methods where it doesn't apply.
    if not src.get("no_auto_fallback") and method != "gnews":
        domain = _domain_of(src.get("url", "") or src.get("site_url", ""))
        if domain and "." in domain:   # only for real domains, not gnews keywords
            root = f"https://www.{domain}"
            # try sitemap (if we weren't already on sitemap)
            if method not in ("sitemap", "page_dates"):
                articles = list(iter_sitemap(root + "/sitemap.xml"))
                if sitemap_ok(articles):
                    return articles, "sitemap(auto)"
            # try RSS (if we weren't already on rss)
            if method not in ("rss", "rss_multi"):
                for path in ("/rss.xml", "/feed", "/rss"):
                    articles = list(iter_rss(root + path))
                    if articles:
                        return articles, "rss(auto)"
            # finally Google News
            articles = list(iter_gnews(domain))
            if articles:
                return articles, "gnews(auto)"

    # explicit gnews fallback (kept for sources that set it directly)
    if src.get("fallback_gnews"):
        articles = list(iter_gnews(src["fallback_gnews"]))
        if articles:
            return articles, "gnews(fallback)"

    return [], method


def diagnose_source(src):
    """Step-by-step diagnostic for one source. Returns a list of check dicts:
    [{"step": ..., "ok": bool, "detail": ...}, ...]. Runs method-specific
    probes (homepage reachability, ID extraction, date parsing, endpoint
    responses) so a zero-article source can be debugged from the UI."""
    checks = []
    method = src.get("method", "rss")
    name = src.get("name", "?")

    def add(step, ok, detail):
        checks.append({"step": step, "ok": bool(ok), "detail": str(detail)[:400]})

    site_url = (src.get("site_url") or src.get("url") or "").rstrip("/")

    # ── universal: is the site/endpoint reachable? ──
    probe_url = site_url if site_url.startswith("http") else None
    if method == "gnews":
        domain = src.get("url", "")
        add("config", True, f"method=gnews domain={domain}")
        try:
            arts = list(iter_gnews(domain))
            add("google news query", len(arts) > 0,
                f"{len(arts)} articles returned" if arts else
                "0 articles — Google News does not index this domain (or rate-limited)")
        except Exception as e:
            add("google news query", False, f"{type(e).__name__}: {e}")
        return checks

    if probe_url:
        try:
            r = requests.get(probe_url, headers=HEADERS, timeout=15)
            add("site reachable", r.status_code == 200,
                f"GET {probe_url} → HTTP {r.status_code}, {len(r.text)} bytes")
            homepage_html = r.text if r.status_code == 200 else ""
        except Exception as e:
            add("site reachable", False, f"{type(e).__name__}: {e}")
            homepage_html = ""
    else:
        homepage_html = ""
        add("config", False, f"no site_url/url configured")

    # ── method-specific probes ──
    if method == "wp_rest":
        api = f"{site_url}/wp-json/wp/v2/posts"
        try:
            r = requests.get(api, headers={**HEADERS, "Accept": "application/json"},
                             params={"per_page": 3, "page": 1,
                                     "orderby": "date", "order": "desc"},
                             timeout=20)
            ct = r.headers.get("content-type", "")
            add("WP REST endpoint", r.status_code == 200,
                f"HTTP {r.status_code}, content-type={ct}")
            if r.status_code == 200:
                try:
                    posts = r.json()
                    ok = isinstance(posts, list) and len(posts) > 0
                    add("JSON parse", ok,
                        f"{len(posts)} posts; first date_gmt="
                        f"{posts[0].get('date_gmt') if posts else '—'}")
                except Exception as e:
                    add("JSON parse", False,
                        f"{e} — body starts: {r.text[:150]!r}")
            else:
                add("response body", False, r.text[:200])
        except Exception as e:
            add("WP REST endpoint", False, f"{type(e).__name__}: {e}")

    elif method == "id_enum":
        id_rx = src.get("id_regex", r"/(\d{4,})")
        try:
            ids = re.findall(id_rx, homepage_html)
            add("ID extraction", len(ids) > 0,
                f"regex {id_rx!r} found {len(ids)} IDs"
                + (f", max={max(map(int, ids))}" if ids else " — REGEX MATCHES NOTHING on homepage"))
        except Exception as e:
            ids = []
            add("ID extraction", False, f"regex error: {e}")
        if ids:
            mx = max(map(int, ids))
            art_url = site_url + src.get("article_pattern", "/{id}").replace("{id}", str(mx))
            try:
                fr = not src.get("no_follow_redirects", False)
                r = requests.get(art_url, headers=HEADERS, timeout=15,
                                 allow_redirects=fr)
                add("article fetch", r.status_code == 200,
                    f"GET {art_url} → HTTP {r.status_code}, final={r.url}")
                if r.status_code == 200 and src.get("canonical_hop"):
                    cm = re.search(r'<link rel="canonical" href="(https?://[^"]+)"', r.text)
                    add("canonical link", bool(cm),
                        cm.group(1) if cm else "no canonical link — dead ID or shell blocked")
                    if cm:
                        try:
                            r = requests.get(cm.group(1), headers=HEADERS, timeout=15)
                            add("canonical fetch", r.status_code == 200,
                                f"HTTP {r.status_code}")
                        except Exception as e:
                            add("canonical fetch", False, str(e))
                if r.status_code == 200:
                    dsrc = src.get("date_source", "jsonld")
                    if dsrc == "nextdata":
                        m = re.search(r'"(?:published_at|created_at|publishedAt|createdAt|date)"\s*:\s*"(\d{4}-\d{2}-\d{2}[^"]*)"', r.text)
                        add("nextdata date present", bool(m),
                            m.group(1) if m else "no ISO date key in __NEXT_DATA__")
                    elif dsrc == "jsonld" or dsrc == "jsonld_timefirst":
                        has = '"datePublished"' in r.text
                        add("datePublished present", has,
                            (re.search(r'"datePublished"\s*:\s*"([^"]+)"', r.text).group(1)
                             if has else "NOT in HTML — date_source=jsonld cannot work"))
                    elif dsrc == "bengali_date":
                        m = re.search(r"[০-৯]{1,2}\s+\S+\s+[০-৯]{4}", r.text)
                        add("bengali date present", bool(m),
                            m.group(0) if m else "no Bengali date pattern found in HTML")
                    elif dsrc == "calendar_dmy":
                        m = re.search(r'(\d{2})-(\d{2})-(\d{4})', r.text)
                        add("DD-MM-YYYY date present", bool(m),
                            m.group(0) if m else "no calendar date found")
                    elif dsrc == "time_element":
                        m = re.search(r'<time[^>]*>([^<]+)</time>', r.text)
                        add("<time> element", bool(m),
                            m.group(1)[:80] if m else "no <time> element in HTML")
            except Exception as e:
                add("article fetch", False, f"{type(e).__name__}: {e}")

    elif method == "ajax_archive":
        archive_url = site_url + "/archives/"
        try:
            r = requests.get(archive_url, headers=HEADERS, timeout=30)
            m = re.search(r"let\s+totalRecord\s*=\s*(\d+)", r.text)
            add("archive page + totalRecord", bool(m),
                f"HTTP {r.status_code}; totalRecord="
                f"{m.group(1) if m else 'NOT FOUND (JS var missing)'}")
            total = int(m.group(1)) if m else 999_999_999
            ajax_url = site_url + src.get("ajax_path", "/ajax_archive_load_more.php")
            # probe today AND the last few days — an empty body for today just
            # means "nothing published today", not a broken endpoint.
            any_hit = False
            for back in range(4):
                dd = (NOW - timedelta(days=back)).astimezone(DHAKA)
                ymd = dd.strftime("%Y-%m-%d")
                q = (f" AND DATE(bc.DateTimeInserted) >= DATE('{ymd}') "
                     f"AND DATE(bc.DateTimeInserted) <= DATE('{ymd}') ORDER BY bc.ContentID ")
                r2 = requests.post(ajax_url, headers=HEADERS,
                                   data={"action": "showContent", "newsCount": 0,
                                         "totalRecord": total, "rowperpage": 20,
                                         "iCategoryID": 0, "sDate": ymd, "q": q,
                                         "db_content": "bn_content"},
                                   timeout=60)
                ids = re.findall(r"/news/(\d+)", r2.text)
                if ids:
                    any_hit = True
                add(f"AJAX POST {ymd}", len(ids) > 0,
                    f"HTTP {r2.status_code}, {len(r2.text)} bytes, {len(ids)} article IDs"
                    + ("" if ids else f" — empty (no articles that day, or query mismatch)"))
                if any_hit:
                    break
        except Exception as e:
            add("AJAX POST", False, f"{type(e).__name__}: {e}")

    elif method == "amarbarta_archive":
        d = NOW.astimezone(DHAKA)
        archive_page = f"{site_url}/archive/online-edition/{d.strftime('%Y/%m/%d')}"
        try:
            r = requests.get(archive_page, headers=HEADERS, timeout=20)
            links = re.findall(r'href="(?:' + re.escape(site_url) + r')?(/[a-z0-9-]+/\d{4,})"',
                               r.text)
            add("dated archive page", len(links) > 0,
                f"GET {archive_page} → HTTP {r.status_code}, {len(links)} article links")
            ajax_url = site_url + src.get("ajax_path",
                                          "/templates/web-view/archive_page/more_archive_ajax.php")
            sess = requests.Session(); sess.headers.update(HEADERS)
            sess.get(archive_page, timeout=15)
            r2 = sess.get(ajax_url,
                          headers={"Referer": archive_page,
                                   "X-Requested-With": "XMLHttpRequest"},
                          params={"page": 1, "date": d.strftime("%Y-%m-%d")}, timeout=20)
            links2 = re.findall(r'/[a-z0-9-]+/\d{4,}', r2.text)
            add("AJAX page=1", True,
                f"HTTP {r2.status_code}, {len(r2.text)} bytes, {len(links2)} links "
                "(empty can be normal if the day fits on one page)")
        except Exception as e:
            add("archive probe", False, f"{type(e).__name__}: {e}")

    elif method == "curl_sitemap":
        xml = _curl_get(src.get("url", ""), timeout=30)
        add("curl fetch", bool(xml.strip()),
            f"{len(xml)} bytes via curl"
            + ("" if xml.strip() else " — curl returned nothing (is curl installed?)"))
        if xml.strip():
            arts = list(iter_curl_sitemap(src))
            add("sitemap parse", len(arts) > 0, f"{len(arts)} articles parsed")

    elif method == "page_dates" and src.get("reverse_sitemap"):
        r = get(src.get("url", ""), timeout=60)
        if not r or r.status_code != 200:
            add("sitemap fetch", False, f"HTTP {r.status_code if r else 'fail'}")
        else:
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
            is_index = "<sitemapindex" in r.text[:2000]
            _junk = ("/tag/", "/category/", "/author/", "/page/", "/wp-content/",
                     "/feed", "/about", "/contact", "/privacy")
            locs = [u for u in locs if not any(j in u.lower() for j in _junk)]
            add("sitemap parse", len(locs) > 0,
                f"{len(locs)} <loc> entries; sitemapindex={is_index}; "
                f"last URL: {locs[-1] if locs else '—'}")
            if locs:
                probe = locs[-1]
                title, dt = _page_date_and_title(probe)
                add("tail page date", dt is not None,
                    f"{probe} → date={dt}, title={title[:60]!r}")

    elif method in ("sitemap", "sitemap_index_merge", "bbc_sitemaps",
                    "aljazeera_dated", "dated_sitemap", "page_dates"):
        try:
            arts, label = _fetch_one_method(src)
            add(f"primary ({label})", len(arts) > 0,
                f"{len(arts)} articles returned")
        except Exception as e:
            add("primary method", False, f"{type(e).__name__}: {e}")

    elif method in ("rss", "rss_multi"):
        try:
            arts, label = _fetch_one_method(src)
            add(f"primary ({label})", len(arts) > 0,
                f"{len(arts)} articles returned")
        except Exception as e:
            add("primary method", False, f"{type(e).__name__}: {e}")

    elif method == "api":
        try:
            arts = list(iter_api(src))
            add("API fetch", len(arts) > 0, f"{len(arts)} articles returned")
        except Exception as e:
            add("API fetch", False, f"{type(e).__name__}: {e}")

    elif method == "daily_star_nodes":
        try:
            ids = re.findall(r'href="/[^"]*?-(\d{6,})"', homepage_html)
            add("node ID extraction", len(ids) > 0,
                f"{len(ids)} node IDs on homepage"
                + (f", max={max(map(int, ids))}" if ids else ""))
        except Exception as e:
            add("node ID extraction", False, str(e))

    return checks


def run(conn, patterns):
    total_new = total_match = 0
    for src in SOURCES:
        name = src["name"]
        print(f"  {name}")
        new_n = match_n = 0
        try:
            articles, method_used = fetch_articles(src)
            if method_used != src["method"]:
                print(f"    ↳ used fallback: {method_used}")
            for title, article_url, dt in articles:
                # Skip articles outside today's window
                if is_today(dt) is False:
                    continue
                if is_seen(conn, article_url):
                    continue
                pub = dt.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M") if dt else ""
                sections, keywords = match_keywords(title, patterns)
                save(conn, article_url, name, title, pub, sections, keywords)
                new_n += 1; total_new += 1
                if sections:
                    match_n += 1; total_match += 1
        except Exception as e:
            print(f"    [error] {e}")
        print(f"    {new_n} new  |  {match_n} matched")
    conn.commit()
    return total_new, total_match

# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ERM daily risk intelligence collector")
    ap.add_argument("--report", action="store_true",
                    help="Rebuild today's Excel from the database without fetching")
    ap.add_argument("--verify", action="store_true",
                    help="Test all source URLs and report article counts")
    ap.add_argument("--no-dedup", action="store_true",
                    help="Disable semantic deduplication (show every article)")
    ap.add_argument("--rematch", action="store_true",
                    help="Re-apply current keywords to ALL articles already in "
                         "the database (use after editing keywords)")
    args = ap.parse_args()

    dedup_on = not args.no_dedup

    if args.verify:
        verify()

    elif args.rematch:
        groups   = load_keywords()
        patterns = build_patterns(groups)
        conn = init_db()
        rematch_all(conn, patterns)
        build_report(conn, dedup_enabled=dedup_on)

    elif args.report:
        build_report(init_db(), dedup_enabled=dedup_on)

    else:
        groups   = load_keywords()
        patterns = build_patterns(groups)
        print(f"\nLoaded {sum(len(v) for v in groups.values())} keywords "
              f"across {len(groups)} sections")
        print(f"Sources: {len(SOURCES)}  |  Window: last {int(WINDOW.total_seconds()//3600)}h "
              f"(Dhaka time)\n")
        conn = init_db()
        total_new, total_match = run(conn, patterns)
        print(f"\nTotal: {total_new} new articles, {total_match} keyword matches")
        build_report(conn, dedup_enabled=dedup_on)
