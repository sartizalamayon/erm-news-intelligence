"""Not a Python package in the app-import sense — this `__init__.py`
exists only so `python -m migrations.legacy.import_legacy_sqlite` (a
one-off script, see that module) can be invoked as a module. The actual
schema lives in this directory's `*.sql` files, applied in filename
order by `app.db.migrate.run_migrations`; nothing here is imported by
`app/`.
"""
