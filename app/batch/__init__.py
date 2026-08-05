"""Daily batch job package — the one producer in the "one producer, one
consumer, once a day" design ruled at GATE 0 (no Celery, no Redis: "one
producer, one consumer, once daily. No back-pressure to arbitrate.").

Runnable independently of the FastAPI app:

    python -m app.batch

This is exactly what the GitHub Actions cron workflow invokes. Nothing
in this package may import app.main or anything under app.api — the
batch job must work if the web app were deleted entirely.
"""
