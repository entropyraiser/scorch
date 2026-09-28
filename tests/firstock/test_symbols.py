"""Tests for Firstock symbol-master download and validation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scorch.firstock.symbols import (
    BASE_URL,
    SEGMENTS,
    DownloadError,
    build_manifest,
    download_all,
    download_symbols,
    fetch_bytes,
    indices_coverage,
    overlap_counts,
    parse_csv,
    require_indices_coverage,
    segment_url,
    validate_segment,
)
from scorch.normalize import build_book, rows_from_firstock
from scorch.snapshot import atomic_write

_NUMERIC = {"LotSize", "TickSize", "FreezeQty", "StrikePrice"}


def _csv_bytes(
    header: list[str],
    rows: int,
    *,
    exchange: str = "NSE",
    exchange_at=None,
    token_at=None,
) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for i in range(rows):
        row: list[str] = []
        for column in header:
            if column in _NUMERIC:
                row.append("1.5")
            elif column == "Token":
                row.append(token_at(i) if token_at else str(i + 1))
            elif column == "Exchange":
                row.append(exchange_at(i) if exchange_at else exchange)
            elif column == "TradingSymbol":
                row.append(f"SYM{i}")
            elif column == "CompanyName":
                row.append(f"Name{i}")
            elif column == "ISIN":
                row.append(f"INE{i:09d}")
            elif column == "Symbol":
                row.append(f"UND{i}")
            elif column == "Expiry":
                row.append("29-SEP-2026")
            elif column == "Instrument":
                row.append("OPTSTK")
            elif column == "OptionType":
                row.append("CE")
            else:
                row.append(f"v{i}")
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def _segment_payloads() -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for name, spec in SEGMENTS.items():
        header = list(spec["header"])
        count = spec["min_rows"]
        if name == "Indices":
            half = count // 2

            def exchange_at(i: int, half: int = half) -> str:
                return "NFO" if i < half else "BFO"

            def token_at(i: int, half: int = half) -> str:
                if i < half:
                    return str(i + 1)
                return str(i - half + 1)

            payloads[name] = _csv_bytes(
                header, count, exchange_at=exchange_at, token_at=token_at
            )
        else:
            payloads[name] = _csv_bytes(header, count, exchange=name)
    return payloads


def test_segment_url() -> None:
    assert segment_url("NSE") == f"{BASE_URL}/NSE?ref=firstock.in"


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
    with pytest.raises(DownloadError, match="row 3 has 1 columns"):
        parse_csv(b"a,b\n1,2\n3\n")


def test_validate_segment_success() -> None:
    header = list(SEGMENTS["Indices"]["header"])
    payload = _csv_bytes(header, SEGMENTS["Indices"]["min_rows"], exchange="NFO")
    info, keys, population = validate_segment("Indices", payload)
    assert info["segment"] == "Indices"
    assert info["filename"] == "Indices_symbols.csv"
    assert info["rows"] == SEGMENTS["Indices"]["min_rows"]
    assert info["bytes"] == len(payload)
    assert info["sha256"] == hashlib.sha256(payload).hexdigest()
    assert info["header"] == header
    assert info["url"] == segment_url("Indices")
    assert ("NFO", "1") in keys
    assert population == {
        "segment": "Indices",
        "zero_lot_rows": 0,
        "zero_tick_rows": 0,
    }


def test_validate_segment_header_mismatch() -> None:
    payload = _csv_bytes(["wrong"], 200)
    with pytest.raises(DownloadError, match="header mismatch"):
        validate_segment("Indices", payload)


def test_validate_segment_too_few_rows() -> None:
    header = list(SEGMENTS["Indices"]["header"])
    payload = _csv_bytes(header, SEGMENTS["Indices"]["min_rows"] - 1, exchange="NFO")
    with pytest.raises(DownloadError, match="expected at least"):
        validate_segment("Indices", payload)


def test_validate_segment_duplicate_token() -> None:
    header = list(SEGMENTS["NSE"]["header"])
    payload = _csv_bytes(header, 2)
    text = payload.decode()
    first_row = text.splitlines()[1]
    doubled = (text + first_row + "\n").encode()
    with pytest.raises(DownloadError, match="duplicate Exchange,Token"):
        validate_segment("NSE", doubled)


def test_validate_segment_rejects_non_numeric_tick() -> None:
    header = list(SEGMENTS["NSE"]["header"])
    row = ["NSE", "1", "1", "SYM", "Name", "INE", "nope", "1"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    with pytest.raises(DownloadError, match="TickSize is not numeric"):
        validate_segment("NSE", buf.getvalue().encode())


def test_overlap_counts_indices_against_nfo_and_bfo() -> None:
    overlaps = overlap_counts(
        {
            "Indices": {("NFO", "1"), ("BFO", "2"), ("NFO", "3")},
            "NFO": {("NFO", "1"), ("NFO", "9")},
            "BFO": {("BFO", "2")},
        }
    )
    assert overlaps == [
        {"segment": "Indices", "also_in": "NFO", "rows": 1},
        {"segment": "Indices", "also_in": "BFO", "rows": 1},
    ]


def test_atomic_write_replaces(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    atomic_write(path, b"one")
    atomic_write(path, b"two")
    assert path.read_bytes() == b"two"
    assert not list(tmp_path.glob("*.tmp"))


def test_build_manifest() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    header = list(SEGMENTS["NSE"]["header"])
    payload = _csv_bytes(header, 1, exchange="NSE")
    book = build_book(rows_from_firstock("NSE", payload))
    overlaps = [{"segment": "Indices", "also_in": "NFO", "rows": 4}]
    coverage = {
        "rows": 1,
        "in_nfo": 1,
        "in_bfo": 0,
        "in_both": 0,
        "uncovered": 0,
    }
    body = json.loads(build_manifest(book, overlaps, coverage, now=now, duration_ms=5))
    assert body["schema_version"] == 3
    assert body["source"] == "firstock"
    assert body["downloaded_at"] == now.isoformat()
    assert body["calendar_day"] == "2026-08-30"
    assert body["timezone"] == "Asia/Kolkata"
    assert body["dedup_key"] == ["exchange", "token"]
    assert body["filename"] == "instruments.csv"
    assert body["sha256"] == book["sha256"]
    assert body["header"][0] == "exchange"
    assert body["overlaps"] == overlaps
    assert body["population"] == book["population"]
    assert body["indices_coverage"] == coverage
    assert body["baseline"] is None
    assert body["segments"] == book["exchanges"]
    assert body["duration_ms"] == 5


def test_download_all_writes_validated_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payloads = _segment_payloads()

    def fake_fetch(url: str) -> bytes:
        name = url.split("/")[-1].split("?")[0]
        return payloads[name]

    monkeypatch.setattr("scorch.firstock.symbols.fetch_bytes", fake_fetch)
    with caplog.at_level(logging.INFO):
        manifest = download_all(tmp_path)
    assert [item["segment"] for item in manifest] == list(SEGMENTS)
    assert "GET" in caplog.text
    body = json.loads((tmp_path / "manifest.json").read_text())
    assert body["schema_version"] == 3
    assert body["baseline"] is None
    assert body["dedup_key"] == ["exchange", "token"]
    assert body["filename"] == "instruments.csv"
    written = (tmp_path / "instruments.csv").read_bytes()
    assert body["sha256"] == hashlib.sha256(written).hexdigest()
    assert written.startswith(b"exchange,token,trading_symbol,")
    expected_rows = sum(
        spec["min_rows"] for name, spec in SEGMENTS.items() if name != "Indices"
    )
    assert body["rows"] == expected_rows
    assert b"\n" in written
    for name in SEGMENTS:
        assert not (tmp_path / f"{name}_symbols.csv").exists()
    assert {item["also_in"] for item in body["overlaps"]} == {"NFO", "BFO"}
    coverage = body["indices_coverage"]
    assert coverage["uncovered"] == 0
    assert coverage["in_both"] == 0
    assert coverage["in_nfo"] + coverage["in_bfo"] == coverage["rows"]
    assert "Indices coverage:" in caplog.text
    assert "no previous snapshot to compare" in caplog.text
    for item in manifest:
        assert item["duration_ms"] >= 0
    assert not (tmp_path.with_name(tmp_path.name + ".partial")).exists()
    assert not (tmp_path.with_name(tmp_path.name + ".bak")).exists()


def test_failed_segment_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "instruments.csv").write_bytes(b"old-book")
    (tmp_path / "manifest.json").write_text('{"old": true}')
    payloads = _segment_payloads()

    def fake_fetch(url: str) -> bytes:
        name = url.split("/")[-1].split("?")[0]
        if name == "BSE":
            raise DownloadError("nope")
        return payloads[name]

    monkeypatch.setattr("scorch.firstock.symbols.fetch_bytes", fake_fetch)
    with pytest.raises(DownloadError, match="nope"):
        download_symbols(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old-book"
    assert json.loads((tmp_path / "manifest.json").read_text()) == {"old": True}
    assert not (tmp_path / "BSE_symbols.csv").exists()
    assert not (tmp_path.with_name(tmp_path.name + ".partial")).exists()


def test_fetch_bytes_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.firstock.symbols.RETRY_BACKOFF_SECONDS", 0)
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

    monkeypatch.setattr("scorch.firstock.symbols.urllib.request.urlopen", urlopen)
    assert fetch_bytes("https://example.test") == payload
    assert attempts["n"] == 3


def test_fetch_bytes_exhausts_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scorch.firstock.symbols.RETRY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(
        "scorch.firstock.symbols.urllib.request.urlopen",
        MagicMock(side_effect=TimeoutError("slow")),
    )
    with pytest.raises(DownloadError, match="failed to GET"):
        fetch_bytes("https://example.test")


def test_cash_rows_allow_zero_lot_and_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(SEGMENTS["NSE"], "min_rows", 3)
    header = list(SEGMENTS["NSE"]["header"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(
        [
            ["NSE", "1", "1", "RELIANCE-EQ", "Reliance", "INE002A01018", "0.05", "0"],
            ["NSE", "26000", "65", "NIFTY", "Nifty 50", "", "0", "0"],
            ["NSE", "26012", "0", "NIFTY 100", "Nifty 100", "", "0", "0"],
        ]
    )
    _info, keys, population = validate_segment("NSE", buf.getvalue().encode())
    assert ("NSE", "26000") in keys
    assert population == {"segment": "NSE", "zero_lot_rows": 1, "zero_tick_rows": 2}


def test_validate_segment_rejects_non_finite_tick() -> None:
    header = list(SEGMENTS["NSE"]["header"])
    row = ["NSE", "1", "1", "SYM", "Name", "INE", "nan", "1"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    with pytest.raises(DownloadError, match="TickSize is not finite"):
        validate_segment("NSE", buf.getvalue().encode())
    row[6] = "inf"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerow(row)
    with pytest.raises(DownloadError, match="TickSize is not finite"):
        validate_segment("NSE", buf.getvalue().encode())


def test_indices_key_missing_from_nfo_and_bfo_is_rejected() -> None:
    coverage = indices_coverage(
        {
            "Indices": {("NFO", "1"), ("BSE", "2")},
            "NFO": {("NFO", "1")},
            "BFO": set(),
        }
    )
    with pytest.raises(DownloadError, match="missing from NFO and BFO"):
        require_indices_coverage(coverage)


def test_indices_key_in_both_books_is_rejected() -> None:
    coverage = indices_coverage(
        {
            "Indices": {("NFO", "1")},
            "NFO": {("NFO", "1")},
            "BFO": {("NFO", "1")},
        }
    )
    with pytest.raises(DownloadError, match="in both NFO and BFO"):
        require_indices_coverage(coverage)


def _shrink_min_rows(monkeypatch: pytest.MonkeyPatch, count: int = 4) -> None:
    for spec in SEGMENTS.values():
        monkeypatch.setitem(spec, "min_rows", count)


def _install_fetch(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes]) -> None:
    monkeypatch.setattr(
        "scorch.firstock.symbols.fetch_bytes",
        lambda url: payloads[url.split("/")[-1].split("?")[0]],
    )


def _baseline_manifest(rows: int, nfo_overlap: int, bfo_overlap: int) -> dict:
    return {
        "segments": [{"segment": name, "rows": rows} for name in SEGMENTS],
        "overlaps": [
            {"segment": "Indices", "also_in": "NFO", "rows": nfo_overlap},
            {"segment": "Indices", "also_in": "BFO", "rows": bfo_overlap},
        ],
    }


def test_row_drop_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    (tmp_path / "instruments.csv").write_bytes(b"old-book")
    (tmp_path / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=40, nfo_overlap=2, bfo_overlap=2))
    )
    _install_fetch(monkeypatch, _segment_payloads())
    with pytest.raises(DownloadError, match="row count changed"):
        download_symbols(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old-book"


def test_overlap_drop_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    (tmp_path / "instruments.csv").write_bytes(b"old-book")
    (tmp_path / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=4, nfo_overlap=100, bfo_overlap=2))
    )
    _install_fetch(monkeypatch, _segment_payloads())
    with pytest.raises(DownloadError, match="Indices overlap with NFO"):
        download_symbols(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old-book"


def test_download_records_same_day_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    (tmp_path / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=4, nfo_overlap=2, bfo_overlap=2))
    )
    _install_fetch(monkeypatch, _segment_payloads())
    download_symbols(tmp_path)
    body = json.loads((tmp_path / "manifest.json").read_text())
    assert body["baseline"]["source"] == "same_directory"
    assert {item["relative_change"] for item in body["baseline"]["segments"]} == {0}
    assert {item["relative_change"] for item in body["baseline"]["overlaps"]} == {0}


def test_first_publish_of_a_day_compares_with_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    day = tmp_path / "20260928"
    previous = tmp_path / "20260927"
    previous.mkdir()
    (previous / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=4, nfo_overlap=2, bfo_overlap=2))
    )
    _install_fetch(monkeypatch, _segment_payloads())
    download_symbols(day)
    body = json.loads((day / "manifest.json").read_text())
    assert body["baseline"]["source"] == "previous_day"
    assert body["baseline"]["calendar_day"] == "2026-09-27"


def test_same_day_baseline_wins_over_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    day = tmp_path / "20260928"
    day.mkdir()
    (day / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=4, nfo_overlap=2, bfo_overlap=2))
    )
    previous = tmp_path / "20260927"
    previous.mkdir()
    (previous / "manifest.json").write_text(
        json.dumps(_baseline_manifest(rows=400, nfo_overlap=200, bfo_overlap=200))
    )
    _install_fetch(monkeypatch, _segment_payloads())
    download_symbols(day)
    body = json.loads((day / "manifest.json").read_text())
    assert body["baseline"]["source"] == "same_directory"
    assert body["baseline"]["calendar_day"] == "2026-09-28"


def test_unreadable_manifest_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shrink_min_rows(monkeypatch)
    (tmp_path / "instruments.csv").write_bytes(b"old-book")
    (tmp_path / "manifest.json").write_text("{")
    _install_fetch(monkeypatch, _segment_payloads())
    with pytest.raises(DownloadError, match="unreadable manifest"):
        download_symbols(tmp_path)
    assert (tmp_path / "instruments.csv").read_bytes() == b"old-book"
