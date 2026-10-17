"""One instrument-master shape for every broker.

The vendor dumps are validated, then discarded. The snapshot stores
`instruments.csv` in this column order and nothing else beside the manifest.
The C++ process reads that file as-is: no cleanup, no derivation, no unit
conversion. This module owns the canonical key and the spot link. Which
exchanges are written, where a derivative's spot lives, the series codes,
and the index aliases come from the universe file.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from scorch.numbers import parse_finite
from scorch.snapshot import MANIFEST_SCHEMA_VERSION, DownloadError, exchange_day
from scorch.universe import universe

COLUMNS: tuple[str, ...] = (
    "key",
    "exchange",
    "token",
    "trading_symbol",
    "kind",
    "underlying",
    "expiry",
    "strike",
    "lot_size",
    "tick_size",
    "spot_key",
)

# Kinds and column order are the file the trading process reads.
_KINDS = frozenset({"EQ", "IDX", "FUT", "CE", "PE"})
_NUMERIC = ("token", "expiry", "strike", "lot_size", "tick_size")

logger = logging.getLogger(__name__)
_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def to_paise(value: str, label: str) -> str:
    """Rupees from the vendor dump, as a whole number of paise."""
    parse_finite(value, label)
    amount = Decimal(value.strip()) * 100
    paise = amount.to_integral_value()
    if amount != paise:
        raise DownloadError(f"{label} is not a whole number of paise: {value!r}")
    return str(paise)


def parse_expiry(value: str, label: str) -> str:
    """Return `YYYYMMDD`, or an empty string when the vendor left it blank."""
    text = value.strip()
    if text == "":
        return ""
    parsed = _expiry_date(text)
    if parsed is None:
        raise DownloadError(f"{label} has expiry {text!r}")
    return parsed.strftime("%Y%m%d")


def _expiry_date(text: str) -> date | None:
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None
    parts = text.split("-")
    if len(parts) == 3 and len(parts[0]) == 2 and len(parts[2]) == 4:
        month = _MONTHS.get(parts[1].upper())
        if month is not None and parts[0].isdigit() and parts[2].isdigit():
            try:
                return date(int(parts[2]), month, int(parts[0]))
            except ValueError:
                return None
    return None


def _whole(value: str, label: str) -> str:
    """A vendor count as a non-negative integer, without a decimal point."""
    text = value.strip()
    parse_finite(text, label)
    amount = Decimal(text)
    whole = amount.to_integral_value()
    if amount != whole:
        raise DownloadError(f"{label} is not a whole number: {value!r}")
    if whole < 0:
        raise DownloadError(f"{label} is negative: {value!r}")
    return str(whole)


def _fold(text: str) -> str:
    """Uppercase, with spaces removed. Keys and underlyings use this form."""
    return "".join(text.split()).upper()


def _index_underlying(trading_symbol: str) -> str:
    folded = _fold(trading_symbol)
    return universe().index_aliases.get(folded, folded)


def _equity_parts(trading_symbol: str) -> tuple[str, str]:
    """Symbol and series. The series is EQ when the vendor symbol has none."""
    text = trading_symbol.strip().upper()
    if "-" in text:
        head, tail = text.rsplit("-", 1)
        if tail == "NAN":
            # Firstock writes the literal "nan" where Kite writes NA.
            tail = "NA"
        if head != "" and tail in universe().series:
            return _fold(head), tail
    return _fold(text), "EQ"


def _row(
    *,
    exchange: str,
    token: str,
    trading_symbol: str,
    kind: str,
    underlying: str,
    expiry: str,
    strike: str,
    lot_size: str,
    tick_size: str,
    series: str,
    where: str,
) -> dict[str, str]:
    book = universe()
    if exchange not in book.exchanges:
        raise DownloadError(f"{where} has exchange {exchange!r}")
    if kind not in _KINDS:
        raise DownloadError(f"{where} has kind {kind!r}")
    if kind in {"FUT", "CE", "PE"} and exchange not in book.derivative_exchanges():
        raise DownloadError(f"{where} {kind} is on {exchange}")
    if kind in {"EQ", "IDX"} and exchange not in book.cash_exchanges():
        raise DownloadError(f"{where} {kind} is on {exchange}")
    if underlying == "" or trading_symbol == "":
        raise DownloadError(f"{where} is missing a symbol")
    # An index is not tradable. Firstock copies the derivative lot onto the
    # index row; Kite sends 0. The contract lot stays on the future and option.
    if kind == "IDX":
        lot_size = "0"
    if kind == "EQ":
        key = f"{exchange}_{underlying}_{series}"
    elif kind == "IDX":
        key = f"{exchange}_{underlying}_IDX"
    elif kind == "FUT":
        key = f"{exchange}_{underlying}_{expiry}_FUT"
    else:
        key = f"{exchange}_{underlying}_{expiry}_{kind}_{strike}"
    row = {
        "key": key,
        "exchange": exchange,
        "token": token,
        "trading_symbol": trading_symbol,
        "kind": kind,
        "underlying": underlying,
        "expiry": expiry,
        "strike": strike,
        "lot_size": lot_size,
        "tick_size": tick_size,
        "spot_key": "",
    }
    _check_fields(row, where)
    return row


def _check_fields(row: Mapping[str, str], where: str) -> None:
    for column in COLUMNS:
        value = row[column]
        if column != "spot_key" and value == "":
            raise DownloadError(f"{where} {column} is empty")
        if any(char in value for char in ',"\r\n'):
            raise DownloadError(f"{where} {column} has a comma or quote: {value!r}")
        if value != value.strip():
            raise DownloadError(f"{where} {column} has trailing whitespace")
        if column != "trading_symbol" and any(char.isspace() for char in value):
            raise DownloadError(f"{where} {column} has whitespace: {value!r}")
    for column in _NUMERIC:
        if not row[column].isdigit():
            raise DownloadError(f"{where} {column} is not an integer: {row[column]!r}")


def _dicts(payload: bytes) -> Iterable[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DownloadError("instrument CSV is not valid UTF-8") from error
    reader = csv.DictReader(io.StringIO(text))
    for line_no, raw in enumerate(reader, start=2):
        yield line_no, {key: (value or "").strip() for key, value in raw.items()}


def _strike(value: str, kind: str, where: str) -> str:
    number = parse_finite(value, f"{where} strike")
    if kind in {"CE", "PE"}:
        if number <= 0:
            raise DownloadError(f"{where} option strike is {value!r}")
        return to_paise(value, f"{where} strike")
    if number > 0:
        raise DownloadError(f"{where} {kind} has strike {value!r}")
    return "0"


def rows_from_firstock(segment: str, payload: bytes) -> list[dict[str, str]]:
    """Normalize one Firstock segment. ``publish = false`` segments are not emitted."""
    broker = universe().brokers.get("firstock")
    if broker is not None:
        limit = broker.segments.get(segment)
        if limit is not None and not limit.publish:
            return []
    cash = universe().cash_exchanges()
    rows: list[dict[str, str]] = []
    for line_no, raw in _dicts(payload):
        where = f"{segment} row {line_no}"
        exchange = raw.get("Exchange", "")
        if exchange != segment:
            raise DownloadError(f"{where} has Exchange {exchange!r}")
        token = _whole(raw.get("Token", ""), f"{where} token")
        trading_symbol = raw.get("TradingSymbol", "")
        lot_size = _whole(raw.get("LotSize", ""), f"{where} lot_size")
        tick_size = to_paise(raw.get("TickSize", ""), f"{where} tick_size")
        if segment in cash:
            if raw.get("ISIN", "").strip() == "":
                underlying = _index_underlying(trading_symbol)
                series = ""
                kind = "IDX"
                expiry = "0"
                strike = "0"
            else:
                underlying, series = _equity_parts(trading_symbol)
                kind = "EQ"
                expiry = "0"
                strike = "0"
        else:
            series = ""
            vendor_kind = raw.get("Instrument", "").upper()
            option = raw.get("OptionType", "").upper()
            kind = _firstock_kind(vendor_kind, option, where)
            underlying = _fold(raw.get("Symbol", ""))
            expiry = parse_expiry(raw.get("Expiry", ""), where)
            if expiry == "":
                raise DownloadError(f"{where} is missing expiry")
            strike = _strike(raw.get("StrikePrice", ""), kind, where)
        rows.append(
            _row(
                exchange=exchange,
                token=token,
                trading_symbol=trading_symbol,
                kind=kind,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                lot_size=lot_size,
                tick_size=tick_size,
                series=series,
                where=where,
            )
        )
    return rows


def _firstock_kind(kind: str, option: str, where: str) -> str:
    if option == "CE" and kind in {"OPTIDX", "OPTSTK"}:
        return "CE"
    if option == "PE" and kind in {"OPTIDX", "OPTSTK"}:
        return "PE"
    if option == "FUT" and kind in {"FUTIDX", "FUTSTK"}:
        return "FUT"
    raise DownloadError(f"{where} has instrument {kind} option {option}")


def rows_from_kite(payload: bytes) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    skipped = 0
    traded = universe().exchanges
    for line_no, raw in _dicts(payload):
        where = f"instruments row {line_no}"
        exchange = raw.get("exchange", "").upper()
        if exchange not in traded:
            skipped += 1
            continue
        segment = raw.get("segment", "")
        vendor_type = raw.get("instrument_type", "").upper()
        trading_symbol = raw.get("tradingsymbol", "")
        expiry_text = parse_expiry(raw.get("expiry", ""), where)
        if segment.upper() == "INDICES":
            if vendor_type not in {"EQ", "INDEX"}:
                raise DownloadError(
                    f"{where} index has instrument_type {vendor_type!r}"
                )
            kind = "IDX"
            underlying = _index_underlying(trading_symbol)
            series = ""
        elif vendor_type == "EQ":
            kind = "EQ"
            underlying, series = _equity_parts(trading_symbol)
        elif vendor_type in {"CE", "PE", "FUT"}:
            kind = vendor_type
            underlying = _fold(raw.get("name", ""))
            series = ""
        else:
            raise DownloadError(f"{where} has instrument_type {vendor_type!r}")
        if kind in {"CE", "PE", "FUT"} and expiry_text == "":
            raise DownloadError(f"{where} is missing expiry")
        if kind in {"EQ", "IDX"} and expiry_text != "":
            raise DownloadError(f"{where} {kind} has expiry {expiry_text}")
        rows.append(
            _row(
                exchange=exchange,
                token=_whole(raw.get("instrument_token", ""), f"{where} token"),
                trading_symbol=trading_symbol,
                kind=kind,
                underlying=underlying,
                expiry=expiry_text or "0",
                strike=_strike(raw.get("strike", ""), kind, where),
                lot_size=_whole(raw.get("lot_size", ""), f"{where} lot_size"),
                tick_size=to_paise(raw.get("tick_size", ""), f"{where} tick_size"),
                series=series,
                where=where,
            )
        )
    if skipped:
        kept = ", ".join(sorted(universe().exchanges))
        logger.info(
            "left out %s rows on exchanges other than %s",
            skipped,
            kept,
        )
    return rows


def build_book(
    rows: list[dict[str, str]],
    *,
    omit_without_spot: Collection[str] = (),
) -> dict[str, Any]:
    """Link spots, reject duplicate identities, and encode `instruments.csv`."""
    if not rows:
        raise DownloadError("refusing to publish an empty instrument list")
    linked, omitted = _link_spots([dict(row) for row in rows], omit_without_spot)
    if not linked:
        raise DownloadError("refusing to publish an empty instrument list")
    seen_keys: set[str] = set()
    seen_tokens: set[tuple[str, str]] = set()
    for row in linked:
        key = row["key"]
        if key in seen_keys:
            raise DownloadError(f"duplicate key {key}")
        seen_keys.add(key)
        identity = (row["exchange"], row["token"])
        if identity in seen_tokens:
            raise DownloadError(f"duplicate exchange,token {identity[0]},{identity[1]}")
        seen_tokens.add(identity)
    for row in linked:
        spot = row["spot_key"]
        if spot != "" and spot not in seen_keys:
            raise DownloadError(f"spot_key {spot} is not in the file")
    linked.sort(key=lambda row: row["key"])
    for item in omitted:
        logger.info(
            "left out %s %s derivative rows; no spot instrument in the vendor file",
            item["rows"],
            item["underlying"],
        )
    payload = _render(linked)
    exchanges, population = _summarize(linked)
    return {
        "csv": payload,
        "rows": len(linked),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "exchanges": exchanges,
        "population": population,
        "omitted_spots": omitted,
    }


def _link_spots(
    rows: list[dict[str, str]],
    omit_without_spot: Collection[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Fill spot_key. Drop derivatives whose spot the vendor does not publish."""
    present = {row["key"] for row in rows}
    omit = set(omit_without_spot)
    exchanges = universe().exchanges
    kept: list[dict[str, str]] = []
    omitted: dict[str, int] = {}
    for row in rows:
        if row["kind"] not in {"FUT", "CE", "PE"}:
            row["spot_key"] = ""
            kept.append(row)
            continue
        spec = exchanges[row["exchange"]]
        home = spec.spot
        if home is None:
            raise DownloadError(f"{row['exchange']} has no spot exchange configured")
        underlying = row["underlying"]
        home_index = f"{home}_{underlying}_IDX"
        home_equity = f"{home}_{underlying}_EQ"
        fallback_equity = ""
        if spec.spot_fallback is not None:
            fallback_equity = f"{spec.spot_fallback}_{underlying}_EQ"
        # The home listing wins. The fallback cash market is only for a stock
        # the home exchange does not list (CDSL and BSE Ltd on BFO).
        if home_index in present and home_equity in present:
            raise DownloadError(
                f"{underlying} is both an index and an equity on {home}"
            )
        if home_index in present:
            row["spot_key"] = home_index
        elif home_equity in present:
            row["spot_key"] = home_equity
        elif fallback_equity != "" and fallback_equity in present:
            row["spot_key"] = fallback_equity
        elif underlying in omit:
            omitted[underlying] = omitted.get(underlying, 0) + 1
            continue
        else:
            raise DownloadError(
                f"no spot instrument for {row['exchange']} {underlying} ({row['key']})"
            )
        kept.append(row)
    listed = [{"underlying": name, "rows": omitted[name]} for name in sorted(omitted)]
    return kept, listed


def _render(rows: list[dict[str, str]]) -> bytes:
    """Plain CSV. No quotes. Spaces are allowed only in trading_symbol."""
    lines = [",".join(COLUMNS)]
    for row in rows:
        fields = [row[column] for column in COLUMNS]
        for column, value in zip(COLUMNS, fields, strict=True):
            if any(char in value for char in ',"\r\n'):
                raise DownloadError(f"{column} is not plain CSV text: {value!r}")
            if column != "trading_symbol" and " " in value:
                raise DownloadError(f"{column} contains a space: {value!r}")
        lines.append(",".join(fields))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _summarize(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    counts: dict[str, int] = {}
    zero_lot: dict[str, int] = {}
    zero_tick: dict[str, int] = {}
    for row in rows:
        exchange = row["exchange"]
        counts[exchange] = counts.get(exchange, 0) + 1
        zero_lot[exchange] = zero_lot.get(exchange, 0) + (row["lot_size"] == "0")
        zero_tick[exchange] = zero_tick.get(exchange, 0) + (row["tick_size"] == "0")
    exchanges = [{"segment": name, "rows": counts[name]} for name in sorted(counts)]
    population = [
        {
            "segment": name,
            "zero_lot_rows": zero_lot[name],
            "zero_tick_rows": zero_tick[name],
        }
        for name in sorted(counts)
    ]
    return exchanges, population


def render_manifest(
    *,
    source: str,
    book: Mapping[str, Any],
    now: datetime | None = None,
    baseline: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    overlaps: list[dict[str, Any]] | None = None,
    indices_coverage: dict[str, int] | None = None,
) -> bytes:
    moment = now or datetime.now(UTC)
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": source,
        "downloaded_at": moment.isoformat(),
        "calendar_day": exchange_day(moment),
        "timezone": universe().timezone,
        "dedup_key": ["exchange", "token"],
        "filename": "instruments.csv",
        "bytes": len(book["csv"]),
        "rows": book["rows"],
        "sha256": book["sha256"],
        "header": list(COLUMNS),
        "duration_ms": duration_ms,
    }
    if overlaps is not None:
        body["overlaps"] = overlaps
    if indices_coverage is not None:
        body["indices_coverage"] = indices_coverage
    body["population"] = book["population"]
    body["omitted_spots"] = book.get("omitted_spots", [])
    body["baseline"] = baseline
    body["segments"] = book["exchanges"]
    return json.dumps(body, indent=2).encode("utf-8")
