"""One instrument-master shape for every broker.

The vendor dumps are validated, then discarded. The snapshot stores
`instruments.csv` in this column order and nothing else beside the manifest.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from scorch.numbers import parse_finite
from scorch.snapshot import MANIFEST_SCHEMA_VERSION, DownloadError, exchange_day

COLUMNS: tuple[str, ...] = (
    "exchange",
    "token",
    "trading_symbol",
    "underlying",
    "instrument",
    "expiry",
    "strike",
    "lot_size",
    "tick_size",
)

_INSTRUMENTS = frozenset({"EQ", "FUT", "CE", "PE", "INDEX"})
# Currency quotes move in fractions of a paisa, so they cannot use this schema.
_CURRENCY_EXCHANGES = frozenset({"CDS", "BCD"})

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


def format_number(number: float) -> str:
    """Render a vendor number without trailing zeros."""
    text = format(number, ".10f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def canonical_number(value: str, label: str) -> str:
    return format_number(parse_finite(value, label))


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


def assemble(
    *,
    exchange: str,
    token: str,
    trading_symbol: str,
    underlying: str,
    instrument: str,
    expiry: str,
    strike: str,
    lot_size: str,
    tick_size: str,
    where: str,
) -> dict[str, str]:
    if instrument not in _INSTRUMENTS:
        raise DownloadError(f"{where} has instrument {instrument!r}")
    if exchange == "" or token == "" or trading_symbol == "" or underlying == "":
        raise DownloadError(f"{where} is missing exchange, token, or symbol")
    return {
        "exchange": exchange,
        "token": token,
        "trading_symbol": trading_symbol,
        "underlying": underlying,
        "instrument": instrument,
        "expiry": expiry,
        "strike": strike,
        "lot_size": lot_size,
        "tick_size": tick_size,
    }


def _dicts(payload: bytes) -> Iterable[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DownloadError("instrument CSV is not valid UTF-8") from error
    reader = csv.DictReader(io.StringIO(text))
    for line_no, raw in enumerate(reader, start=2):
        yield line_no, {key: (value or "").strip() for key, value in raw.items()}


def _strike(value: str, instrument: str, where: str) -> str:
    number = parse_finite(value, f"{where} strike")
    if instrument in {"CE", "PE"}:
        if number <= 0:
            raise DownloadError(f"{where} option strike is {value!r}")
        return to_paise(value, f"{where} strike")
    if number > 0:
        raise DownloadError(f"{where} {instrument} has strike {value!r}")
    return ""


def rows_from_firstock(segment: str, payload: bytes) -> list[dict[str, str]]:
    """Normalize one Firstock segment. The Indices file is not emitted."""
    if segment == "Indices":
        return []
    rows: list[dict[str, str]] = []
    for line_no, raw in _dicts(payload):
        where = f"{segment} row {line_no}"
        exchange = raw.get("Exchange", "")
        if exchange != segment:
            raise DownloadError(f"{where} has Exchange {exchange!r}")
        token = raw.get("Token", "")
        trading_symbol = raw.get("TradingSymbol", "")
        lot_size = canonical_number(raw.get("LotSize", ""), f"{where} lot_size")
        tick_size = to_paise(raw.get("TickSize", ""), f"{where} tick_size")
        if segment in {"NSE", "BSE"}:
            instrument = _cash_instrument(raw.get("ISIN", ""))
            underlying = trading_symbol
            expiry = ""
            strike = ""
        else:
            kind = raw.get("Instrument", "").upper()
            option = raw.get("OptionType", "").upper()
            instrument = _firstock_instrument(kind, option, where)
            underlying = raw.get("Symbol", "")
            expiry = parse_expiry(raw.get("Expiry", ""), where)
            if expiry == "":
                raise DownloadError(f"{where} is missing expiry")
            strike = _strike(raw.get("StrikePrice", ""), instrument, where)
        rows.append(
            assemble(
                exchange=exchange,
                token=token,
                trading_symbol=trading_symbol,
                underlying=underlying,
                instrument=instrument,
                expiry=expiry,
                strike=strike,
                lot_size=lot_size,
                tick_size=tick_size,
                where=where,
            )
        )
    return rows


def _cash_instrument(isin: str) -> str:
    """An empty ISIN is an index. Any other cash row is an equity."""
    if isin.strip() == "":
        return "INDEX"
    return "EQ"


def _firstock_instrument(kind: str, option: str, where: str) -> str:
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
    for line_no, raw in _dicts(payload):
        where = f"instruments row {line_no}"
        if raw.get("exchange", "").upper() in _CURRENCY_EXCHANGES:
            skipped += 1
            continue
        segment = raw.get("segment", "")
        vendor_type = raw.get("instrument_type", "").upper()
        if segment.upper() == "INDICES":
            if vendor_type not in {"EQ", "INDEX"}:
                raise DownloadError(
                    f"{where} index has instrument_type {vendor_type!r}"
                )
            instrument = "INDEX"
        elif vendor_type in _INSTRUMENTS:
            instrument = vendor_type
        else:
            raise DownloadError(f"{where} has instrument_type {vendor_type!r}")
        expiry = parse_expiry(raw.get("expiry", ""), where)
        if instrument in {"CE", "PE", "FUT"} and expiry == "":
            raise DownloadError(f"{where} is missing expiry")
        if instrument in {"EQ", "INDEX"} and expiry != "":
            raise DownloadError(f"{where} {instrument} has expiry {expiry}")
        strike = _strike(raw.get("strike", ""), instrument, where)
        trading_symbol = raw.get("tradingsymbol", "")
        underlying = (
            raw.get("name", "") if instrument in {"CE", "PE", "FUT"} else trading_symbol
        )
        rows.append(
            assemble(
                exchange=raw.get("exchange", ""),
                token=raw.get("instrument_token", ""),
                trading_symbol=trading_symbol,
                underlying=underlying,
                instrument=instrument,
                expiry=expiry,
                strike=strike,
                lot_size=canonical_number(raw.get("lot_size", ""), f"{where} lot_size"),
                tick_size=to_paise(raw.get("tick_size", ""), f"{where} tick_size"),
                where=where,
            )
        )
    if skipped:
        logger.info(
            "left out %s currency rows; their prices are finer than one paisa",
            skipped,
        )
    return rows


def build_book(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Sort, reject duplicate identities, and encode `instruments.csv`."""
    if not rows:
        raise DownloadError("refusing to publish an empty instrument list")
    seen_tokens: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["exchange"], row["token"])
        if key in seen_tokens:
            raise DownloadError(f"duplicate exchange,token {key[0]},{key[1]}")
        seen_tokens.add(key)
    rows.sort(key=lambda row: (row["exchange"], row["trading_symbol"], row["token"]))
    payload = _render(rows)
    exchanges, population = _summarize(rows)
    return {
        "csv": payload,
        "rows": len(rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "exchanges": exchanges,
        "population": population,
    }


def _render(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row[column] for column in COLUMNS])
    return buffer.getvalue().encode("utf-8")


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
        "timezone": "Asia/Kolkata",
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
    body["baseline"] = baseline
    body["segments"] = book["exchanges"]
    return json.dumps(body, indent=2).encode("utf-8")
