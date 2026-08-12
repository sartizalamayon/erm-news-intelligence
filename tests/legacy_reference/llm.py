"""
llm.py — pluggable LLM layer (judgment + summarization)
========================================================
This file defines a MODULAR interface for adding an LLM later, WITHOUT changing
the rest of the app. Right now there is no model wired in — the default
provider does nothing — but the structure is here so you can drop one in.

The design: the app talks to an abstract `LLMProvider`. You can later create a
provider for whatever you choose (Azure OpenAI, a local model, Anthropic, etc.)
by filling in two methods. The app calls `get_provider()` and uses whatever is
configured — it never hard-codes a specific model.

Two capabilities the app will use:
  judge(article)     -> a relevance/importance score + reasoning
  summarize(article) -> a short summary

To ADD an LLM later you will:
  1. Write a subclass of LLMProvider (e.g. AzureOpenAIProvider) below.
  2. Point get_provider() at it (or set it via config/env var).
Nothing else in the app needs to change.
"""

from __future__ import annotations
import os
from dataclasses import dataclass


def _get_relevance_guidance():
    """Read the analyst's custom relevance guidance from the settings table
    (set on the Refine page). Returns '' if none or on any error."""
    try:
        from database import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM settings WHERE key='relevance_guidance'").fetchone()
        conn.close()
        return (row["value"] if row else "") or ""
    except Exception:
        return ""


def _get_relevance_examples():
    """Read the editable few-shot examples from settings; fall back to defaults.
    Format: one example per line, 'headline => score' (score 1-10)."""
    try:
        from database import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM settings WHERE key='relevance_examples'").fetchone()
        conn.close()
        if row and (row["value"] or "").strip():
            return row["value"]
    except Exception:
        pass
    return DEFAULT_EXAMPLES


# Fixed editorial rules — the backbone of relevance judgment, from the ERM
# Daily Risk Insights guide. Baked in (not editable) so judgments stay anchored.
FIXED_GUIDE = (
    "You are a risk analyst at Grameenphone (GP), Bangladesh's largest telecom "
    "operator (owned by Telenor), preparing a Daily Risk Insights brief for the "
    "Enterprise Risk Management (ERM) team.\n"
    "CORE TEST for every article: does this plausibly affect Grameenphone, the "
    "telecom sector, or Bangladesh's macro / energy / trade / fiscal / "
    "geopolitical-risk environment? If a risk officer in Dhaka wouldn't care, it "
    "does not belong.\n\n"
    "STANDING THEMES — always evaluated, carried even on a quiet day:\n"
    "- Middle East conflict (US-Iran-Israel-Hormuz-Gulf) — the dominant oil-price "
    "driver feeding Bangladesh's energy import bill. Treat as top-tier.\n"
    "- Russia-Ukraine war — carried as a standing geopolitical/energy item.\n"
    "- Taka/FX pressure and reserves — tracked continuously (GP carries USD exposure).\n"
    "- Bangladesh banking-sector health — an ongoing crisis to track.\n\n"
    "Score every article 1-10. The score maps to a colour band the team reads:\n\n"
    "RED — score 8, 9, or 10 — MOST RELEVANT. Two families (list roughly most "
    "to least relevant within each):\n"
    "  - Telecom / direct mandate: any Grameenphone mention; BTRC; SIM; spectrum; "
    "5G; 700 MHz; network; mobile tower; optical fibre; NEIR; QoS; telecom tax; "
    "SIM tax; withholding tax on regulatory payments; Banglalink; Robi; Axiata; "
    "Teletalk; Airtel; VEON; BTCL; Summit Communications; IIG; bandwidth pricing; "
    "MFS; Nagad; bKash; other telco news. Always top tier even when thin.\n"
    "  - BD macroeconomy / energy: oil; Brent; crude; gas; LNG; Hormuz; "
    "Bab el-Mandeb; energy supply; load-shedding; fuel & furnace-oil pricing; "
    "forex reserves; GDP; taka; FX; dollar rate; inflation; ADP; govt revenue; "
    "NBR collection; BD budget; remittance; IMF; WB; ADB; AIIB; JICA; external "
    "debt; sovereign borrowing.\n\n"
    "YELLOW — score 6 or 7 — MEDIUM. Use for:\n"
    "  - Banking-sector health: NPLs, distressed loans, CRAR, bank losses/mergers, "
    "BB liquidity support, IMF banking conditions.\n"
    "  - Secondary economic signals: RMG/apparel exports; FDI (aggregate or "
    "sector-level flows and pipelines, NOT individual-company acquisitions); "
    "EPZ/FTZ/economic zones; trade deals; external financing projects; capital "
    "markets / BSEC / DSEX; PMI; wheat / food imports.\n"
    "  - US tariff / Section 301 exposure.\n"
    "  - Governance and political process: parliament, constitutional amendment, "
    "cabinet, elections, AL ban.\n"
    "  - Tax-policy direction and NBR structural reform: VAT/BIN rules, tax-appeal "
    "changes, capital gains tax.\n"
    "  - China-BD / bilateral diplomacy.\n"
    "  - Monsoon floods; Chattogram port & supply-chain disruption.\n"
    "  - Middle East conflict where BD-market transmission is indirect.\n\n"
    "GREEN — score 5 — SOFT / THRESHOLD. Relevant but minor; qualifies for "
    "inclusion but rarely earns a bullet on a busy day. Use for:\n"
    "  - Routine regulatory circulars, minor allocations, secondary corporate "
    "moves, single-company capital-market notices.\n"
    "  - Individual-company M&A / acquisitions (e.g. a lone foreign firm taking a "
    "stake in one local company — NOT aggregate FDI).\n"
    "  - Russia-Ukraine; Myanmar / Rohingya (standing items).\n\n"
    "MAGENTA — score 1-4 — LEAST RELEVANT (mostly excluded). Borderline padding "
    "and noise. Use for:\n"
    "  - Routine data releases and minor administrative / operational regulatory "
    "tweaks with no risk signal (these sit at 3-4, above pure noise).\n"
    "  - Sports, entertainment, celebrity, lifestyle.\n"
    "  - Opinion / editorial; advertorial / sponsored content (always excluded).\n"
    "  - Crime / accidents.\n"
    "  - Pure foreign domestic news with no BD or oil hook.\n\n"
    "DON'T PAD: when unsure between green and magenta, prefer MAGENTA — borderline "
    "items should lose by default and only surface when nothing better exists. "
    "Exception: a sports/entertainment story WITH a genuine telecom/network hook "
    "(e.g. streaming-failure, telco sponsorship) can rise out of magenta."
)

# Default few-shot examples (editable on the Refine page). 'headline => score'.
# Bilingual: each story appears as an English line AND its Bengali line at the
# SAME score. The judge normally scores title_en (English), but falls back to
# the raw native title when a translation is missing or poor — so anchoring the
# model on real Bengali headlines helps both the fallback and imperfect-
# translation cases. At least 3 stories per colour band.
DEFAULT_EXAMPLES = (
    # RED (8-10): telecom/BTRC-direct, oil/Hormuz/LNG, core macro fundamentals,
    # domestic fuel pricing, external financing, govt-revenue structural measures
    "GP becomes first operator to deploy 700 MHz nationwide => 10\n"
    "দেশজুড়ে ৭০০ মেগাহার্টজ চালু করা প্রথম অপারেটর গ্রামীণফোন => 10\n"
    "BTRC upholds Tk3cr fine on Summit for discriminatory bandwidth pricing => 10\n"
    "বৈষম্যমূলক ব্যান্ডউইথ মূল্যে সামিটকে ৩ কোটি টাকা জরিমানা বহাল রাখল বিটিআরসি => 10\n"
    "Mobile operators owe govt Tk13,344cr, GP tops list => 10\n"
    "সরকারের পাওনা ১৩,৩৪৪ কোটি টাকা, শীর্ষে গ্রামীণফোন => 10\n"
    "BTRC approves Starlink to export unfiltered bandwidth => 9\n"
    "আনফিল্টার্ড ব্যান্ডউইথ রপ্তানিতে স্টারলিংককে অনুমোদন বিটিআরসির => 9\n"
    "US launches new Iran strikes, reimposes naval blockade => 9\n"
    "ইরানে নতুন হামলা যুক্তরাষ্ট্রের, ফের নৌ-অবরোধ => 9\n"
    "Iran closes Strait of Hormuz => 9\n"
    "হরমুজ প্রণালি বন্ধ করল ইরান => 9\n"
    "QatarEnergy halves 2026 LNG deliveries to Bangladesh => 9\n"
    "বাংলাদেশে ২০২৬ সালের এলএনজি সরবরাহ অর্ধেক করল কাতার এনার্জি => 9\n"
    "Dollar rises to Tk123 on import pressure => 8\n"
    "আমদানি চাপে ডলারের দাম বেড়ে ১২৩ টাকা => 8\n"
    "Net forex reserves cross $36.1b => 8\n"
    "৪৪ মাস পর বৈদেশিক মুদ্রার রিজার্ভ ৩৬.১ বিলিয়ন ডলার ছাড়াল => 8\n"
    "Bangladesh formally applies for new $4-4.5b IMF loan => 8\n"
    "নতুন ৪-৪.৫ বিলিয়ন ডলার ঋণের জন্য আইএমএফে আবেদন বাংলাদেশের => 8\n"
    "GDP crosses $500bn for the first time => 8\n"
    "প্রথমবারের মতো জিডিপি ৫০০ বিলিয়ন ডলার ছাড়াল => 8\n"
    "NBR asks BB to require BIN for business bank accounts => 8\n"
    "ব্যবসায়িক ব্যাংক হিসাবে বিআইএন বাধ্যতামূলক করতে বললো এনবিআর => 8\n"
    "BERC cuts furnace oil price by Tk4.44/litre => 8\n"
    "ফার্নেস অয়েলের দাম লিটারে ৪.৪৪ টাকা কমাল বিইআরসি => 8\n"
    # YELLOW (6-7): banking health, aggregate FDI/EPZ, lender macro calls,
    # US Section 301, RMG figures, capital-market reform, governance process,
    # China-BD diplomacy, port/supply-chain disruption
    "Bangladesh has world's 2nd-highest NPL rate after Ukraine => 7\n"
    "ইউক্রেনের পর খেলাপি ঋণে বিশ্বে দ্বিতীয় বাংলাদেশ => 7\n"
    "Distressed loans soar to Tk10.87 lakh crore => 7\n"
    "চাপে থাকা ঋণ বেড়ে ১০.৮৭ লাখ কোটি টাকা => 7\n"
    "Chinese firms drive two-thirds of EPZ investments in FY26 => 7\n"
    "ইপিজেড বিনিয়োগের দুই-তৃতীয়াংশই চীনা প্রতিষ্ঠানের => 7\n"
    "ADB cuts Bangladesh growth forecast to 4.5% => 7\n"
    "বাংলাদেশের প্রবৃদ্ধির পূর্বাভাস ৪.৫ শতাংশে নামাল এডিবি => 7\n"
    "Bangladesh may face more tariffs from USTR => 7\n"
    "ইউএসটিআরের নতুন শুল্কের মুখে পড়তে পারে বাংলাদেশ => 7\n"
    "RMG exports to EU fall 19.3% YoY => 6\n"
    "ইইউতে তৈরি পোশাক রপ্তানি কমেছে ১৯.৩ শতাংশ => 6\n"
    "BSEC approves intraday trading => 6\n"
    "ইন্ট্রাডে ট্রেডিংয়ের অনুমোদন দিল বিএসইসি => 6\n"
    "Govt pushes 18th constitutional amendment; opposition walks out => 6\n"
    "১৮তম সংবিধান সংশোধনী নিয়ে এগোচ্ছে সরকার, ওয়াকআউট বিরোধীদের => 6\n"
    "Bangladesh-China ties enter new era, 17 agreements signed => 6\n"
    "নতুন যুগে বাংলাদেশ-চীন সম্পর্ক, ১৭ চুক্তি সই => 6\n"
    "Monsoon floods devastate Chattogram, port operations disrupted => 6\n"
    "বন্যায় বিপর্যস্ত চট্টগ্রাম, বন্দরের কার্যক্রম ব্যাহত => 6\n"
    # GREEN (5): standing geopolitics, tax-policy direction, telecom-adjacent
    # payments, routine circulars, single-company M&A
    "Russia-Ukraine: Trump pushes peace at G7 => 5\n"
    "জি৭-এ রাশিয়া-ইউক্রেন শান্তির তাগিদ ট্রাম্পের => 5\n"
    "Myanmar junta denies ASEAN access to Suu Kyi => 5\n"
    "সু চির সঙ্গে আসিয়ানের বৈঠকে বাধা মিয়ানমার জান্তার => 5\n"
    "Capital gains tax on gold set at 15% => 5\n"
    "স্বর্ণে মূলধনি মুনাফা কর ১৫ শতাংশ নির্ধারণ => 5\n"
    "Digital transactions up 13% in volume and value => 5\n"
    "ডিজিটাল লেনদেন বেড়েছে ১৩ শতাংশ => 5\n"
    "Bangla QR scales to 9.63 lakh merchants => 5\n"
    "বাংলা কিউআর পৌঁছাল ৯.৬৩ লাখ মার্চেন্টে => 5\n"
    "BB extends FC-Taka swap facility to EPZ exporters => 5\n"
    "ইপিজেড রপ্তানিকারকদের জন্য এফসি-টাকা সোয়াপ সুবিধা বাড়াল বিবি => 5\n"
    "Softlogic to acquire 60% of Diamond Life Insurance => 5\n"
    "ডায়মন্ড লাইফ ইন্স্যুরেন্সের ৬০ শতাংশ কিনছে সফটলজিক => 5\n"
    # MAGENTA (1-4): routine data / minor circulars (3-4), then pure noise (1-2)
    "Non-bank loans rose 2% in Jan-Mar => 4\n"
    "জানুয়ারি-মার্চে ব্যাংকবহির্ভূত ঋণ বেড়েছে ২ শতাংশ => 4\n"
    "Banks told to ensure uninterrupted savings certificate services => 4\n"
    "সঞ্চয়পত্র সেবা নিরবচ্ছিন্ন রাখার নির্দেশ ব্যাংকগুলোকে => 4\n"
    "Banks can now transfer consumer loans directly to vendors => 4\n"
    "ভোক্তা ঋণ সরাসরি বিক্রেতাকে দিতে পারবে ব্যাংক => 4\n"
    "BB relaxes rules on visa-fee remittances => 3\n"
    "ভিসা ফি রেমিট্যান্সের নিয়ম শিথিল করল বিবি => 3\n"
    "Wildfires ravage historic forest near Paris => 3\n"
    "প্যারিসের কাছে ঐতিহাসিক বনে ভয়াবহ দাবানল => 3\n"
    "10 injured as air conditioner explodes in Old Dhaka salon => 2\n"
    "পুরান ঢাকার সেলুনে এসি বিস্ফোরণে আহত ১০ => 2\n"
    "Food cart worker accused of molesting CU student => 2\n"
    "চবি শিক্ষার্থীকে হেনস্তার অভিযোগ ফুড কার্টকর্মীর বিরুদ্ধে => 2\n"
    "bdnews24 sponsored advertorial content => 2\n"
    "বিজ্ঞাপন/স্পন্সরড কনটেন্ট => 2\n"
    "Messi hat-trick sees Argentina beat Algeria => 1\n"
    "মেসির হ্যাটট্রিকে আলজেরিয়াকে হারাল আর্জেন্টিনা => 1\n"
    "Bengal's forgotten connections with Nazi Germany => 1\n"
    "নাৎসি জার্মানির সঙ্গে বাংলার ভুলে যাওয়া যোগসূত্র => 1"
)



# ── the data shapes the LLM returns ───────────────────────────────────────────
@dataclass
class Judgment:
    """Result of asking the LLM how relevant an article is to Grameenphone."""
    relevant: bool          # keep or drop (score >= threshold)
    score: float            # 1–10 relevance to Grameenphone (10 = critical)
    reason: str             # short explanation (for transparency in the UI)


@dataclass
class Summary:
    text: str               # the summary text


# ── the abstract interface every provider implements ──────────────────────────
class LLMProvider:
    """Base class. A real provider fills in judge() and summarize().
    `name` shows up in the admin/debug UI so you can see what's active."""
    name = "none"
    enabled = False

    def judge(self, title: str, source: str, sections: str) -> Judgment:
        raise NotImplementedError

    def verify_group(self, headlines: list) -> list:
        """Given a list of headlines proposed as the same story, return a list of
        group-id integers (same number = same event). Default: all one group."""
        return [0] * len(headlines)

    def summarize(self, title: str, source: str, body: str = "") -> Summary:
        raise NotImplementedError


# ── the default: a no-op provider (used until you add a real LLM) ──────────────
class NullProvider(LLMProvider):
    """Does nothing. Lets the whole app run with the LLM layer 'present but off'.
    judge() always keeps the article (so nothing is silently dropped), and
    summarize() returns empty. This is what's active now."""
    name = "none (LLM not configured)"
    enabled = False

    def judge(self, title, source, sections) -> Judgment:
        # neutral: keep everything, no opinion
        return Judgment(relevant=True, score=0.0, reason="LLM not configured")

    def summarize(self, title, source, body="") -> Summary:
        return Summary(text="")


# ── EXAMPLE skeleton for when you add a real LLM (left unwired on purpose) ─────
class ExampleProvider(LLMProvider):
    """A template showing where a real LLM would go. NOT active. When you're
    ready, copy this, fill in the API calls, and return it from get_provider().

    Example for an HTTP-based LLM API (pseudo-code in the methods):
    """
    name = "example (template — not active)"
    enabled = False

    def __init__(self, api_key: str = "", endpoint: str = "", model: str = ""):
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model

    def judge(self, title, source, sections) -> Judgment:
        # TODO when adding an LLM:
        #   prompt = f"Is this article relevant to a telecom risk team? ..."
        #   response = call_your_llm(prompt)
        #   parse response -> relevant / score / reason
        raise NotImplementedError("Fill in when wiring a real LLM")

    def summarize(self, title, source, body="") -> Summary:
        # TODO when adding an LLM:
        #   prompt = f"Summarize in 2 sentences: {title}\n{body}"
        #   text = call_your_llm(prompt)
        raise NotImplementedError("Fill in when wiring a real LLM")


# ── Ollama: free, local, private LLM (runs on your own machine) ───────────────
class OllamaProvider(LLMProvider):
    """Talks to a locally-running Ollama server (https://ollama.com).

    Setup (one time, on the machine that runs the app):
      1. Install Ollama from ollama.com
      2. Pull a model:   ollama pull llama3.1:8b   (or qwen2.5, gemma2, etc.)
      3. Ollama runs a local server at http://localhost:11434 automatically
      4. Start the app with:  LLM_PROVIDER=ollama
         (optional) LLM_MODEL=llama3.1:8b   to pick the model

    Nothing leaves your machine — ideal for ERM data. No API key, no cost.

    judge()     -> asks the model if a headline is relevant to telecom/ERM risk
    summarize() -> asks for a one-sentence summary
    """
    def __init__(self, model="qwen3:8b", host="http://localhost:11434"):
        self.model = model
        self.host = host.rstrip("/")
        self.name = f"ollama · {model}"
        # consider it enabled only if the server actually answers
        self.enabled = self._ping()

    def _ping(self):
        try:
            import requests
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def _generate(self, prompt, system="", fmt=None, num_predict=None, num_ctx=None):
        """Call Ollama's /api/generate and return the text response."""
        import requests, json
        options = {"temperature": 0.1}       # low temp = consistent judgments
        if num_predict:
            options["num_predict"] = num_predict   # raise output cap for big JSON
        if num_ctx:
            options["num_ctx"] = num_ctx           # raise INPUT context (default is small!)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "think": False,                       # qwen3: skip chain-of-thought (faster)
            "options": options,
        }
        if fmt:
            payload["format"] = fmt               # 'json' forces JSON output
        timeout = 300 if num_ctx else 120         # big-context calls run longer
        r = requests.post(f"{self.host}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json().get("response", "").strip()

    def _build_system(self):
        """Assemble the scoring system prompt: fixed guide + editable examples
        + optional analyst guidance."""
        system = FIXED_GUIDE
        examples = _get_relevance_examples()
        if examples.strip():
            system += "\n\nExamples (headline => score):\n" + examples.strip()
        guidance = _get_relevance_guidance()
        if guidance.strip():
            system += "\n\nAdditional analyst guidance:\n" + guidance.strip()
        return system

    def judge(self, title, source, sections) -> Judgment:
        """Score one headline's relevance to Grameenphone, 1–10, using the fixed
        editorial guide + few-shot examples + any analyst guidance."""
        import json
        system = self._build_system() + (
            "\n\nRespond ONLY with JSON: "
            "{\"score\": <1-10 integer>, \"reason\": \"<short phrase>\"}.")
        prompt = f"Headline: {title}\nSource: {source}\n\nRate relevance to Grameenphone."
        try:
            raw = self._generate(prompt, system=system, fmt="json")
            data = json.loads(raw)
            score = max(1, min(10, int(round(float(data.get("score", 5))))))
            return Judgment(relevant=(score >= 5), score=score,
                            reason=str(data.get("reason", ""))[:120])
        except Exception as e:
            return Judgment(relevant=True, score=5, reason=f"llm error: {e}")

    def judge_batch(self, items):
        """Score MANY headlines in ONE call (much faster than one-at-a-time).
        `items` = list of dicts with 'title' (and optionally 'title_en', 'source').
        Uses 'title_en' (the English translation) when present, so the
        English-only relevance guidance applies consistently to Bengali
        articles too, instead of relying on the model's implicit translation.
        Returns a list of Judgment, same order. Falls back to neutral on any
        parse failure so nothing is dropped.
        """
        import json
        if not items:
            return []
        numbered = "\n".join(
            f"{i+1}. {it.get('title_en') or it.get('title','')}"
            for i, it in enumerate(items))
        system = self._build_system() + (
            "\n\nYou will receive a numbered list of headlines. Score EACH one. "
            "Respond ONLY with JSON: {\"scores\": [{\"n\": <number>, "
            "\"score\": <1-10>, \"reason\": \"<short phrase>\"}, ...]} with exactly "
            f"{len(items)} entries, one per headline.")
        prompt = f"Headlines:\n{numbered}\n\nScore all {len(items)} headlines."
        try:
            raw = self._generate(prompt, system=system, fmt="json")
            data = json.loads(raw)
            by_n = {}
            for entry in data.get("scores", []):
                n = int(entry.get("n", 0))
                sc = max(1, min(10, int(round(float(entry.get("score", 5))))))
                by_n[n] = (sc, str(entry.get("reason", ""))[:120])
            out = []
            for i in range(len(items)):
                sc, reason = by_n.get(i + 1, (5, "no score returned"))
                out.append(Judgment(relevant=(sc >= 5), score=sc, reason=reason))
            return out
        except Exception as e:
            # whole-batch failure: keep all at neutral (never drop)
            return [Judgment(relevant=True, score=5, reason=f"llm error: {e}")
                    for _ in items]

    def cluster_topics_chunk(self, items, known_labels):
        """Assign a TOPIC label to each headline in ONE chunk, reusing labels
        already established by earlier chunks so the same real topic doesn't
        fragment into variants across chunks.

        This is Phase 1 of clustering. The caller feeds chunks SEQUENTIALLY,
        passing the growing `known_labels` list each time, so chunk N sees the
        canonical topics chunks 1..N-1 created and snaps to them when they fit —
        keeping the label space bounded even across 700-800 articles.

        `items`        = list of dicts with 'title' (and optionally 'title_en').
        `known_labels` = list of canonical topic-label strings so far.

        Returns a list of label strings, SAME ORDER as `items`. Missing/failed
        entries come back as "" (caller leaves them untagged — never blocks).
        Partial results are kept: if the model labels 48 of 50, those 48 stand
        and the 2 gaps are "".
        """
        import json
        if not items:
            return []
        numbered = "\n".join(
            f"{i+1}. {it.get('title_en') or it.get('title','')}"
            for i, it in enumerate(items))
        known_block = ""
        if known_labels:
            known_block = (
                "\n\nTOPICS ALREADY IN USE (reuse the EXACT label when a headline "
                "fits one of these; only invent a new label when none fits):\n"
                + "\n".join(f"- {l}" for l in known_labels))
        system = (
            "You group news headlines by TOPIC for a Bangladesh risk-analysis "
            "desk. A topic is a real-world theme, BROADER than a single event: "
            "different events in the same ongoing situation share one topic (e.g. "
            "a strike, a shipping-lane closure, and an LNG-supply cut during one "
            "Middle East crisis are ALL 'Middle East / oil'). Keep genuinely "
            "unrelated stories in separate topics — do NOT over-merge. Labels are "
            "SHORT (2-4 words, e.g. 'Middle East / oil', 'Banking / NPLs', "
            "'GP / spectrum', 'RMG exports')." + known_block + "\n\n"
            "Assign EVERY numbered headline a topic label. Return ONLY JSON: "
            "{\"labels\": [{\"n\": <number>, \"label\": \"<short label>\"}, ...]} "
            f"with one entry per headline ({len(items)} total), no preamble.")
        prompt = f"Headlines:\n{numbered}\n\nLabel all {len(items)}."
        try:
            raw = self._generate(prompt, system=system, fmt="json",
                                 num_predict=4096)
            data = json.loads(raw)
            by_n = {}
            for entry in data.get("labels", []):
                try:
                    n = int(entry.get("n", 0))
                except (ValueError, TypeError):
                    continue
                lab = str(entry.get("label", "")).strip()[:60]
                if lab:
                    by_n[n] = lab
            # partial-tolerant: fill gaps with "" instead of discarding the chunk
            return [by_n.get(i + 1, "") for i in range(len(items))]
        except Exception:
            return ["" for _ in items]   # whole-chunk failure → all untagged

    def consolidate_labels(self, labels):
        """Merge topic labels that name the SAME broad theme into one canonical
        label. Fixes fragmentation where Phase-1 chunked clustering invented
        several labels for one theme ('Middle East conflict', 'Iran-US tensions',
        'Gulf crisis' → one). Runs on the distinct labels only (~12-25 items), so
        it is a small, reliable call.

        `labels` = list of distinct label strings.
        Returns dict {original_label: canonical_label}. Labels the model leaves
        alone map to themselves. On failure returns identity (no merging).

        IMPORTANT: merge only labels that are the SAME theme. Do NOT merge
        related-but-distinct themes (e.g. 'Maritime security' and 'US-Iran
        conflict' stay separate even though both are geopolitics).
        """
        import json
        labels = [l for l in labels if l]
        if len(labels) < 2:
            return {l: l for l in labels}
        numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(labels))
        system = (
            "You are tidying a list of news-topic labels for a Bangladesh risk "
            "desk. Some labels name the SAME broad theme in different words and "
            "should be MERGED into one canonical label. Merge ONLY true synonyms "
            "of the same theme (e.g. 'Middle East conflict', 'Iran-US tensions', "
            "'US strikes on Iran' → one). Do NOT merge related-but-distinct themes "
            "(e.g. 'Maritime security' and 'US-Iran conflict' are DIFFERENT; "
            "'Banking / NPLs' and 'Capital markets' are DIFFERENT). When merging, "
            "pick the clearest, broadest existing label as the canonical one.\n\n"
            "Return ONLY JSON mapping EVERY input number to a canonical label "
            "string: {\"map\": [{\"n\": 1, \"canon\": \"<canonical label>\"}, ...]} "
            "with one entry per input label. Labels that stand alone map to "
            "themselves. No preamble.")
        prompt = f"Topic labels:\n{numbered}\n\nGive the canonical label for each."
        try:
            raw = self._generate(prompt, system=system, fmt="json",
                                 num_predict=2048)
            data = json.loads(raw)
            out = {}
            for entry in (data.get("map", []) if isinstance(data, dict) else []):
                try:
                    idx = int(entry.get("n", 0)) - 1
                except (ValueError, TypeError, AttributeError):
                    continue
                canon = str(entry.get("canon", "")).strip()[:60]
                if 0 <= idx < len(labels) and canon:
                    out[labels[idx]] = canon
            # any label the model skipped maps to itself
            for l in labels:
                out.setdefault(l, l)
            return out
        except Exception as e:
            if os.environ.get("TOPIC_DEBUG", "1") != "0":
                print(f"    [consolidate] FAILED ({type(e).__name__}: {e}) "
                      f"— no label merging")
            return {l: l for l in labels}

    def find_duplicates_batch(self, shown, candidates):
        """Simplest possible fold decision: given a few already-SHOWN articles
        and a small batch of CANDIDATE articles, say which candidates are just a
        restatement of the SAME story as one of the shown ones.

        The LLM does ONLY this judgment — no bookkeeping, no parent bookkeeping,
        no whole-topic partition. Python drives batching and everything else.
        Output is a tiny list of {candidate number -> shown number or 0}, which a
        9B model emits reliably (unlike the big partition schema that kept
        collapsing).

        `shown`      = list of dicts (title/title_en, source, content_text) —
                       the substantial cards found so far (may be empty).
        `candidates` = list of dicts — the batch to classify.

        Returns dict {candidate_index: shown_index}. A candidate NOT in the dict
        is NOT a duplicate (it becomes its own shown card). On failure returns
        {} (nothing folds — safe).
        """
        import json
        if not candidates:
            return {}

        def _title(m):
            return (m.get("title_en") or m.get("title") or "").strip()

        def _snip(m):
            return (m.get("content_text") or "").strip().replace("\n", " ")[:300]

        # cap the reference set so a big topic doesn't grow the prompt (and
        # per-call time) without bound — 30 representative headlines is plenty
        # to catch a restatement, and keeps every call fast.
        shown_ref = shown[:30]
        # SHOWN: headline only — they're already-decided reference points; sending
        # their bodies just bloats context and invites the model to echo them.
        shown_block = "\n".join(
            f"S{i+1}. {_title(m)}  — {m.get('source','')}"
            for i, m in enumerate(shown_ref)) or "(none yet)"
        # CANDIDATES: headline + a SHORT body snippet to judge sameness.
        cand_block = "\n".join(
            f"C{i+1}. {_title(m)}  — {m.get('source','')}"
            + (f" :: {_snip(m)}" if _snip(m) else "")
            for i, m in enumerate(candidates))

        system = (
            "You are de-duplicating news for a Bangladesh risk desk. You get a "
            "list of ALREADY-SHOWN stories (S1, S2, ...) and a batch of CANDIDATE "
            "stories (C1, C2, ...), all within one broad topic.\n\n"
            "For each CANDIDATE, decide: does it cover the SAME DEVELOPMENT as one "
            "of the SHOWN ones, with NOTHING MATERIALLY NEW? Fold restatements, "
            "retellings by other outlets, translations, follow-ups, reactions, and "
            "minor variations of a development that is already shown — if an "
            "analyst who read the shown story learns nothing important from the "
            "candidate, it folds. Use the body text when present; when an article "
            "has NO body text, judge from its HEADLINE alone — two near-identical "
            "headlines in the same topic are almost always the same development, "
            "so fold them.\n\n"
            "Keep a candidate separate ONLY if it is a genuinely NEW development: "
            "a different event, a major escalation or reversal, or significant new "
            "concrete facts. 'Iran closes Hormuz' vs 'US-Iran talks collapse' are "
            "different developments; five variations of 'US strikes Iran, dozens "
            "killed' are ONE development — fold the extras.\n\n"
            "When unsure, FOLD — a folded item stays one click away, it is not "
            "deleted.\n\n"
            "Return ONLY JSON: {\"dupes\": [{\"c\": <candidate number>, "
            "\"same_as\": <shown number>}, ...]}. Include a candidate ONLY if it "
            "folds into a shown story; omit candidates that are new/distinct. If "
            "none fold, return {\"dupes\": []}. No preamble.")
        prompt = (f"SHOWN stories:\n{shown_block}\n\n"
                  f"CANDIDATE stories:\n{cand_block}\n\n"
                  f"Which candidates fold into a shown story?")
        est_tokens = (len(system) + len(prompt)) // 4 + 256
        num_ctx = min(16384, max(4096, ((est_tokens // 2048) + 1) * 2048))
        try:
            raw = self._generate(prompt, system=system, fmt="json",
                                 num_predict=1024, num_ctx=num_ctx)
            data = json.loads(raw)
            out = {}
            for d in (data.get("dupes", []) if isinstance(data, dict) else []):
                try:
                    ci = int(d.get("c", 0)) - 1
                    si = int(d.get("same_as", 0)) - 1
                except (ValueError, TypeError, AttributeError):
                    continue
                if 0 <= ci < len(candidates) and 0 <= si < len(shown_ref):
                    out[ci] = si
            return out
        except Exception as e:
            if os.environ.get("TOPIC_DEBUG", "1") != "0":
                _r = locals().get("raw", "")
                print(f"    [find_dupes] batch of {len(candidates)} FAILED "
                      f"({type(e).__name__}: {e}) — none folded")
                print(f"    [find_dupes]   raw[:400]={_r[:400]!r}")
            return {}

    def summarize(self, title, source, body="") -> Summary:
        """Short 2-3 line summary for a risk briefing card."""
        system = ("Summarize this news item in 2 to 3 short sentences (roughly "
                  "30-50 words total) for a Grameenphone risk briefing. Lead with "
                  "the single most important fact or figure, then add why it matters "
                  "for the telecom/macro risk picture. No preamble, no bullet points, "
                  "plain sentences.")
        prompt = f"Headline: {title}\nSource: {source}\n{body}".strip()
        try:
            text = self._generate(prompt, system=system)
            return Summary(text=text[:400])
        except Exception:
            return Summary(text="")

    def translate_title(self, title: str) -> str:
        """Translate a Bengali (or any non-English) headline to English, for
        internal use by dedup/scoring. Plain text only, no commentary."""
        system = ("Translate this news headline to natural English. Output ONLY "
                  "the translated headline, nothing else — no quotes, no preamble, "
                  "no explanation.")
        try:
            text = self._generate(title, system=system).strip()
            return text.strip('"\u201c\u201d\'')[:300]
        except Exception:
            return ""

    def draft_bullet(self, title: str, body: str, section: str) -> str:
        """Rewrite an approved article into the house Daily-Risk-Insights bullet:
        a short bold hook, an en-dash, then a one-sentence risk interpretation.
        `body` is the FETCHED FULL ARTICLE TEXT (may be empty if the fetch
        failed, in which case this falls back to headline-only drafting).
        The user polishes before circulating, so this is a draft."""
        system = (
            "You write bullets for a Grameenphone Daily Risk Insights brief. "
            "Rewrite the item as ONE bullet in this exact style: a short bold "
            "hook of 2-5 words, then ' \u2013 ' (space-endash-space), then ONE "
            "sentence explaining the RISK SIGNAL for Bangladesh's telecom/macro "
            "environment. If the article text below contains specific NUMBERS, "
            "FIGURES, or NAMED ENTITIES (amounts, percentages, dates, "
            "institutions), cite the most important one — don't write a vague "
            "generality when a concrete figure is available. Draw out the "
            "implication (use framing like 'signalling', 'indicating', "
            "'reflecting' where natural). Be concise and analytical. Do NOT "
            "start with a bullet character. Output only the bullet text.\n"
            "Example: 'Port policy tension \u2013 Chattogram committee calls to halt "
            "leasing of two terminals (NCT, CCT), signalling potential delays "
            "in port modernisation.'")
        if body:
            prompt = f"Section: {section}\nHeadline: {title}\nArticle text: {body}"
        else:
            # fetch failed or gave nothing usable — draft from the headline alone
            prompt = f"Section: {section}\nHeadline: {title}\n(No article text available — draft from the headline only, do not invent figures.)"
        prompt = prompt.strip()
        try:
            text = self._generate(prompt, system=system).strip()
            # strip any leading bullet/dash the model may add
            return text.lstrip("\u2022-* ").strip()[:400]
        except Exception:
            return title

    def suggest_guidance(self, approved: list, rejected: list) -> str:
        """Analyse the user's approve/reject decisions and SUGGEST a short
        guidance addition (returned as plain text for the user to review — this
        never auto-applies). Human-in-the-loop by design."""
        appr = "\n".join(f"- {t}" for t in approved[:40])
        rej = "\n".join(f"- {t}" for t in rejected[:40])
        system = (
            "You help refine the relevance guidance for a Grameenphone risk-news "
            "scorer. You are given headlines the analyst APPROVED (relevant) and "
            "REJECTED (not relevant). Infer any PATTERN that the current rules may "
            "be missing — e.g. a topic consistently approved that isn't emphasised, "
            "or a topic consistently rejected that should be excluded.\n"
            "Output 2-4 short imperative guidance sentences the analyst can ADD to "
            "their existing guidance. Be specific and conservative — only suggest "
            "what the decisions clearly support. No preamble, just the sentences.")
        prompt = f"APPROVED (relevant):\n{appr}\n\nREJECTED (not relevant):\n{rej}"
        return self._generate(prompt, system=system)[:600]

    def verify_group(self, headlines: list) -> list:
        """Group headlines by whether they report the SAME SPECIFIC EVENT.
        Conservative by design: when in doubt, keep separate. This is the
        primary grouping decision (embeddings only pre-cluster tight matches).
        Returns a sub-group id per headline (same id = same event)."""
        import json
        if len(headlines) <= 1:
            return [0] * len(headlines)
        numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
        system = (
            "You decide which news headlines report the EXACT SAME specific event "
            "(same who, what, and when) — i.e. the same story carried by different "
            "outlets. Headlines may be English or Bengali; read both carefully.\n\n"
            "BE CONSERVATIVE. Only group two headlines together if you are confident "
            "they describe the SAME single event. When in doubt, keep them SEPARATE.\n"
            "Do NOT group merely because headlines:\n"
            "- share a topic or theme (e.g. both about the economy, both about Iran),\n"
            "- mention the same country, person, or organisation,\n"
            "- use similar words or are both in Bengali.\n"
            "Different companies, different incidents, different announcements, or "
            "different days = DIFFERENT events = different groups.\n\n"
            "Assign each headline an integer group id. Headlines that match no other "
            "get their own unique id. Respond ONLY with JSON: "
            "{\"groups\": [one integer per headline]}. "
            "Example (only 1 and 3 are the same event): {\"groups\": [0, 1, 0]}. "
            "Example (all different): {\"groups\": [0, 1, 2]}."
        )
        prompt = (f"Headlines:\n{numbered}\n\nGroup ONLY headlines that are clearly "
                  f"the same specific event. Default to separate.")
        try:
            raw = self._generate(prompt, system=system, fmt="json")
            groups = json.loads(raw).get("groups", [])
            if len(groups) == len(headlines):
                return [int(g) for g in groups]
        except Exception:
            pass
        # on failure, keep ALL SEPARATE (conservative — never wrongly merge)
        return list(range(len(headlines)))


# ── Groq: hosted API (fast, no local GPU/RAM needed) ─────────────────────────
class GroqProvider(OllamaProvider):
    """Talks to Groq's OpenAI-compatible chat-completions API.

    Setup (one time):
      1. Get a free key at https://console.groq.com/keys
      2. Start the app with:
           LLM_PROVIDER=groq  GROQ_API_KEY=gsk_...

    It SUBCLASSES OllamaProvider on purpose. Every prompt method in this file
    (judge, judge_batch, cluster_topics_chunk, consolidate_labels,
    find_duplicates_batch, translate_title, draft_bullet, suggest_guidance,
    verify_group, summarize) funnels through _generate(). So overriding just
    _ping + _generate swaps the transport and inherits all eleven prompts
    unchanged — identical wording, JSON parsing, and fallback behaviour. There
    is no second copy of any prompt to keep in sync.

    UNLIKE Ollama, this is a metered network service, so _generate adds what a
    localhost call never needed: retry with exponential backoff on 429 (rate
    limit) and 5xx, honouring the Retry-After header when Groq sends one. A run
    makes hundreds of calls, so without this a single burst would fail a batch.

    NOTE: unlike Ollama, headlines and article snippets leave the machine.
    """
    def __init__(self, api_key="", model="llama-3.3-70b-versatile",
                 host="https://api.groq.com/openai/v1"):
        self.api_key = api_key
        self.model = model
        self.host = host.rstrip("/")
        self.name = f"groq · {model}"
        self.enabled = self._ping()

    def _ping(self):
        """Enabled only if we have a key AND Groq answers. Also verifies the
        configured model actually exists — the failure mode that bit the Ollama
        setup (a model name that was never pulled) reports 'LLM on' and then
        fails on every call, so we check it up front and say so loudly."""
        if not self.api_key:
            print("[groq] no GROQ_API_KEY set — LLM stays off")
            return False
        try:
            import requests
            r = requests.get(f"{self.host}/models",
                             headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=10)
            if r.status_code == 401:
                print("[groq] API key rejected (401) — LLM stays off")
                return False
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])]
            if ids and self.model not in ids:
                print(f"[groq] model {self.model!r} not available. "
                      f"Set LLM_MODEL to one of: {', '.join(sorted(filter(None, ids)))}")
                return False
            return True
        except Exception as e:
            print(f"[groq] unreachable ({e}) — LLM stays off")
            return False

    def _generate(self, prompt, system="", fmt=None, num_predict=None, num_ctx=None):
        """Same contract as OllamaProvider._generate: return the response text.
        `num_ctx` is ignored — context size is fixed server-side by the model,
        there is no client-side knob to raise it."""
        import requests, time
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,          # low temp = consistent judgments
        }
        if num_predict:
            payload["max_tokens"] = num_predict
        if fmt == "json":
            # Groq's JSON mode requires the word "JSON" somewhere in the
            # messages — every prompt here already says "Respond ONLY with JSON".
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}"}
        attempts = int(os.environ.get("GROQ_RETRIES", "4"))
        delay = 2.0
        for attempt in range(attempts):
            try:
                r = requests.post(f"{self.host}/chat/completions",
                                  json=payload, headers=headers, timeout=120)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == attempts - 1:
                        r.raise_for_status()
                    # Groq tells us exactly how long to wait when it can
                    wait = float(r.headers.get("Retry-After") or delay)
                    print(f"[groq] {r.status_code} — retrying in {wait:.0f}s "
                          f"({attempt + 1}/{attempts})")
                    time.sleep(wait)
                    delay *= 2
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except requests.exceptions.Timeout:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        return ""


# ── the single place the app asks "which LLM should I use?" ────────────────────
_provider_singleton: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the active LLM provider. The whole app calls THIS, never a
    specific provider — so swapping in a real LLM later is a one-line change.

    Selection is driven by an environment variable so you don't edit code:
        LLM_PROVIDER=none      -> NullProvider (default, current)
        LLM_PROVIDER=groq      -> GroqProvider (hosted, fast; needs GROQ_API_KEY)
        LLM_PROVIDER=ollama    -> OllamaProvider (free, local, fully private)
        LLM_PROVIDER=example   -> ExampleProvider (template)
    For ollama you can also set LLM_MODEL (default qwen3:8b).
    For groq set GROQ_API_KEY, and optionally LLM_MODEL.
    """
    global _provider_singleton
    if _provider_singleton is not None:
        return _provider_singleton

    choice = os.environ.get("LLM_PROVIDER", "none").lower()
    if choice == "groq":
        _provider_singleton = GroqProvider(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"),
            host=os.environ.get("LLM_HOST", "https://api.groq.com/openai/v1"),
        )
    elif choice == "ollama":
        _provider_singleton = OllamaProvider(
            model=os.environ.get("LLM_MODEL", "qwen3:8b"),
            host=os.environ.get("LLM_HOST", "http://localhost:11434"),
        )
    elif choice == "example":
        _provider_singleton = ExampleProvider(
            api_key=os.environ.get("LLM_API_KEY", ""),
            endpoint=os.environ.get("LLM_ENDPOINT", ""),
            model=os.environ.get("LLM_MODEL", ""),
        )
    else:
        _provider_singleton = NullProvider()
    return _provider_singleton


def provider_status() -> dict:
    """Small helper for the admin/debug UI to show what's active."""
    p = get_provider()
    return {"name": p.name, "enabled": p.enabled}
