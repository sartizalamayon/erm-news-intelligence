"""Source-plugin layer — the fetch-layer architecture described in
docs/speed-techniques.md §4 point 1.

This package is a HOME for that architecture, not an implementation of
it (Wave 0 / ARCH-REPO builds skeleton only; P1-FETCH implements
fetching). Concrete sources will live one-per-file here as
`app/sources/<site>.py`, each implementing the `NewsSource` ABC in
`app/sources/base.py` and registering itself via `app/sources/registry.py`.

Nothing in this package should perform network I/O yet.
"""
