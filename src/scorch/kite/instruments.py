"""Download the Kite Connect instrument master.

Public CSV. No API key or access token: GET /instruments returns the dump
with or without an Authorization header. The file is generated once a day,
so last_price is not a live quote.
Docs: https://kite.trade/docs/connect/v3/market-quotes/

One file covers every exchange (NSE, BSE, NFO, BFO, MCX, CDS, NCO, and
smaller lists such as GLOBAL and NSEIX). Currency rows on CDS and BCD
are left out of the book, because their prices are finer than one paisa.
Index rows use segment INDICES inside this file; there is no separate
indices download. The published snapshot is the normalized instruments.csv.
last_price is checked on the vendor row and is not written. Kite's own
unique key is (exchange, tradingsymbol), not instrument_token.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from scorch import __version__
from scorch.normalize import build_book, render_manifest, rows_from_kite
from scorch.numbers import parse_finite
from scorch.snapshot import (
    DownloadError,
    continuity_check,
    continuity_rows,
    describe_baseline,
    exchange_day,
    load_baseline,
    log_continuity,
    publish_snapshot,
)

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
USER_AGENT = f"scorch-kite-instrument-downloader/{__version__}"
TIMEOUT_SECONDS = 60
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Sanity floor near half of the 2026-09-28 dump (~110,000 rows).
MIN_ROWS = 50_000
NUMERIC_COLUMNS = ("last_price", "strike", "tick_size", "lot_size")

logger = logging.getLogger(__name__)

# Expected header is the contract with Kite. Fail the run if it drifts.
HEADER: tuple[str, ...] = (
    "instrument_token",
    "exchange_token",
    "tradingsymbol",
    "name",
    "last_price",
    "expiry",
    "strike",
    "tick_size",
    "lot_size",
    "instrument_type",
    "segment",
    "exchange",
)


class InstrumentInfo(TypedDict):
    segment: str
    url: str
    filename: str
    bytes: int
    rows: int
    sha256: str
    header: list[str]
    duration_ms: NotRequired[int]


class PopulationInfo(TypedDict):
    segment: str
    zero_lot_rows: int
    zero_tick_rows: int


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv,*/*",
            "X-Kite-Version": "3",
        },
        method="GET",
    )
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise DownloadError(f"{url} returned HTTP {response.status}")
                return response.read()
        except (urllib.error.URLError, TimeoutError, DownloadError) as error:
            last_error = error
            if attempt == RETRIES:
                break
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise DownloadError(f"failed to GET {url}: {last_error}") from last_error


def csv_bytes(payload: bytes) -> bytes:
    """Return CSV bytes, decompressing a gzip body when Kite sends one."""
    if payload.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(payload)
        except gzip.BadGzipFile as error:
            raise DownloadError("instruments response is not valid gzip") from error
    return payload


def parse_csv(payload: bytes) -> tuple[list[str], int]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DownloadError("instruments CSV is not valid UTF-8") from error
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as error:
        raise DownloadError("empty CSV") from error
    rows = 0
    for line_no, row in enumerate(reader, start=2):
        if len(row) != len(header):
            raise DownloadError(
                f"instruments row {line_no} has {len(row)} columns, "
                f"expected {len(header)}"
            )
        rows += 1
    return header, rows


def _validate_rows(payload: bytes) -> PopulationInfo:
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    tokens: set[str] = set()
    symbols: set[tuple[str, str]] = set()
    zero_lot = 0
    zero_tick = 0
    for line_no, row in enumerate(reader, start=2):
        token = row["instrument_token"].strip()
        exchange_token = row["exchange_token"].strip()
        tradingsymbol = row["tradingsymbol"].strip()
        exchange = row["exchange"].strip()
        if token == "" or exchange_token == "" or tradingsymbol == "" or exchange == "":
            raise DownloadError(f"instruments row {line_no} is missing an identifier")
        if not token.isdigit() or not exchange_token.isdigit():
            raise DownloadError(f"instruments row {line_no} has a non-numeric token")
        if token in tokens:
            raise DownloadError(f"instruments duplicate instrument_token {token}")
        tokens.add(token)
        key = (exchange, tradingsymbol)
        if key in symbols:
            raise DownloadError(
                "instruments duplicate exchange,tradingsymbol "
                f"{exchange},{tradingsymbol}"
            )
        symbols.add(key)
        for column in NUMERIC_COLUMNS:
            value = row[column].strip()
            if value == "":
                raise DownloadError(f"instruments row {line_no} {column} is empty")
            number = parse_finite(value, f"instruments row {line_no} {column}")
            if column == "lot_size" and number == 0:
                zero_lot += 1
            elif column == "tick_size" and number == 0:
                zero_tick += 1
    return {
        "segment": "ALL",
        "zero_lot_rows": zero_lot,
        "zero_tick_rows": zero_tick,
    }


def validate_instruments(payload: bytes) -> tuple[InstrumentInfo, PopulationInfo]:
    expected = list(HEADER)
    header, rows = parse_csv(payload)
    if header != expected:
        raise DownloadError(
            f"instruments header mismatch\n  expected: {expected}\n  got:      {header}"
        )
    population = _validate_rows(payload)
    if rows < MIN_ROWS:
        raise DownloadError(
            f"instruments has {rows} rows, expected at least {MIN_ROWS}"
        )
    info: InstrumentInfo = {
        "segment": "ALL",
        "url": INSTRUMENTS_URL,
        "filename": "instruments.csv",
        "bytes": len(payload),
        "rows": rows,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "header": header,
    }
    return info, population


def default_out_dir(now: datetime | None = None) -> Path:
    return Path("data/kite") / exchange_day(now)


def build_manifest(
    book: dict[str, Any],
    now: datetime | None = None,
    *,
    baseline: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> bytes:
    return render_manifest(
        source="kite",
        book=book,
        now=now,
        baseline=baseline,
        duration_ms=duration_ms,
    )


def download_instruments(
    out_dir: Path | None = None, now: datetime | None = None
) -> Path:
    """Download the Kite instrument dump and publish the normalized book."""
    target = out_dir if out_dir is not None else default_out_dir(now)
    logger.info("GET %s", INSTRUMENTS_URL)
    started = time.perf_counter()
    stored = csv_bytes(fetch_bytes(INSTRUMENTS_URL))
    validate_instruments(stored)
    book = build_book(rows_from_kite(stored))
    duration_ms = round((time.perf_counter() - started) * 1000)
    logger.info(
        "instruments.csv: %s rows, %s bytes, %sms",
        book["rows"],
        len(book["csv"]),
        duration_ms,
    )
    published = {"instruments.csv": book["csv"]}

    def before_swap() -> None:
        source, baseline = load_baseline(target)
        exchange_rows = {item["segment"]: item["rows"] for item in book["exchanges"]}
        compared, current_rows = continuity_rows(baseline, exchange_rows, book["rows"])
        segment_deltas = continuity_check(compared, current_rows)
        log_continuity(logger, source, segment_deltas, [])
        published["manifest.json"] = build_manifest(
            book,
            now=now,
            duration_ms=duration_ms,
            baseline=describe_baseline(source, target, segment_deltas, []),
        )

    publish_snapshot(target, published, before_swap=before_swap)
    return target
