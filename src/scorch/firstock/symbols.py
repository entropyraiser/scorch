"""Download Firstock V1 symbol masters for every documented segment.

Public, unauthenticated GET. HEAD on these URLs returns 404 — always GET.
Docs: https://firstock.in/api/docs/downloaders/

The Indices file is index F&O only (FUTIDX/OPTIDX) and overlaps NFO+BFO.
Spot indices (NIFTY, BANKNIFTY, SENSEX, ...) live in the NSE/BSE cash files.
All five files are downloaded and validated. Indices rows must also appear in
NFO or in BFO, and not in both. They are not written: the published file is
the normalized instruments.csv, which already contains those contracts.
"""

from __future__ import annotations

import csv
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
from scorch.normalize import build_book, render_manifest, rows_from_firstock
from scorch.numbers import parse_finite
from scorch.snapshot import (
    DownloadError,
    continuity_check,
    continuity_rows,
    describe_baseline,
    exchange_day,
    load_baseline,
    log_continuity,
    overlap_continuity,
    publish_snapshot,
)

BASE_URL = "https://api.firstock.in/V1/symbols"
USER_AGENT = f"scorch-firstock-symbol-downloader/{__version__}"
TIMEOUT_SECONDS = 60
RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
NUMERIC_COLUMNS = ("LotSize", "TickSize", "FreezeQty", "StrikePrice")

logger = logging.getLogger(__name__)


class SegmentSpec(TypedDict):
    header: tuple[str, ...]
    min_rows: int


class SegmentInfo(TypedDict):
    segment: str
    url: str
    filename: str
    bytes: int
    rows: int
    sha256: str
    header: list[str]
    duration_ms: NotRequired[int]


class OverlapInfo(TypedDict):
    segment: str
    also_in: str
    rows: int


class PopulationInfo(TypedDict):
    segment: str
    zero_lot_rows: int
    zero_tick_rows: int


class IndicesCoverage(TypedDict):
    rows: int
    in_nfo: int
    in_bfo: int
    in_both: int
    uncovered: int


# Expected header is the contract with Firstock. Fail the run if it drifts.
# min_rows is a sanity floor near half the 2026-08-30 snapshot, so a short
# file fails without treating that day's count as a permanent size.
SEGMENTS: dict[str, SegmentSpec] = {
    "NSE": {
        "header": (
            "Exchange",
            "Token",
            "LotSize",
            "TradingSymbol",
            "CompanyName",
            "ISIN",
            "TickSize",
            "FreezeQty",
        ),
        "min_rows": 5000,
    },
    "BSE": {
        "header": (
            "Exchange",
            "Token",
            "LotSize",
            "TradingSymbol",
            "CompanyName",
            "ISIN",
            "TickSize",
            "FreezeQty",
        ),
        "min_rows": 6000,
    },
    "NFO": {
        "header": (
            "Exchange",
            "Token",
            "LotSize",
            "Symbol",
            "TradingSymbol",
            "CompanyName",
            "Expiry",
            "Instrument",
            "OptionType",
            "StrikePrice",
            "TickSize",
            "FreezeQty",
        ),
        "min_rows": 30000,
    },
    "BFO": {
        "header": (
            "Exchange",
            "Token",
            "LotSize",
            "Symbol",
            "TradingSymbol",
            "CompanyName",
            "Expiry",
            "Instrument",
            "OptionType",
            "StrikePrice",
            "TickSize",
            "FreezeQty",
        ),
        "min_rows": 15000,
    },
    "Indices": {
        "header": (
            "Exchange",
            "Token",
            "LotSize",
            "Symbol",
            "TradingSymbol",
            "Expiry",
            "Instrument",
            "OptionType",
            "StrikePrice",
            "TickSize",
        ),
        "min_rows": 8000,
    },
}


def segment_url(name: str) -> str:
    return f"{BASE_URL}/{name}?ref=firstock.in"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
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


def parse_csv(payload: bytes) -> tuple[list[str], int]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DownloadError("CSV is not valid UTF-8") from error
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as error:
        raise DownloadError("empty CSV") from error
    rows = 0
    for line_no, row in enumerate(reader, start=2):
        if len(row) != len(header):
            raise DownloadError(
                f"row {line_no} has {len(row)} columns, expected {len(header)}"
            )
        rows += 1
    return header, rows


def _validate_rows(
    name: str, payload: bytes, header: list[str]
) -> tuple[set[tuple[str, str]], PopulationInfo]:
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    numeric = [column for column in NUMERIC_COLUMNS if column in header]
    keys: set[tuple[str, str]] = set()
    zero_lot = 0
    zero_tick = 0
    for line_no, row in enumerate(reader, start=2):
        exchange = row["Exchange"].strip()
        token = row["Token"].strip()
        if exchange == "" or token == "":
            raise DownloadError(f"{name} row {line_no} is missing Exchange or Token")
        key = (exchange, token)
        if key in keys:
            raise DownloadError(f"{name} duplicate Exchange,Token {exchange},{token}")
        keys.add(key)
        for column in numeric:
            value = row[column].strip()
            if value == "":
                raise DownloadError(f"{name} row {line_no} {column} is empty")
            number = parse_finite(value, f"{name} row {line_no} {column}")
            if column == "LotSize" and number == 0:
                zero_lot += 1
            elif column == "TickSize" and number == 0:
                zero_tick += 1
    population: PopulationInfo = {
        "segment": name,
        "zero_lot_rows": zero_lot,
        "zero_tick_rows": zero_tick,
    }
    return keys, population


def validate_segment(
    name: str, payload: bytes
) -> tuple[SegmentInfo, set[tuple[str, str]], PopulationInfo]:
    spec = SEGMENTS[name]
    expected = list(spec["header"])
    header, rows = parse_csv(payload)
    if header != expected:
        raise DownloadError(
            f"{name} header mismatch\n  expected: {expected}\n  got:      {header}"
        )
    keys, population = _validate_rows(name, payload, header)
    min_rows = spec["min_rows"]
    if rows < min_rows:
        raise DownloadError(f"{name} has {rows} rows, expected at least {min_rows}")
    info: SegmentInfo = {
        "segment": name,
        "url": segment_url(name),
        "filename": f"{name}_symbols.csv",
        "bytes": len(payload),
        "rows": rows,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "header": header,
    }
    return info, keys, population


def overlap_counts(keys: dict[str, set[tuple[str, str]]]) -> list[OverlapInfo]:
    """How many Indices (Exchange, Token) keys also sit in NFO and in BFO."""
    indices = keys.get("Indices", set())
    return [
        {
            "segment": "Indices",
            "also_in": other,
            "rows": len(indices & keys.get(other, set())),
        }
        for other in ("NFO", "BFO")
    ]


def indices_coverage(keys: dict[str, set[tuple[str, str]]]) -> IndicesCoverage:
    """How Indices keys sit inside NFO and BFO.

    A key in both files would make dedup ambiguous. A key in neither is not
    the index-derivative file this downloader is written against.
    """
    indices = keys.get("Indices", set())
    nfo = keys.get("NFO", set())
    bfo = keys.get("BFO", set())
    return {
        "rows": len(indices),
        "in_nfo": len(indices & nfo),
        "in_bfo": len(indices & bfo),
        "in_both": len(indices & nfo & bfo),
        "uncovered": len(indices - (nfo | bfo)),
    }


def require_indices_coverage(coverage: IndicesCoverage) -> None:
    if coverage["uncovered"]:
        raise DownloadError(
            f"Indices has {coverage['uncovered']} (Exchange, Token) keys "
            "missing from NFO and BFO"
        )
    if coverage["in_both"]:
        raise DownloadError(
            f"Indices has {coverage['in_both']} (Exchange, Token) keys "
            "in both NFO and BFO"
        )


def default_out_dir(now: datetime | None = None) -> Path:
    return Path("data/firstock") / exchange_day(now)


def build_manifest(
    book: dict[str, Any],
    overlaps: list[OverlapInfo],
    coverage: IndicesCoverage,
    now: datetime | None = None,
    *,
    baseline: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> bytes:
    return render_manifest(
        source="firstock",
        book=book,
        now=now,
        baseline=baseline,
        duration_ms=duration_ms,
        overlaps=list(overlaps),
        indices_coverage=dict(coverage),
    )


def download_all(out_dir: Path, now: datetime | None = None) -> list[SegmentInfo]:
    """Fetch and validate every segment, then publish the normalized book."""
    payloads: dict[str, bytes] = {}
    manifest: list[SegmentInfo] = []
    keys: dict[str, set[tuple[str, str]]] = {}
    started_all = time.perf_counter()
    for name in SEGMENTS:
        url = segment_url(name)
        logger.info("GET %s", url)
        started = time.perf_counter()
        payload = fetch_bytes(url)
        info, segment_keys, population = validate_segment(name, payload)
        info["duration_ms"] = round((time.perf_counter() - started) * 1000)
        logger.info(
            "%s: %s rows, %s bytes, %sms, %s zero-lot, %s zero-tick",
            info["filename"],
            info["rows"],
            info["bytes"],
            info["duration_ms"],
            population["zero_lot_rows"],
            population["zero_tick_rows"],
        )
        payloads[name] = payload
        manifest.append(info)
        keys[name] = segment_keys
    overlaps = overlap_counts(keys)
    coverage = indices_coverage(keys)
    require_indices_coverage(coverage)
    logger.info(
        "Indices coverage: %s/%s in NFO, %s/%s in BFO",
        coverage["in_nfo"],
        coverage["rows"],
        coverage["in_bfo"],
        coverage["rows"],
    )
    normalized: list[dict[str, str]] = []
    for name, payload in payloads.items():
        normalized.extend(rows_from_firstock(name, payload))
    book = build_book(normalized)
    duration_ms = round((time.perf_counter() - started_all) * 1000)
    logger.info(
        "instruments.csv: %s rows, %s bytes, %sms",
        book["rows"],
        len(book["csv"]),
        duration_ms,
    )
    published = {"instruments.csv": book["csv"]}

    def before_swap() -> None:
        source, baseline = load_baseline(out_dir)
        exchange_rows = {item["segment"]: item["rows"] for item in book["exchanges"]}
        compared, current_rows = continuity_rows(baseline, exchange_rows, book["rows"])
        segment_deltas = continuity_check(compared, current_rows)
        overlap_deltas = overlap_continuity(baseline, overlaps)
        log_continuity(logger, source, segment_deltas, overlap_deltas)
        published["manifest.json"] = build_manifest(
            book,
            overlaps,
            coverage,
            now=now,
            duration_ms=duration_ms,
            baseline=describe_baseline(source, out_dir, segment_deltas, overlap_deltas),
        )

    publish_snapshot(out_dir, published, before_swap=before_swap)
    return manifest


def download_symbols(out_dir: Path | None = None, now: datetime | None = None) -> Path:
    """Download all symbol segments and write a dated manifest."""
    target = out_dir if out_dir is not None else default_out_dir(now)
    download_all(target, now=now)
    return target
