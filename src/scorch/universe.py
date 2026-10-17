"""Load the trading universe from ``config/universe.toml``.

The file is the policy: exchanges, spot venues, series codes, index aliases,
brokers, and the floors that reject a short or drifting download. The morning
job points ``SCORCH_UNIVERSE`` at that file. A checkout finds it by walking up
from this module. Vendor column layouts and the ``instruments.csv`` shape stay
in the downloader.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TOP_LEVEL = frozenset(
    {
        "timezone",
        "http",
        "continuity",
        "series",
        "index_aliases",
        "exchanges",
        "brokers",
    }
)
_HTTP_KEYS = frozenset({"timeout_seconds", "retries", "retry_backoff_seconds"})
_CONTINUITY_KEYS = frozenset({"max_relative_change_bp", "retired_segments"})
_EXCHANGE_KEYS = frozenset({"role", "spot", "spot_fallback"})
_BROKER_KEYS = frozenset({"min_rows", "omit_without_spot", "segments"})
_SEGMENT_KEYS = frozenset({"min_rows", "publish"})
_SYMBOL = re.compile(r"[A-Z0-9]+")
_EXCHANGE = re.compile(r"[A-Z][A-Z0-9]*")
_BROKER = re.compile(r"[a-z][a-z0-9_-]*")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*")


class ConfigError(RuntimeError):
    """The universe file is missing or does not match the expected shape."""


@dataclass(frozen=True)
class HttpLimits:
    timeout_seconds: float
    retries: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class Continuity:
    max_relative_change_bp: int
    retired_segments: frozenset[str]


@dataclass(frozen=True)
class Exchange:
    role: str
    spot: str | None = None
    spot_fallback: str | None = None


@dataclass(frozen=True)
class SegmentLimit:
    min_rows: int
    publish: bool


@dataclass(frozen=True)
class Broker:
    min_rows: int | None
    omit_without_spot: frozenset[str]
    segments: Mapping[str, SegmentLimit]


@dataclass(frozen=True)
class Universe:
    path: Path
    timezone: str
    http: HttpLimits
    continuity: Continuity
    series: frozenset[str]
    index_aliases: Mapping[str, str]
    exchanges: Mapping[str, Exchange]
    brokers: Mapping[str, Broker]

    def cash_exchanges(self) -> frozenset[str]:
        return frozenset(
            name for name, item in self.exchanges.items() if item.role == "cash"
        )

    def derivative_exchanges(self) -> frozenset[str]:
        return frozenset(
            name for name, item in self.exchanges.items() if item.role == "derivative"
        )


def default_universe_path() -> Path:
    """The file named by ``SCORCH_UNIVERSE``, or ``config/universe.toml``."""
    override = os.environ.get("SCORCH_UNIVERSE")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "universe.toml"
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "universe config not found. Set SCORCH_UNIVERSE to the "
        "config/universe.toml file in the repository."
    )


def load_universe(path: Path | None = None) -> Universe:
    """Read and check one universe file."""
    source = path if path is not None else default_universe_path()
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise ConfigError(f"cannot read universe config {source}: {error}") from error
    try:
        parsed = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigError(f"{source} is not UTF-8") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{source}: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigError(f"{source} must be a table")
    return _universe(source, parsed)


@lru_cache(maxsize=1)
def universe() -> Universe:
    """The process universe. The first successful load is kept."""
    return load_universe()


def _universe(path: Path, raw: dict[str, Any]) -> Universe:
    _only(raw, _TOP_LEVEL, path, "universe")
    for key in _TOP_LEVEL:
        if key not in raw:
            _fail(path, f"universe is missing {key}")
    timezone = _timezone(raw["timezone"], path)
    http = _http(raw["http"], path)
    continuity = _continuity(raw["continuity"], path)
    series = _symbols(raw["series"], path, "series")
    aliases = _aliases(raw["index_aliases"], path)
    exchanges = _exchanges(raw["exchanges"], path)
    overlap = continuity.retired_segments & set(exchanges)
    if overlap:
        names = ", ".join(sorted(overlap))
        _fail(path, f"retired_segments includes exchanges: {names}")
    brokers = _brokers(raw["brokers"], path)
    return Universe(
        path=path,
        timezone=timezone,
        http=http,
        continuity=continuity,
        series=series,
        index_aliases=aliases,
        exchanges=exchanges,
        brokers=brokers,
    )


def _timezone(value: object, path: Path) -> str:
    if not isinstance(value, str) or value == "":
        _fail(path, "timezone must be a zone name")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        _fail(path, f"timezone {value!r} is not a known zone")
    return value


def _http(value: object, path: Path) -> HttpLimits:
    table = _table(value, path, "http")
    _only(table, _HTTP_KEYS, path, "http")
    for key in _HTTP_KEYS:
        if key not in table:
            _fail(path, f"http is missing {key}")
    timeout = _number(table["timeout_seconds"], path, "http.timeout_seconds")
    if timeout <= 0:
        _fail(path, "http.timeout_seconds must be positive")
    retries = _int(table["retries"], path, "http.retries")
    if retries < 1:
        _fail(path, "http.retries must be at least 1")
    backoff = _number(
        table["retry_backoff_seconds"], path, "http.retry_backoff_seconds"
    )
    if backoff < 0:
        _fail(path, "http.retry_backoff_seconds must be zero or positive")
    return HttpLimits(
        timeout_seconds=timeout,
        retries=retries,
        retry_backoff_seconds=backoff,
    )


def _continuity(value: object, path: Path) -> Continuity:
    table = _table(value, path, "continuity")
    _only(table, _CONTINUITY_KEYS, path, "continuity")
    for key in _CONTINUITY_KEYS:
        if key not in table:
            _fail(path, f"continuity is missing {key}")
    limit = _int(
        table["max_relative_change_bp"], path, "continuity.max_relative_change_bp"
    )
    if limit < 0:
        _fail(path, "continuity.max_relative_change_bp must be zero or positive")
    retired = _names(table["retired_segments"], path, "continuity.retired_segments")
    return Continuity(max_relative_change_bp=limit, retired_segments=retired)


def _aliases(value: object, path: Path) -> Mapping[str, str]:
    table = _table(value, path, "index_aliases")
    aliases: dict[str, str] = {}
    for key, item in table.items():
        folded = _symbol(key, path, "index_aliases")
        aliases[folded] = _symbol(item, path, f"index_aliases.{key}")
    return MappingProxyType(aliases)


def _exchanges(value: object, path: Path) -> Mapping[str, Exchange]:
    table = _table(value, path, "exchanges")
    if not table:
        _fail(path, "exchanges must list at least one exchange")
    parsed: dict[str, Exchange] = {}
    for name, body in table.items():
        if not _EXCHANGE.fullmatch(name):
            _fail(path, f"exchange name {name!r} must be uppercase")
        item = _table(body, path, f"exchanges.{name}")
        _only(item, _EXCHANGE_KEYS, path, f"exchanges.{name}")
        if "role" not in item:
            _fail(path, f"exchanges.{name} is missing role")
        role = item["role"]
        if role not in {"cash", "derivative"}:
            _fail(path, f"exchanges.{name}.role must be cash or derivative")
        spot = _optional_symbol(item, "spot", path, f"exchanges.{name}.spot")
        fallback = _optional_symbol(
            item, "spot_fallback", path, f"exchanges.{name}.spot_fallback"
        )
        if role == "cash" and (spot is not None or fallback is not None):
            _fail(path, f"exchanges.{name} is cash and cannot name a spot")
        if role == "derivative" and spot is None:
            _fail(path, f"exchanges.{name} must name spot")
        parsed[name] = Exchange(role=role, spot=spot, spot_fallback=fallback)
    cash = {name for name, item in parsed.items() if item.role == "cash"}
    if not cash:
        _fail(path, "exchanges must include a cash exchange")
    for name, item in parsed.items():
        if item.role != "derivative":
            continue
        if item.spot not in cash:
            _fail(path, f"exchanges.{name}.spot must be a cash exchange")
        if item.spot_fallback is None:
            continue
        if item.spot_fallback not in cash:
            _fail(path, f"exchanges.{name}.spot_fallback must be a cash exchange")
        if item.spot_fallback == item.spot:
            _fail(path, f"exchanges.{name}.spot_fallback must differ from spot")
    return MappingProxyType(parsed)


def _brokers(value: object, path: Path) -> Mapping[str, Broker]:
    table = _table(value, path, "brokers")
    if not table:
        _fail(path, "brokers must list at least one broker")
    parsed: dict[str, Broker] = {}
    for name, body in table.items():
        if not _BROKER.fullmatch(name):
            _fail(path, f"broker name {name!r} must be a lowercase token")
        item = _table(body, path, f"brokers.{name}")
        _only(item, _BROKER_KEYS, path, f"brokers.{name}")
        has_rows = "min_rows" in item
        has_segments = "segments" in item
        if has_rows == has_segments:
            _fail(path, f"brokers.{name} must set min_rows or segments")
        min_rows = None
        if has_rows:
            min_rows = _floor(item["min_rows"], path, f"brokers.{name}.min_rows")
        if has_segments:
            segments = _segments(item["segments"], path, name)
        else:
            segments = MappingProxyType({})
        omit = _symbols(
            item.get("omit_without_spot", []),
            path,
            f"brokers.{name}.omit_without_spot",
        )
        parsed[name] = Broker(
            min_rows=min_rows,
            omit_without_spot=omit,
            segments=segments,
        )
    return MappingProxyType(parsed)


def _segments(value: object, path: Path, broker: str) -> Mapping[str, SegmentLimit]:
    table = _table(value, path, f"brokers.{broker}.segments")
    if not table:
        _fail(path, f"brokers.{broker}.segments must list at least one segment")
    parsed: dict[str, SegmentLimit] = {}
    for name, body in table.items():
        if not _NAME.fullmatch(name):
            _fail(path, f"brokers.{broker}.segments.{name} is not a segment name")
        item = _table(body, path, f"brokers.{broker}.segments.{name}")
        _only(item, _SEGMENT_KEYS, path, f"brokers.{broker}.segments.{name}")
        if "min_rows" not in item:
            _fail(path, f"brokers.{broker}.segments.{name} is missing min_rows")
        floor = _floor(
            item["min_rows"], path, f"brokers.{broker}.segments.{name}.min_rows"
        )
        publish = True
        if "publish" in item:
            publish = _bool(
                item["publish"],
                path,
                f"brokers.{broker}.segments.{name}.publish",
            )
        parsed[name] = SegmentLimit(min_rows=floor, publish=publish)
    return MappingProxyType(parsed)


def _optional_symbol(
    table: Mapping[str, Any], key: str, path: Path, where: str
) -> str | None:
    if key not in table:
        return None
    return _symbol(table[key], path, where)


def _symbols(value: object, path: Path, where: str) -> frozenset[str]:
    if not isinstance(value, list):
        _fail(path, f"{where} must be an array")
    seen: set[str] = set()
    for item in value:
        symbol = _symbol(item, path, where)
        if symbol in seen:
            _fail(path, f"{where} lists {symbol} more than once")
        seen.add(symbol)
    return frozenset(seen)


def _names(value: object, path: Path, where: str) -> frozenset[str]:
    if not isinstance(value, list):
        _fail(path, f"{where} must be an array")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _NAME.fullmatch(item):
            _fail(path, f"{where} must be names, got {item!r}")
        if item in seen:
            _fail(path, f"{where} lists {item} more than once")
        seen.add(item)
    return frozenset(seen)


def _symbol(value: object, path: Path, where: str) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        _fail(path, f"{where} must be an uppercase symbol, got {value!r}")
    return value


def _table(value: object, path: Path, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, f"{where} must be a table")
    return value


def _only(
    table: Mapping[str, Any], allowed: frozenset[str], path: Path, where: str
) -> None:
    extra = set(table) - allowed
    if extra:
        names = ", ".join(sorted(extra))
        _fail(path, f"{where} has unknown keys: {names}")


def _floor(value: object, path: Path, where: str) -> int:
    number = _int(value, path, where)
    if number < 1:
        _fail(path, f"{where} must be at least 1")
    return number


def _int(value: object, path: Path, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"{where} must be an integer")
    return value


def _number(value: object, path: Path, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(path, f"{where} must be a number")
    return float(value)


def _bool(value: object, path: Path, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"{where} must be true or false")
    return value


def _fail(path: Path, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")
