"""Tests for the Kite instrument-master download and validation."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scorch.kite.instruments import (
    HEADER,
    INSTRUMENTS_URL,
    MIN_ROWS,
    DownloadError,
    build_manifest,
    csv_bytes,
    download_instruments,
    fetch_bytes,
    parse_csv,
    validate_instruments,
)
from scorch.normalize import build_book, rows_from_kite
from scorch.snapshot import atomic_write

_NUMERIC = {"last_price", "strike", "tick_size", "lot_size"}


def _csv_bytes(header: list[str], rows: int) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for i in range(rows):
        row: list[str] = []
        for column in header:
            if column == "strike":
                row.append("0")
            elif column in _NUMERIC:
                row.append("1.5")
            elif column in {"instrument_token", "exchange_token"}:
                row.append(str(i + 1))
            elif column == "tradingsymbol":
                row.append(f"SYM{i}")
            elif column == "exchange":
                row.append("NSE")
            elif column == "instrument_type":
                row.append("EQ")
            elif column == "segment":
                row.append("NSE")
            elif column == "expiry":
                row.append("")
            elif column == "name":
                row.append(f"Name{i}")
            else:
                row.append(f"v{i}")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def test_min_rows_floor_is_half_a_full_dump() -> None:
    assert MIN_ROWS >= 50_000


def test_parse_csv_counts_rows() -> None:
    payload = _csv_bytes(["a", "b"], 3)
    header, rows = parse_csv(payload)
    assert header == ["a", "b"]
    assert rows == 3


def test_parse_csv_strips_bom() -> None:
    payload = b"\xef\xbb\xbfa,b\n1,2\n"
    header, rows = parse_csv(payload)
    assert header == ["a", "b"]
    assert rows == 1


def test_parse_csv_empty_raises() -> None:
    with pytest.raises(DownloadError, match="empty CSV"):
        parse_csv(b"")


def test_parse_csv_rejects_bad_utf8() -> None:
    with pytest.raises(DownloadError, match="not valid UTF-8"):
        parse_csv(b"\xff\xfe")


def test_parse_csv_rejects_ragged_row() -> None:
    payload = b"a,b\n1,2\n3\n"
    with pytest.raises(DownloadError, match="row 3 has 1 columns"):
        parse_csv(payload)


def test_csv_bytes_decompresses_gzip() -> None:
    raw = b"a,b\n1,2\n"
    assert csv_bytes(gzip.compress(raw)) == raw
    assert csv_bytes(raw) == raw


def test_csv_bytes_rejects_bad_gzip() -> None:
    with pytest.raises(DownloadError, match="not valid gzip"):
        csv_bytes(b"\x1f\x8bnot-gzip")


def test_validate_instruments_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 2)
    payload = _csv_bytes(list(HEADER), 2)
    info, population = validate_instruments(payload)
    assert info["segment"] == "ALL"
    assert info["filename"] == "instruments.csv"
    assert info["url"] == INSTRUMENTS_URL
    assert info["rows"] == 2
    assert info["bytes"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()
    assert info["header"] == list(HEADER)
    assert population == {"segment": "ALL", "zero_lot_rows": 0, "zero_tick_rows": 0}


def test_validate_instruments_header_mismatch() -> None:
    payload = _csv_bytes(["wrong"], 1)
    with pytest.raises(DownloadError, match="header mismatch"):
        validate_instruments(payload)


def test_validate_instruments_too_few_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 4)
    payload = _csv_bytes(list(HEADER), 3)
    with pytest.raises(DownloadError, match="expected at least"):
        validate_instruments(payload)


def test_validate_instruments_duplicate_symbol() -> None:
    payload = _csv_bytes(list(HEADER), 1)
    text = payload.decode()
    doubled = (text + text.splitlines()[1] + "\n").encode()
    with pytest.raises(DownloadError, match="duplicate"):
        validate_instruments(doubled)


def test_validate_instruments_rejects_bad_tick() -> None:
    header = list(HEADER)
    row = [
        "1",
        "2",
        "INFY",
        "Infosys",
        "1",
        "",
        "0",
        "nope",
        "1",
        "EQ",
        "NSE",
        "NSE",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    with pytest.raises(DownloadError, match="tick_size is not numeric"):
        validate_instruments(buf.getvalue().encode())


def test_atomic_write_replaces(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    atomic_write(path, b"one")
    atomic_write(path, b"two")
    assert path.read_bytes() == b"two"
    assert not list(tmp_path.glob("*.tmp"))


def test_build_manifest() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    book = build_book(rows_from_kite(_csv_bytes(list(HEADER), 2)))
    body = json.loads(build_manifest(book, now=now, duration_ms=5))
    assert body["schema_version"] == 3
    assert body["source"] == "kite"
    assert body["downloaded_at"] == now.isoformat()
    assert body["calendar_day"] == "2026-08-30"
    assert body["timezone"] == "Asia/Kolkata"
    assert body["dedup_key"] == ["exchange", "token"]
    assert body["filename"] == "instruments.csv"
    assert body["sha256"] == book["sha256"]
    assert body["population"] == book["population"]
    assert body["baseline"] is None
    assert body["segments"] == book["exchanges"]
    assert body["duration_ms"] == 5


def test_download_instruments_writes_csv_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 2)
    payload = _csv_bytes(list(HEADER), 2)
    monkeypatch.setattr(
        "scorch.kite.instruments.fetch_bytes", lambda url: gzip.compress(payload)
    )
    out = download_instruments(tmp_path)
    assert out == tmp_path
    written = (tmp_path / "instruments.csv").read_bytes()
    assert written.startswith(b"exchange,token,trading_symbol,")
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["source"] == "kite"
    assert manifest["rows"] == 2
    assert manifest["segments"] == [{"segment": "NSE", "rows": 2}]
    assert manifest["sha256"] == hashlib.sha256(written).hexdigest()
    assert manifest["duration_ms"] >= 0
    assert manifest["schema_version"] == 3
    assert manifest["dedup_key"] == ["exchange", "token"]
    assert manifest["baseline"] is None
    assert manifest["population"][0]["zero_tick_rows"] == 0
    assert not (tmp_path.with_name(tmp_path.name + ".bak")).exists()


def test_failed_download_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "instruments.csv").write_bytes(b"old")
    (tmp_path / "manifest.json").write_bytes(b'{"old": true}')
    monkeypatch.setattr(
        "scorch.kite.instruments.fetch_bytes", lambda url: b"not,a,header\n"
    )
    with pytest.raises(DownloadError):
        download_instruments(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old"
    assert (tmp_path / "manifest.json").read_bytes() == b'{"old": true}'


def test_fetch_bytes_sends_version_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"ok"
    seen: dict[str, str] = {}

    class Response:
        status = 200

        def read(self) -> bytes:
            return payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        del timeout
        seen.update({key.lower(): value for key, value in request.header_items()})
        return Response()

    monkeypatch.setattr("scorch.kite.instruments.urllib.request.urlopen", urlopen)
    assert fetch_bytes(INSTRUMENTS_URL) == payload
    assert seen["x-kite-version"] == "3"
    assert "authorization" not in seen


def test_fetch_bytes_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.kite.instruments.RETRY_BACKOFF_SECONDS", 0)
    payload = b"ok"
    attempts = {"n": 0}

    class Response:
        status = 200

        def read(self) -> bytes:
            return payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def urlopen(_request: object, timeout: float) -> Response:
        del timeout
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("slow")
        return Response()

    monkeypatch.setattr("scorch.kite.instruments.urllib.request.urlopen", urlopen)
    assert fetch_bytes(INSTRUMENTS_URL) == payload
    assert attempts["n"] == 3


def test_validate_instruments_allows_zero_tick_and_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 1)
    header = list(HEADER)
    row = [
        "1",
        "2",
        "NIFTY",
        "Nifty 50",
        "0",
        "",
        "0",
        "0",
        "0",
        "EQ",
        "INDICES",
        "NSE",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    _info, population = validate_instruments(buf.getvalue().encode())
    assert population == {"segment": "ALL", "zero_lot_rows": 1, "zero_tick_rows": 1}


def test_validate_instruments_rejects_non_finite_price() -> None:
    header = list(HEADER)
    row = ["1", "2", "INFY", "Infosys", "nan", "", "0", "0.05", "1", "EQ", "NSE", "NSE"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    with pytest.raises(DownloadError, match="last_price is not finite"):
        validate_instruments(buf.getvalue().encode())


def test_row_drop_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 2)
    (tmp_path / "instruments.csv").write_bytes(b"old")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"segments": [{"segment": "ALL", "rows": 100}]})
    )
    payload = _csv_bytes(list(HEADER), 2)
    monkeypatch.setattr("scorch.kite.instruments.fetch_bytes", lambda url: payload)
    with pytest.raises(DownloadError, match="row count changed"):
        download_instruments(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old"


def test_download_records_same_day_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("scorch.kite.instruments.MIN_ROWS", 2)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"segments": [{"segment": "ALL", "rows": 2}]})
    )
    payload = _csv_bytes(list(HEADER), 2)
    monkeypatch.setattr("scorch.kite.instruments.fetch_bytes", lambda url: payload)
    download_instruments(tmp_path)
    body = json.loads((tmp_path / "manifest.json").read_text())
    assert body["baseline"]["source"] == "same_directory"
    assert body["baseline"]["segments"] == [
        {"segment": "ALL", "previous_rows": 2, "rows": 2, "relative_change": 0}
    ]


def test_fetch_bytes_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.kite.instruments.RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(
        "scorch.kite.instruments.urllib.request.urlopen",
        MagicMock(side_effect=TimeoutError("slow")),
    )
    with pytest.raises(DownloadError, match="failed to GET"):
        fetch_bytes(INSTRUMENTS_URL)
