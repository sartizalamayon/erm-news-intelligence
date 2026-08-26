"""The export download streams; it does not assemble the file in memory.

WHY THIS IS PINNED. `fetch_pool` runs `.fetchall()` with no LIMIT for
every filter except `topn`, and the buffered writers then held the rows
as dicts, the whole document, AND its encoded bytes at the same time.
Measured on the 79,892-article dev corpus against a Render FREE
instance's 512 MB, from a ~53 MB baseline:

    CSV   1 day       2,561 rows   0.09s    70 MB
    CSV   1 month    20,392 rows   1.13s   187 MB
    CSV   1 year     27,311 rows   1.07s   270 MB
    XLSX  12 years   27,422 rows   3.68s   199 MB

One export peaked at 270 MB. Two at once, or one alongside the nightly
batch, is an OOM -- and the archive grows by 2,000 to 8,000 articles a
day, so the number only climbs. After the change, on the same corpus:

    CSV   1 month     1.10s    91 MB
    CSV   12 years    1.21s    99 MB
    XLSX  1 month     2.29s   108 MB
    XLSX  12 years    2.95s   108 MB

The headline is not that the peak halved. It is that 1 month and 12
years now cost the SAME, because peak is set by the cursor batch size
rather than by how much the analyst asked for.

These tests assert the mechanism rather than a megabyte figure: RSS is
too dependent on interpreter state, GC timing and what else the suite
has already run to be a stable assertion, but "reads through a
server-side cursor" and "hands back more than one chunk" are exact, and
they are the two properties that would have to be given up to
reintroduce the bug.
"""

from __future__ import annotations

import inspect

import pytest

from app.db.testing import migrated_schema, pg_available
from app.services import export as export_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _seed(conn, n: int) -> None:
    """Bangla titles on purpose: this product is bilingual, and a
    multi-byte title is what makes a chunk boundary interesting -- a
    naive byte-slicing writer would split a character in half.
    """
    for i in range(n):
        conn.execute(
            "INSERT INTO articles (url, url_norm, source, title, title_en, day, sections, "
            "keywords, review_status, llm_score, is_primary) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)",
            (
                f"https://example.test/{i}", "", "Test Source",
                f"শিরোনাম {i}", f"Headline {i}", "2026-08-22",
                "Macroeconomy", "inflation", "approved", 7.0,
            ),
        )
    conn.commit()


def test_the_download_route_returns_a_streaming_response_not_a_buffered_body() -> None:
    """A `Response(content=...)` requires the whole document up front.
    That is the shape this must never go back to.
    """
    from app.api.routers.export import do_export

    # A string, not the class: app/api/routers/export.py carries
    # `from __future__ import annotations`, so every annotation in it is
    # lazy. Comparing against the imported class would silently never
    # match and this test would pass on a buffered handler too.
    assert inspect.signature(do_export).return_annotation == "StreamingResponse"


def test_row_reader_uses_a_server_side_cursor() -> None:
    """`conn.cursor(name=...)` is what keeps the result set on the server.

    Without a name psycopg materialises every row client-side on
    execute, which is precisely `fetchall()` with extra steps -- the
    generator would still yield one row at a time while the whole result
    already sat in memory, and the fix would be cosmetic.
    """
    src = inspect.getsource(export_service.iter_export_rows)
    assert "conn.cursor(name=" in src, "iter_export_rows must open a NAMED (server-side) cursor"
    assert "itersize" in src, "a server-side cursor still needs its batch size set"


def test_a_large_csv_is_handed_back_in_more_than_one_chunk() -> None:
    """The observable consequence for CSV: bytes arrive progressively.

    A buffered writer can only ever produce one chunk, so this fails on
    any return to `getvalue()`-and-hand-it-over.
    """
    with migrated_schema() as conn:
        _seed(conn, 1200)  # comfortably over _CSV_CHUNK_ROWS
        chunks = list(
            export_service.stream_csv(
                lambda: __import__("contextlib").nullcontext(conn),
                filter="approved", top_n=0, scope="range",
                date_from="2026-08-22", date_to="2026-08-22",
                sections=[], sort="relevance",
            )
        )
    assert len(chunks) > 1, "the CSV came back as a single chunk, i.e. it was buffered"
    assert all(isinstance(c, bytes) for c in chunks)
    # Bangla survives the chunk boundaries: a writer that sliced bytes
    # rather than encoding whole rows would split a multi-byte character
    # and the join below would not decode.
    b"".join(chunks).decode("utf-8-sig")


def test_xlsx_streams_by_mechanism_since_a_zip_cannot_stream_row_by_row() -> None:
    """XLSX gets a different assertion, honestly, because it has a
    different constraint.

    An xlsx is a ZIP and a ZIP's central directory is written last, so
    the file genuinely cannot be emitted row-by-row down the wire -- and
    a small one legitimately arrives as a single chunk. Asserting a
    chunk COUNT here would therefore be asserting the test's seed size,
    not the product's behaviour.

    What must hold is that no point in the process has every row live at
    once: write-only mode keeps one row, and the finished workbook goes
    to a spooled file that rolls over to disk rather than sitting in RSS
    on a 512 MB box.
    """
    src = inspect.getsource(export_service.stream_xlsx)
    assert "write_only=True" in src, "a normal Workbook holds every cell object in memory"
    assert "SpooledTemporaryFile" in src, "the finished workbook must be able to roll over to disk"
    assert "_XLSX_CHUNK_BYTES" in src, "it must be read back in fixed-size pieces, not all at once"

    with migrated_schema() as conn:
        _seed(conn, 1200)
        chunks = list(
            export_service.stream_xlsx(
                lambda: __import__("contextlib").nullcontext(conn),
                filter="approved", top_n=0, scope="range",
                date_from="2026-08-22", date_to="2026-08-22",
                sections=[], sort="relevance",
            )
        )
    body = b"".join(chunks)
    assert body.startswith(b"PK"), "still a real xlsx"
    assert all(len(c) <= export_service._XLSX_CHUNK_BYTES for c in chunks), "a chunk exceeded the read size"


def test_the_byte_order_mark_is_written_once_not_per_chunk() -> None:
    """`utf-8-sig` on every chunk would scatter U+FEFF through the file
    and break the first column of every batch after the first.
    """
    with migrated_schema() as conn:
        _seed(conn, 1200)
        chunks = list(
            export_service.stream_csv(
                lambda: __import__("contextlib").nullcontext(conn),
                filter="approved", top_n=0, scope="range",
                date_from="2026-08-22", date_to="2026-08-22",
                sections=[], sort="relevance",
            )
        )
    body = b"".join(chunks)
    assert body.startswith(b"\xef\xbb\xbf"), "the BOM must lead the file"
    assert body.count(b"\xef\xbb\xbf") == 1, "the BOM must appear exactly once"


def test_streaming_and_buffered_readers_cannot_drift_apart() -> None:
    """Both go through `_pool_sql`. A filter or ordering fixed in one and
    not the other would mean a download whose contents disagree with the
    row count the Export screen previewed for it.
    """
    assert "_pool_sql(" in inspect.getsource(export_service.fetch_pool)
    assert "_pool_sql(" in inspect.getsource(export_service.iter_export_rows)
