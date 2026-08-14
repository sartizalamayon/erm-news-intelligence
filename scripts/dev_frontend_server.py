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
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class NoCacheRequestHandler(SimpleHTTPRequestHandler):
    """No-cache headers, plus SPA history-API fallback.

    The fallback is what makes client-side routes real. `frontend/js/
    route.js` gives every screen an address (`/review`, `/sources`,
    `/dash/long`), but those paths are NOT files on disk — a plain
    static server answers 404 for all of them. The result is a router
    that works while you click and breaks the moment anyone reloads,
    bookmarks, or shares a link, which is exactly the case the routing
    exists to serve.

    So: a request that is not a real file, and that looks like a
    navigation (GET, `Accept: text/html`), is served `index.html`
    instead, and the app reads `window.location` to decide what to
    show. Requests for missing ASSETS still 404 honestly — silently
    serving HTML in place of a missing .js is how you get a
    "Unexpected token '<'" console error that tells you nothing.
    """

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _is_navigation_request(self) -> bool:
        return self.command == "GET" and "text/html" in self.headers.get("Accept", "")

    def send_head(self):  # noqa: ANN201 - matches the base class's untyped signature
        path = self.translate_path(self.path)
        missing = not (os.path.isdir(path) or os.path.isfile(path))
        # Only rewrite genuine navigations. An extensioned path is an
        # asset request; letting it fall through to index.html would
        # hide the real 404 behind a confusing parse error.
        if missing and self._is_navigation_request() and not os.path.splitext(self.path)[1]:
            self.path = "/index.html"
        return super().send_head()


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
