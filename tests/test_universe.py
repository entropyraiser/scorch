"""The universe file is the trading book and the download limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from scorch.firstock.symbols import configured_segments
from scorch.universe import (
    ConfigError,
    default_universe_path,
    load_universe,
)

_MINIMAL = """\
timezone = "Asia/Kolkata"
series = ["EQ"]

[http]
timeout_seconds = 5
retries = 1
retry_backoff_seconds = 0

[continuity]
max_relative_change_bp = 4000
retired_segments = ["MCX"]

[index_aliases]

[exchanges.NSE]
role = "cash"

[exchanges.BSE]
role = "cash"

[exchanges.NFO]
role = "derivative"
spot = "NSE"
spot_fallback = "BSE"

[brokers.kite]
min_rows = 10

[brokers.firstock]
omit_without_spot = ["FOCIT"]

[brokers.firstock.segments.NSE]
min_rows = 10
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "universe.toml"
    path.write_text(text)
    return path


def test_shipped_universe_preserves_the_previous_book() -> None:
    book = load_universe()
    assert book.timezone == "Asia/Kolkata"
    assert book.http.timeout_seconds == 60
    assert book.http.retries == 3
    assert book.http.retry_backoff_seconds == 2
    assert book.continuity.max_relative_change_bp == 4000
    assert book.continuity.retired_segments == frozenset(
        {"Indices", "MCX", "NCO", "CDS", "BCD", "GLOBAL", "NSEIX"}
    )
    assert set(book.exchanges) == {"NSE", "BSE", "NFO", "BFO"}
    assert book.exchanges["NSE"].role == "cash"
    assert book.exchanges["BSE"].role == "cash"
    assert book.exchanges["NFO"].spot == "NSE"
    assert book.exchanges["NFO"].spot_fallback == "BSE"
    assert book.exchanges["BFO"].spot == "BSE"
    assert book.exchanges["BFO"].spot_fallback == "NSE"
    assert book.index_aliases["NIFTY50"] == "NIFTY"
    assert book.index_aliases["BSEFOCUSEDIT"] == "FOCIT"
    assert len(book.series) == 127
    assert {"EQ", "BE", "NA"} <= book.series
    assert "AUTO" not in book.series
    assert len(book.index_aliases) == 8
    assert list(book.brokers) == ["firstock", "kite"]
    firstock = book.brokers["firstock"]
    assert firstock.min_rows is None
    assert firstock.omit_without_spot == frozenset({"FOCIT"})
    assert {name: limit.min_rows for name, limit in firstock.segments.items()} == {
        "NSE": 5000,
        "BSE": 6000,
        "NFO": 30000,
        "BFO": 15000,
        "Indices": 8000,
    }
    assert firstock.segments["Indices"].publish is False
    assert firstock.segments["NFO"].publish is True
    kite = book.brokers["kite"]
    assert kite.min_rows == 50_000
    assert kite.omit_without_spot == frozenset()
    assert kite.segments == {}


def test_default_path_is_the_repo_file() -> None:
    path = default_universe_path()
    assert path.name == "universe.toml"
    assert path.parent.name == "config"


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, _MINIMAL)
    monkeypatch.setenv("SCORCH_UNIVERSE", str(path))
    assert default_universe_path() == path
    loaded = load_universe()
    assert loaded.brokers["kite"].min_rows == 10


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "extra = 1\n" + _MINIMAL)
    with pytest.raises(ConfigError, match="unknown keys: extra"):
        load_universe(path)


def test_derivative_must_name_a_spot(tmp_path: Path) -> None:
    text = _MINIMAL.replace('spot = "NSE"\n', "")
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="must name spot"):
        load_universe(path)


def test_cash_exchange_cannot_name_a_spot(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        '[exchanges.NSE]\nrole = "cash"\n',
        '[exchanges.NSE]\nrole = "cash"\nspot = "BSE"\n',
    )
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="cannot name a spot"):
        load_universe(path)


def test_retired_segment_cannot_be_an_exchange(tmp_path: Path) -> None:
    text = _MINIMAL.replace('retired_segments = ["MCX"]', 'retired_segments = ["NSE"]')
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="retired_segments includes exchanges"):
        load_universe(path)


def test_broker_cannot_set_min_rows_and_segments(tmp_path: Path) -> None:
    text = _MINIMAL + "\n[brokers.kite.segments.NSE]\nmin_rows = 1\n"
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="must set min_rows or segments"):
        load_universe(path)


def test_duplicate_series_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path, _MINIMAL.replace('series = ["EQ"]', 'series = ["EQ", "EQ"]')
    )
    with pytest.raises(ConfigError, match="lists EQ more than once"):
        load_universe(path)


def test_unknown_firstock_segment_is_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + "\n[brokers.firstock.segments.NOPE]\nmin_rows = 1\n"
    book = load_universe(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="no header"):
        configured_segments(book)


def test_published_segment_must_be_an_exchange(tmp_path: Path) -> None:
    text = _MINIMAL + "\n[brokers.firstock.segments.Indices]\nmin_rows = 1\n"
    book = load_universe(_write(tmp_path, text))
    with pytest.raises(ConfigError, match="not an exchange"):
        configured_segments(book)
