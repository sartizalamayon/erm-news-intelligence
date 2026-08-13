"""Local dev static file server for frontend/, with cache revalidation forced.

Why this exists instead of bare `python -m http.server`: http.server sends
a `Last-Modified` header but no `Cache-Control` of its own choosing, so
browsers fall back to heuristic caching and can keep serving stale JS/CSS
for a long time after a file changes on disk. A plain reload does not
reliably pick up the new bytes — only an explicit cache-bypass does. That
already cost two debugging sessions chasing a "bug" that was really just
stale cached JS (see README-rebuild.md).

This subclasses SimpleHTTPRequestHandler and injects
`Cache-Control: no-cache` on every response before headers are sent. That
forces revalidation on every load (browser always asks "is this still
current?") without disabling the conditional-GET / 304 machinery that
`Last-Modified`/`If-Modified-Since` already gives us for free — so it stays
cheap on the wire, it just never trusts a cached copy blindly.

Usage matches `python -m http.server`:
    .venv/bin/python scripts/dev_frontend_server.py 5173 --directory frontend
"""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int, nargs="?", default=8000)
    parser.add_argument("--directory", "-d", default=".")
    parser.add_argument("--bind", "-b", default="")
    args = parser.parse_args()

    handler = lambda *a, **kw: NoCacheRequestHandler(*a, directory=args.directory, **kw)  # noqa: E731
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Serving {args.directory!r} on port {args.port} (Cache-Control: no-cache on every response) ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
