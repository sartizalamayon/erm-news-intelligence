"""
compare_models.py — test candidate embedding models BEFORE committing to one
==============================================================================
Prints cosine similarity for several headline pairs (real duplicates that
SHOULD score high, and unrelated pairs that should score low) under each
candidate model, side by side. Use this to pick a model that actually
discriminates well on YOUR kind of headlines before changing EMBED_MODEL.

Run:
    python compare_models.py

Each candidate model is a separate download (~1-2GB each) the first time it's
used — this WILL take a few minutes and several GB of disk on first run.
"""

from sentence_transformers import SentenceTransformer, util

# (label, headline_a, headline_b, should_match)
TEST_PAIRS = [
    ("GP spectrum (EN/EN, different wording)",
     "Grameenphone wins spectrum auction",
     "GP secures spectrum in BTRC auction", True),
    ("GP spectrum (EN/BN, same event)",
     "Grameenphone wins spectrum auction",
     "গ্রামীণফোন স্পেকট্রাম নিলামে জয়ী", True),
    ("Unrelated EN/EN",
     "Grameenphone wins spectrum auction",
     "Local festival draws large crowds", False),
    ("Unrelated BN/BN (different topics)",
     "সরকার নতুন বাজেট ঘোষণা করেছে",
     "ঢাকায় বৃষ্টিপাত অব্যাহত", False),
    ("Same event, BN/BN, different wording",
     "বাংলাদেশ ব্যাংকের নতুন সুদের হার নির্ধারণ",
     "কেন্দ্রীয় ব্যাংক সুদহার পরিবর্তন করলো", True),
]

# candidates to compare — add/remove as you like
MODELS = [
    "paraphrase-multilingual-MiniLM-L12-v2",   # current default
    "intfloat/multilingual-e5-large",
    "BAAI/bge-m3",
]


def main():
    results = {}
    for model_name in MODELS:
        print(f"\nLoading {model_name} (first run downloads it, please wait)...")
        try:
            model = SentenceTransformer(model_name)
        except Exception as e:
            print(f"  FAILED to load: {e}")
            continue
        prefix = "query: " if "e5" in model_name.lower() else ""
        scores = []
        for label, a, b, should_match in TEST_PAIRS:
            ea = model.encode(prefix + a, normalize_embeddings=True)
            eb = model.encode(prefix + b, normalize_embeddings=True)
            sim = float(util.cos_sim(ea, eb))
            scores.append((label, sim, should_match))
        results[model_name] = scores

    # print a comparison table
    print("\n" + "=" * 100)
    print(f"{'Pair':45s} {'Want':6s} " + "  ".join(f"{m[:18]:18s}" for m in results))
    print("-" * 100)
    for i, (label, _a, _b, should_match) in enumerate(TEST_PAIRS):
        row = f"{label:45s} {'HIGH' if should_match else 'low':6s} "
        for m in results:
            sim = results[m][i][1]
            row += f"  {sim:.3f}             "
        print(row)
    print("=" * 100)
    print("\nA good model: HIGH-want pairs score clearly above ~0.6,")
    print("              low-want pairs stay clearly below ~0.4.")
    print("Pick whichever model has the cleanest separation, then set:")
    print('    set EMBED_MODEL=<that model name>')


if __name__ == "__main__":
    main()
