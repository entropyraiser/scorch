"""The normalized instrument book is the same shape for every broker."""

from __future__ import annotations

import csv
import io

import pytest

from scorch.normalize import (
    COLUMNS,
    build_book,
    rows_from_firstock,
    rows_from_kite,
)
from scorch.snapshot import DownloadError


def _csv(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode()


_CASH = [
    "Exchange",
    "Token",
    "LotSize",
    "TradingSymbol",
    "CompanyName",
    "ISIN",
    "TickSize",
    "FreezeQty",
]
_FO = [
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
]
_KITE = [
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
]


def test_firstock_cash_equity_and_index() -> None:
    payload = _csv(
        _CASH,
        [
            ["NSE", "1", "1", "RELIANCE-EQ", "Reliance", "INE002A01018", "0.05", "0.0"],
            ["NSE", "26000", "65", "NIFTY", "Nifty 50", "", "0", "0"],
            ["NSE", "26012", "0", "NIFTY 100", "Nifty 100", "", "0", "0"],
        ],
    )
    rows = rows_from_firstock("NSE", payload)
    by_symbol = {row["trading_symbol"]: row for row in rows}
    equity = by_symbol["RELIANCE-EQ"]
    assert equity["instrument"] == "EQ"
    assert equity["underlying"] == "RELIANCE-EQ"
    assert equity["expiry"] == ""
    assert equity["strike"] == ""
    assert equity["tick_size"] == "5"
    nifty = by_symbol["NIFTY"]
    assert nifty["instrument"] == "INDEX"
    assert nifty["underlying"] == "NIFTY"
    assert nifty["lot_size"] == "65"
    assert nifty["tick_size"] == "0"
    assert by_symbol["NIFTY 100"]["lot_size"] == "0"
    assert set(equity) == set(COLUMNS)


def test_junk_isin_stays_an_equity_and_does_not_become_the_id() -> None:
    bse_payload = _csv(
        _CASH,
        [
            ["BSE", "504671", "1", "CHASBRT", "CHASE BRIGHT", "NaN", "0.05", "0"],
            ["BSE", "531562", "1", "PUSHPIN", "PUSHPSONS", "NaN", "0.05", "0"],
        ],
    )
    bse = {row["trading_symbol"]: row for row in rows_from_firstock("BSE", bse_payload)}
    assert bse["CHASBRT"]["instrument"] == "EQ"
    assert bse["CHASBRT"]["trading_symbol"] == "CHASBRT"
    assert bse["PUSHPIN"]["instrument"] == "EQ"
    nse_payload = _csv(
        _CASH,
        [["NSE", "1", "1", "011NSETEST-EQ", "TEST", "DUMMYSAN005", "0.05", "0"]],
    )
    test_row = rows_from_firstock("NSE", nse_payload)[0]
    assert test_row["instrument"] == "EQ"
    assert test_row["trading_symbol"] == "011NSETEST-EQ"


def test_firstock_option_and_future() -> None:
    payload = _csv(
        _FO,
        [
            [
                "NFO",
                "35000",
                "30",
                "BANKNIFTY",
                "BANKNIFTY29SEP26C72600",
                "BANKNIFTY",
                "29-SEP-2026",
                "OPTIDX",
                "CE",
                "72600",
                "0.05",
                "600",
            ],
            [
                "NFO",
                "9",
                "1100",
                "NIFTYFPI",
                "NIFTYFPI29SEP26F",
                "NIFTYFPI",
                "29-SEP-2026",
                "FUTIDX",
                "FUT",
                "-0.01",
                "0.05",
                "1800",
            ],
        ],
    )
    rows = {row["token"]: row for row in rows_from_firstock("NFO", payload)}
    option = rows["35000"]
    assert option["instrument"] == "CE"
    assert option["expiry"] == "20260929"
    assert option["strike"] == "7260000"
    assert option["tick_size"] == "5"
    assert option["underlying"] == "BANKNIFTY"
    future = rows["9"]
    assert future["instrument"] == "FUT"
    assert future["expiry"] == "20260929"
    assert future["strike"] == ""


def test_firstock_fractional_strike_is_paise() -> None:
    payload = _csv(
        _FO,
        [
            [
                "NFO",
                "1",
                "1",
                "IDEA",
                "IDEA29SEP26C222.5",
                "IDEA",
                "29-SEP-2026",
                "OPTSTK",
                "CE",
                "222.5",
                "0.05",
                "1",
            ]
        ],
    )
    row = rows_from_firstock("NFO", payload)[0]
    assert row["strike"] == "22250"
    assert row["expiry"] == "20260929"


def test_indices_segment_is_not_emitted() -> None:
    payload = _csv(
        [
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
        ],
        [
            [
                "NFO",
                "1",
                "30",
                "BANKNIFTY",
                "BANKNIFTY29SEP26C72600",
                "29-SEP-2026",
                "OPTIDX",
                "CE",
                "72600",
                "0.05",
            ]
        ],
    )
    assert rows_from_firstock("Indices", payload) == []


def test_kite_equity_index_and_option() -> None:
    payload = _csv(
        _KITE,
        [
            [
                "738561",
                "3000001",
                "RELIANCE",
                "RELIANCE INDUSTRIES",
                "1234.5",
                "",
                "0",
                "0.05",
                "1",
                "EQ",
                "NSE",
                "NSE",
            ],
            [
                "256265",
                "26000",
                "NIFTY 50",
                "NIFTY 50",
                "0",
                "",
                "0",
                "0",
                "0",
                "EQ",
                "INDICES",
                "NSE",
            ],
            [
                "123",
                "456",
                "BANKNIFTY26SEP72600CE",
                "BANKNIFTY",
                "10",
                "2026-09-29",
                "72600",
                "0.05",
                "30",
                "CE",
                "NFO-OPT",
                "NFO",
            ],
        ],
    )
    rows = {row["trading_symbol"]: row for row in rows_from_kite(payload)}
    equity = rows["RELIANCE"]
    assert equity["instrument"] == "EQ"
    assert equity["token"] == "738561"
    assert equity["strike"] == ""
    assert equity["tick_size"] == "5"
    assert "1234.5" not in equity.values()
    index = rows["NIFTY 50"]
    assert index["instrument"] == "INDEX"
    assert index["trading_symbol"] == "NIFTY 50"
    colon_name = _csv(
        _KITE,
        [
            [
                "403209",
                "1",
                "BSE SENSEX SIXTY 65:35",
                "BSE SENSEX SIXTY 65:35",
                "0",
                "",
                "0",
                "0",
                "0",
                "EQ",
                "INDICES",
                "BSE",
            ]
        ],
    )
    named = rows_from_kite(colon_name)[0]
    assert named["trading_symbol"] == "BSE SENSEX SIXTY 65:35"
    assert named["instrument"] == "INDEX"
    assert index["tick_size"] == "0"
    option = rows["BANKNIFTY26SEP72600CE"]
    assert option["expiry"] == "20260929"
    assert option["strike"] == "7260000"
    assert option["token"] == "123"


def test_kite_leaves_out_currency_and_rejects_a_sub_paisa_tick() -> None:
    currency = _csv(
        _KITE,
        [
            [
                "1",
                "2",
                "USDINR26SEP963750CE",
                "USDINR",
                "1",
                "2026-09-29",
                "96.375",
                "0.0025",
                "1",
                "CE",
                "CDS-OPT",
                "CDS",
            ],
            [
                "3",
                "4",
                "RELIANCE",
                "RELIANCE",
                "1",
                "",
                "0",
                "0.05",
                "1",
                "EQ",
                "NSE",
                "NSE",
            ],
        ],
    )
    rows = rows_from_kite(currency)
    assert [row["trading_symbol"] for row in rows] == ["RELIANCE"]
    assert rows[0]["tick_size"] == "5"
    fine = _csv(
        _KITE,
        [
            [
                "1",
                "2",
                "SYM",
                "NAME",
                "1",
                "",
                "0",
                "0.0025",
                "1",
                "EQ",
                "NSE",
                "NSE",
            ]
        ],
    )
    with pytest.raises(DownloadError, match="whole number of paise"):
        rows_from_kite(fine)


def test_unknown_instrument_and_bad_option_strike_fail() -> None:
    weird = _csv(
        _KITE,
        [
            [
                "1",
                "2",
                "SYM",
                "NAME",
                "1",
                "",
                "0",
                "0.05",
                "1",
                "BOND",
                "NSE",
                "NSE",
            ]
        ],
    )
    with pytest.raises(DownloadError, match="instrument_type"):
        rows_from_kite(weird)
    bad_strike = _csv(
        _FO,
        [
            [
                "NFO",
                "1",
                "1",
                "NIFTY",
                "NIFTY",
                "NIFTY",
                "29-SEP-2026",
                "OPTIDX",
                "CE",
                "0",
                "0.05",
                "1",
            ]
        ],
    )
    with pytest.raises(DownloadError, match="option strike"):
        rows_from_firstock("NFO", bad_strike)


def test_book_sorts_and_rejects_a_duplicate_identity() -> None:
    rows = rows_from_firstock(
        "NSE",
        _csv(
            _CASH,
            [["NSE", "1", "1", "RELIANCE-EQ", "Reliance", "INE002A01018", "0.05", "0"]],
        ),
    )
    rows.extend(
        rows_from_firstock(
            "BSE",
            _csv(
                _CASH,
                [["BSE", "2", "1", "SBIN", "SBIN", "INE062A01020", "0.05", "0"]],
            ),
        )
    )
    book = build_book(rows)
    text = book["csv"].decode()
    assert text.splitlines()[0] == ",".join(COLUMNS)
    body = list(csv.DictReader(io.StringIO(text)))
    assert [row["exchange"] for row in body] == ["BSE", "NSE"]
    assert book["exchanges"] == [
        {"segment": "BSE", "rows": 1},
        {"segment": "NSE", "rows": 1},
    ]
    duplicate = rows_from_firstock(
        "NSE",
        _csv(
            _CASH,
            [
                ["NSE", "1", "1", "AAA-EQ", "A", "INE000000001", "0.05", "0"],
                ["NSE", "1", "1", "BBB-EQ", "B", "INE000000002", "0.05", "0"],
            ],
        ),
    )
    with pytest.raises(DownloadError, match="duplicate exchange,token"):
        build_book(duplicate)
