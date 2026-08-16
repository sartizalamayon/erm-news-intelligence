"""P3 prompt templates and the deterministic parts of prompt assembly —
byte-identical to `backend/llm.py`, per docs/p3-contract.md (the frozen
behavioral contract). Nothing here is paraphrased or "improved": every
system/user prompt string below is either copied verbatim from
`backend/llm.py` (FIXED_GUIDE, DEFAULT_EXAMPLES — extracted by a script
reading the exact source line ranges, never hand-retyped, to rule out
transcription drift in the Bengali text) or reconstructed to produce the
IDENTICAL string value `llm.py`'s own f-string/concatenation would
produce for the same inputs (verified in tests/test_p3_prompts.py by
diffing against p3-contract.md's fenced blocks).

`FIXED_GUIDE` is the non-editable editorial backbone (p3-contract.md
§2.1). `DEFAULT_EXAMPLES` is the fallback few-shot block used whenever
the admin-editable `relevance_examples` setting is unset/blank — which
p3-contract.md's header notes is true of every real production run to
date (the `settings` table only ever had `report_cache` populated).

Per ADR §9.1 / the mission's THREE FACTS: the PROMPT is frozen, the
MODEL is not. Nothing in this file decides which provider or model runs
these prompts — that is app.services.llm.routing's job.
"""

from __future__ import annotations

from typing import Any

from app.db.connection import DictConnection
from app.db.settings import get_setting
from app.logging import get_logger

logger = get_logger(__name__)

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


# ── Admin-editable guidance/examples (p3-contract.md §1.2, §2.3) ───────
#
# Mirrors `llm.py`'s `_get_relevance_guidance()` / `_get_relevance_examples()`
# (llm.py:28-55) exactly: both read from the `settings` key/value table
# (0001_settings.sql — the same table legacy's SQLite `settings` table
# carries forward into, per that migration's own docstring), falling
# back to '' (guidance) or DEFAULT_EXAMPLES (examples) when unset or
# blank. `app/api/routers/prompts.py` (ADMIN-API's stub, "PUT
# /prompts/guidance", "PUT /prompts/examples") is the intended future
# caller of the setter half of this pair.

RELEVANCE_GUIDANCE_KEY = "relevance_guidance"
RELEVANCE_EXAMPLES_KEY = "relevance_examples"


def get_relevance_guidance(conn: DictConnection) -> str:
    """`_get_relevance_guidance()` (llm.py:28-39): settings value for
    `relevance_guidance`, or '' if unset/on any error. The DB read here
    can't silently except-and-return-'' the way legacy's try/except did
    (legacy was guarding against a totally separate SQLite connection
    module failing to import) — a missing row is simply '' by
    `get_setting`'s own default, which is the same net behavior.
    """
    return get_setting(conn, RELEVANCE_GUIDANCE_KEY, default="")


def get_relevance_examples(conn: DictConnection) -> str:
    """`_get_relevance_examples()` (llm.py:42-55): settings value for
    `relevance_examples` if set and non-blank, else `DEFAULT_EXAMPLES`.
    """
    value = get_setting(conn, RELEVANCE_EXAMPLES_KEY, default="")
    if value.strip():
        return value
    return DEFAULT_EXAMPLES


def build_system_guide(examples: str, guidance: str) -> str:
    """`_build_system()` (llm.py:354-364): FIXED_GUIDE + editable
    examples + editable analyst guidance, in that order. Both extra
    sections are appended ONLY if non-blank after stripping.
    """
    system = FIXED_GUIDE
    if examples.strip():
        system += "\n\nExamples (headline => score):\n" + examples.strip()
    if guidance.strip():
        system += "\n\nAdditional analyst guidance:\n" + guidance.strip()
    return system


def _stage_body(instructions: str | None, default: str) -> str:
    """The editable half of a per-stage system prompt.

    `None` (no DB, or nothing usable stored) means "use the built-in",
    which is what keeps every stage byte-identical to the frozen contract
    until somebody actually edits it. A stored value is stripped, and a
    value that strips to nothing is treated as absent rather than as an
    instruction to send an empty system prompt — the last-ditch guard
    behind `stage_prompt_problem`, for the case where a blank slips past
    it (a stored string of only newlines, say).
    """
    if instructions is None:
        return default
    stripped = instructions.strip()
    return stripped or default


# ── 1.1 judge() — single-item relevance score. DEAD IN PRODUCTION ──────
# (p3-contract.md §1.1 / §7 — judge_batch() is the live call site) but
# documented and implemented in full per the "extract everything
# verbatim" mandate.


def judge_system(base_system: str) -> str:
    return base_system + (
        "\n\nRespond ONLY with JSON: "
        '{"score": <1-10 integer>, "reason": "<short phrase>"}.'
    )


def judge_prompt(title: str, source: str) -> str:
    return f"Headline: {title}\nSource: {source}\n\nRate relevance to Grameenphone."


# ── 1.2 judge_batch() — the LIVE relevance-scoring call ────────────────


def _numbered_titles(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"{i+1}. {it.get('title_en') or it.get('title','')}" for i, it in enumerate(items))


def judge_batch_system(base_system: str, n: int) -> str:
    return base_system + (
        "\n\nYou will receive a numbered list of headlines. Score EACH one. "
        'Respond ONLY with JSON: {"scores": [{"n": <number>, '
        '"score": <1-10>, "reason": "<short phrase>"}, ...]} with exactly '
        f"{n} entries, one per headline."
    )


def judge_batch_prompt(items: list[dict[str, Any]]) -> str:
    numbered = _numbered_titles(items)
    return f"Headlines:\n{numbered}\n\nScore all {len(items)} headlines."


# ── 1.3 cluster_topics_chunk() — topic labelling, Phase 1 ──────────────

CLUSTER_TOPICS_NUM_PREDICT = 4096  # llm.py:466-467

# The EDITABLE half of this stage's system prompt (see "Per-stage
# editable prompts" at the foot of this module). Everything after it —
# the running list of topics already in use, and the JSON envelope the
# parser reads — is assembled by code and is NOT editable, because a
# well-meant rewrite that drops the `{"labels": [...]}` contract does not
# produce worse topics, it produces NO topics.
DEFAULT_TOPIC_CLUSTERING_PROMPT = (
    "You group news headlines by TOPIC for a Bangladesh risk-analysis "
    "desk. A topic is a real-world theme, BROADER than a single event: "
    "different events in the same ongoing situation share one topic (e.g. "
    "a strike, a shipping-lane closure, and an LNG-supply cut during one "
    "Middle East crisis are ALL \'Middle East / oil\'). Keep genuinely "
    "unrelated stories in separate topics — do NOT over-merge. Labels are "
    "SHORT (2-4 words, e.g. \'Middle East / oil\', \'Banking / NPLs\', "
    "\'GP / spectrum\', \'RMG exports\')."
)


def cluster_topics_system(known_labels: list[str], n: int, instructions: str | None = None) -> str:
    known_block = ""
    if known_labels:
        known_block = (
            "\n\nTOPICS ALREADY IN USE (reuse the EXACT label when a headline "
            "fits one of these; only invent a new label when none fits):\n"
            + "\n".join(f"- {label}" for label in known_labels)
        )
    body = _stage_body(instructions, DEFAULT_TOPIC_CLUSTERING_PROMPT)
    return (
        body + known_block + "\n\n"
        "Assign EVERY numbered headline a topic label. Return ONLY JSON: "
        '{"labels": [{"n": <number>, "label": "<short label>"}, ...]} '
        f"with one entry per headline ({n} total), no preamble."
    )


def cluster_topics_prompt(items: list[dict[str, Any]]) -> str:
    numbered = _numbered_titles(items)
    return f"Headlines:\n{numbered}\n\nLabel all {len(items)}."


# ── 1.4 consolidate_labels() — topic-label merge ────────────────────────

CONSOLIDATE_LABELS_NUM_PREDICT = 2048  # llm.py:518-519


def consolidate_labels_system(n: int) -> str:
    return (
        "You are tidying a list of news-topic labels for a Bangladesh risk "
        "desk. Some labels name the SAME broad theme in different words and "
        "should be MERGED into one canonical label. Merge ONLY true synonyms "
        "of the same theme (e.g. \'Middle East conflict\', \'Iran-US tensions\', "
        "\'US strikes on Iran\' → one). Do NOT merge related-but-distinct themes "
        "(e.g. \'Maritime security\' and \'US-Iran conflict\' are DIFFERENT; "
        "\'Banking / NPLs\' and \'Capital markets\' are DIFFERENT). When merging, "
        "pick the clearest, broadest existing label as the canonical one.\n\n"
        "Return ONLY JSON mapping EVERY input number to a canonical label "
        'string: {"map": [{"n": 1, "canon": "<canonical label>"}, ...]} '
        "with one entry per input label. Labels that stand alone map to "
        "themselves. No preamble."
    )


def consolidate_labels_prompt(labels: list[str]) -> str:
    numbered = "\n".join(f"{i+1}. {label}" for i, label in enumerate(labels))
    return f"Topic labels:\n{numbered}\n\nGive the canonical label for each."


# ── 1.5 find_duplicates_batch() — topic-fold / standout decision ───────

FIND_DUPLICATES_NUM_PREDICT = 1024  # llm.py:614-615
FIND_DUPLICATES_SHOWN_CAP = 30  # llm.py:572, hard-coded shown[:30]
FIND_DUPLICATES_SNIPPET_MAXLEN = 300  # llm.py:567, _snip()


def _fd_title(m: dict[str, Any]) -> str:
    return (m.get("title_en") or m.get("title") or "").strip()


def _fd_snip(m: dict[str, Any]) -> str:
    return (m.get("content_text") or "").strip().replace("\n", " ")[:FIND_DUPLICATES_SNIPPET_MAXLEN]


def find_duplicates_system() -> str:
    return (
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
        "concrete facts. \'Iran closes Hormuz\' vs \'US-Iran talks collapse\' are "
        "different developments; five variations of \'US strikes Iran, dozens "
        "killed\' are ONE development — fold the extras.\n\n"
        "When unsure, FOLD — a folded item stays one click away, it is not "
        "deleted.\n\n"
        'Return ONLY JSON: {"dupes": [{"c": <candidate number>, '
        '"same_as": <shown number>}, ...]}. Include a candidate ONLY if it '
        "folds into a shown story; omit candidates that are new/distinct. If "
        'none fold, return {"dupes": []}. No preamble.'
    )


def find_duplicates_shown_block(shown: list[dict[str, Any]]) -> str:
    shown_ref = shown[:FIND_DUPLICATES_SHOWN_CAP]
    return "\n".join(f"S{i+1}. {_fd_title(m)}  — {m.get('source','')}" for i, m in enumerate(shown_ref)) or (
        "(none yet)"
    )


def find_duplicates_candidate_block(candidates: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"C{i+1}. {_fd_title(m)}  — {m.get('source','')}" + (f" :: {_fd_snip(m)}" if _fd_snip(m) else "")
        for i, m in enumerate(candidates)
    )


def find_duplicates_prompt(shown: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    shown_block = find_duplicates_shown_block(shown)
    cand_block = find_duplicates_candidate_block(candidates)
    return (
        f"SHOWN stories:\n{shown_block}\n\n"
        f"CANDIDATE stories:\n{cand_block}\n\n"
        f"Which candidates fold into a shown story?"
    )


def find_duplicates_num_ctx(system: str, prompt: str) -> int:
    """llm.py:611-612's dynamic context-window estimate (Ollama-only
    knob; Groq ignores num_ctx entirely per p3-contract.md §4/§8, but the
    VALUE is still computed identically so a future Ollama-shaped
    transport — or a test asserting call shape — sees the same number).
    """
    est_tokens = (len(system) + len(prompt)) // 4 + 256
    return min(16384, max(4096, ((est_tokens // 2048) + 1) * 2048))


# ── 1.6 summarize() — per-article summary. DEAD (zero callers) ─────────


def summarize_system() -> str:
    return (
        "Summarize this news item in 2 to 3 short sentences (roughly "
        "30-50 words total) for a Grameenphone risk briefing. Lead with "
        "the single most important fact or figure, then add why it matters "
        "for the telecom/macro risk picture. No preamble, no bullet points, "
        "plain sentences."
    )


def summarize_prompt(title: str, source: str, body: str = "") -> str:
    return f"Headline: {title}\nSource: {source}\n{body}".strip()


# ── 1.7 translate_title() — headline translation ───────────────────────
# Note (p3-contract.md §1.7): the prompt itself IS the raw title, with NO
# surrounding template — the caller passes `title` directly as the user
# turn, all instruction carried in `system`.


def translate_title_system() -> str:
    return (
        "Translate this news headline to natural English. Output ONLY "
        "the translated headline, nothing else — no quotes, no preamble, "
        "no explanation."
    )


# ── 1.7b translate_titles_batch() — the BATCHED form of the above ──────
#
# The single-title call above is faithful to legacy (p3-contract.md §1.7:
# "the prompt IS the raw title, no template") and is kept, unchanged, as
# the fallback path. But one HTTP round-trip per headline does not scale:
# a real backlog day is 1,376 non-English titles, which at one call each
# is 1,376 sequential-ish round-trips — minutes of pure latency for work
# that is a single sentence of translation.
#
# Batching mirrors `judge_batch` exactly (numbered list in, JSON keyed by
# the same numbers out), so the shape is already proven in this codebase
# and the parser can reuse the same per-item-missing-index defaulting.
# Translation quality is unaffected: each headline is still translated on
# its own terms, just in one request.


DEFAULT_TRANSLATION_PROMPT = (
    "You translate news headlines to natural English for a "
    "Bangladesh risk-analysis desk.\n\n"
    "You will receive a numbered list of headlines in mixed languages "
    "(mostly Bengali, some already English). Translate EACH one to "
    "natural English. If a headline is already English, return it "
    "unchanged."
)


def translate_titles_batch_system(n: int, instructions: str | None = None) -> str:
    return (
        _stage_body(instructions, DEFAULT_TRANSLATION_PROMPT) + "\n\n"
        'Respond ONLY with JSON: {"translations": [{"n": <number>, '
        '"text": "<english headline>"}, ...]} with exactly '
        f"{n} entries, one per input headline, numbered to match. "
        "No preamble, no explanation, no extra keys."
    )


def translate_titles_batch_prompt(titles: list[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    return f"Headlines:\n{numbered}\n\nTranslate all {len(titles)} headlines."


# ── 1.8 draft_bullet() — report-bullet drafting ─────────────────────────
# Downstream of P3 scoring (report-generation stage, not the collection
# pipeline itself) but still an llm.py prompt method, reproduced here per
# the mission's framing of llm.py as "the P3 core" and DoD #1's routing
# surface needing a "summarization" feature slot.


# Fully editable: unlike every other stage prompt, this one carries no
# machine-readable envelope - the response is prose. `app.services.brief`
# splits on the endash separator and, when it is absent, keeps the whole
# line as the bullet's lead, so even a rewrite that drops the format
# instruction degrades to a plain bullet rather than an empty brief.
DEFAULT_SUMMARIZATION_PROMPT = (
    "You write bullets for a Grameenphone Daily Risk Insights brief. "
    "Rewrite the item as ONE bullet in this exact style: a short bold "
    "hook of 2-5 words, then \' \u2013 \' (space-endash-space), then ONE "
    "sentence explaining the RISK SIGNAL for Bangladesh\'s telecom/macro "
    "environment. If the article text below contains specific NUMBERS, "
    "FIGURES, or NAMED ENTITIES (amounts, percentages, dates, "
    "institutions), cite the most important one — don\'t write a vague "
    "generality when a concrete figure is available. Draw out the "
    "implication (use framing like \'signalling\', \'indicating\', "
    "\'reflecting\' where natural). Be concise and analytical. Do NOT "
    "start with a bullet character. Output only the bullet text.\n"
    "Example: \'Port policy tension \u2013 Chattogram committee calls to halt "
    "leasing of two terminals (NCT, CCT), signalling potential delays "
    "in port modernisation.\'"
)


def draft_bullet_system(instructions: str | None = None) -> str:
    return _stage_body(instructions, DEFAULT_SUMMARIZATION_PROMPT)


def draft_bullet_prompt(title: str, body: str, section: str) -> str:
    if body:
        prompt = f"Section: {section}\nHeadline: {title}\nArticle text: {body}"
    else:
        prompt = (
            f"Section: {section}\nHeadline: {title}\n"
            "(No article text available — draft from the headline only, do not invent figures.)"
        )
    return prompt.strip()


# ── 1.9 suggest_guidance() — guidance-refinement suggestion ────────────
# Human-in-the-loop admin feature (frontend-contract.md §3.9 "POST
# /prompts/suggest"; app/api/routers/prompts.py's stub names this as
# P3-LLM's action to implement). Never auto-applied.


def suggest_guidance_system() -> str:
    return (
        "You help refine the relevance guidance for a Grameenphone risk-news "
        "scorer. You are given headlines the analyst APPROVED (relevant) and "
        "REJECTED (not relevant). Infer any PATTERN that the current rules may "
        "be missing — e.g. a topic consistently approved that isn\'t emphasised, "
        "or a topic consistently rejected that should be excluded.\n"
        "Output 2-4 short imperative guidance sentences the analyst can ADD to "
        "their existing guidance. Be specific and conservative — only suggest "
        "what the decisions clearly support. No preamble, just the sentences."
    )


def suggest_guidance_prompt(approved: list[str], rejected: list[str]) -> str:
    appr = "\n".join(f"- {t}" for t in approved[:40])
    rej = "\n".join(f"- {t}" for t in rejected[:40])
    return f"APPROVED (relevant):\n{appr}\n\nREJECTED (not relevant):\n{rej}"


# ── 1.10 verify_group() — same-event verification ──────────────────────


# The EDITABLE half of the duplicate-verification system prompt. The
# JSON envelope below it is appended by `verify_group_system` and is
# deliberately out of reach: `parse_verify_group` needs the
# {"groups": [...]} shape, and without it every article in a proposed
# group is left ungrouped rather than merely grouped badly.
DEFAULT_DEDUP_VERIFICATION_PROMPT = (
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
    "different days = DIFFERENT events = different groups."
)


def verify_group_system(instructions: str | None = None) -> str:
    return (
        _stage_body(instructions, DEFAULT_DEDUP_VERIFICATION_PROMPT) + "\n\n"
        "Assign each headline an integer group id. Headlines that match no other "
        "get their own unique id. Respond ONLY with JSON: "
        '{"groups": [one integer per headline]}. '
        'Example (only 1 and 3 are the same event): {"groups": [0, 1, 0]}. '
        'Example (all different): {"groups": [0, 1, 2]}.'
    )


def verify_group_prompt(headlines: list[str]) -> str:
    numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
    return (
        f"Headlines:\n{numbered}\n\n"
        "Group ONLY headlines that are clearly the same specific event. Default to separate."
    )


# ═══════════════════════════════════════════════════════════════════════
# Per-stage editable prompts
# ═══════════════════════════════════════════════════════════════════════
#
# Scoring has had two admin-editable slots (guidance, examples) since
# legacy. The other four AI stages had their instructions frozen in this
# file, which meant the only way to change how headlines are translated,
# how duplicates are judged, how topics are named, or how brief bullets
# are written was a code deploy.
#
# The storage shape here is deliberately the SAME one guidance/examples
# already use, not a new one: the ACTIVE text lives in a plain
# `settings` key (below), version history is JSON alongside it under
# `prompt_versions:{field}`, and `app.services.admin` drives both. So a
# stage prompt gets version history and "activate a previous version"
# for free, and there is exactly one mechanism to understand.
#
# What is editable is the INSTRUCTION half only. Every stage except
# summarization appends a machine-readable envelope (the JSON shape its
# parser reads) that no edit can reach — see each DEFAULT_* constant's
# own note. The distinction matters: an analyst rewriting the prose gets
# different judgements, which is the point; an analyst deleting
# `Return ONLY JSON: {"labels": ...}` would get NO judgements, which is
# an outage.

TRANSLATION_PROMPT_KEY = "stage_prompt:translation"
DEDUP_VERIFICATION_PROMPT_KEY = "stage_prompt:dedup_verification"
TOPIC_CLUSTERING_PROMPT_KEY = "stage_prompt:topic_clustering"
SUMMARIZATION_PROMPT_KEY = "stage_prompt:summarization"

# Keyed by `app.services.llm.routing`'s feature names, so a stage's
# prompt and its provider routing are looked up under the same word.
STAGE_PROMPT_KEYS: dict[str, str] = {
    "translation": TRANSLATION_PROMPT_KEY,
    "dedup_verification": DEDUP_VERIFICATION_PROMPT_KEY,
    "topic_clustering": TOPIC_CLUSTERING_PROMPT_KEY,
    "summarization": SUMMARIZATION_PROMPT_KEY,
}

DEFAULT_STAGE_PROMPTS: dict[str, str] = {
    "translation": DEFAULT_TRANSLATION_PROMPT,
    "dedup_verification": DEFAULT_DEDUP_VERIFICATION_PROMPT,
    "topic_clustering": DEFAULT_TOPIC_CLUSTERING_PROMPT,
    "summarization": DEFAULT_SUMMARIZATION_PROMPT,
}

# A ceiling, not a style rule. Every one of these prompts is prepended to
# a batch of headlines and sent on EVERY call of its stage, so a prompt
# that ran away (a paste of an entire article archive, a runaway edit)
# would blow the context window on every request for the rest of the day
# and cost real quota doing it. The built-ins are all under 2,000
# characters; 20,000 leaves room to be genuinely verbose first.
MAX_STAGE_PROMPT_CHARS = 20_000


def is_stage_prompt_field(field: str) -> bool:
    return field in STAGE_PROMPT_KEYS


def stage_prompt_problem(value: object) -> str:
    """Why `value` cannot be used as a stage prompt, in words an analyst
    can act on, or `""` when it is fine.

    Empty is the failure that actually happens: somebody selects all,
    deletes, and saves before pasting the replacement. Sending an empty
    system prompt does not degrade the stage, it unmoors it completely,
    and the damage is silent because the model still answers.
    """
    if not isinstance(value, str):
        return "the saved prompt is not text"
    if not value.strip():
        return "the saved prompt is empty"
    if len(value) > MAX_STAGE_PROMPT_CHARS:
        return f"the saved prompt is longer than {MAX_STAGE_PROMPT_CHARS:,} characters"
    return ""


def resolve_stage_prompt(conn: DictConnection | None, field: str) -> tuple[str, str]:
    """`(prompt_text, fallback_reason)` for one stage.

    `fallback_reason` is `""` on the happy path (stored text is usable,
    or nothing is stored and the built-in is the intended answer) and a
    plain-English reason when a stored value had to be REJECTED in favour
    of the built-in. The caller surfaces it; the admin console shows it
    on the stage's card so a broken save is visible without reading logs.
    """
    if not is_stage_prompt_field(field):
        raise KeyError(f"unknown stage prompt field {field!r}")
    default = DEFAULT_STAGE_PROMPTS[field]
    if conn is None:
        return default, ""
    stored = get_setting(conn, STAGE_PROMPT_KEYS[field], default="")
    if not stored:
        # No row, or a row explicitly cleared to "" — which this codebase
        # treats as identical to "never configured" everywhere else
        # (`get_setting`'s own contract; see `_reset_llm_settings` in
        # tests/test_admin_prompts.py). Not a fallback, just the shipped
        # default, so it reports no problem: a warning here would appear
        # on every stage of a fresh install.
        #
        # Whitespace, by contrast, is a real save of nothing and falls
        # through to `stage_prompt_problem` below.
        return default, ""
    problem = stage_prompt_problem(stored)
    if problem:
        logger.warning(
            "stage_prompt_fell_back_to_builtin",
            extra={"field": field, "reason": problem, "stored_length": len(stored)},
        )
        return default, problem
    return stored, ""


def get_stage_prompt(conn: DictConnection | None, field: str) -> str:
    """The text a stage should actually send, built-in or stored."""
    return resolve_stage_prompt(conn, field)[0]
