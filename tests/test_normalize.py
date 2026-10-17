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
    assert equity["kind"] == "EQ"
    assert equity["key"] == "NSE_RELIANCE_EQ"
    assert equity["underlying"] == "RELIANCE"
    assert equity["expiry"] == "0"
    assert equity["strike"] == "0"
    assert equity["spot_key"] == ""
    assert equity["tick_size"] == "5"
    nifty = by_symbol["NIFTY"]
    assert nifty["kind"] == "IDX"
    assert nifty["key"] == "NSE_NIFTY_IDX"
    assert nifty["underlying"] == "NIFTY"
    assert nifty["lot_size"] == "0"
    assert nifty["tick_size"] == "0"
    spaced = by_symbol["NIFTY 100"]
    assert spaced["key"] == "NSE_NIFTY100_IDX"
    assert spaced["underlying"] == "NIFTY100"
    assert spaced["trading_symbol"] == "NIFTY 100"
    assert spaced["lot_size"] == "0"
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
    assert bse["CHASBRT"]["kind"] == "EQ"
    assert bse["CHASBRT"]["key"] == "BSE_CHASBRT_EQ"
    assert bse["CHASBRT"]["trading_symbol"] == "CHASBRT"
    assert bse["PUSHPIN"]["kind"] == "EQ"
    nse_payload = _csv(
        _CASH,
        [["NSE", "1", "1", "011NSETEST-EQ", "TEST", "DUMMYSAN005", "0.05", "0"]],
    )
    test_row = rows_from_firstock("NSE", nse_payload)[0]
    assert test_row["kind"] == "EQ"
    assert test_row["key"] == "NSE_011NSETEST_EQ"
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
    assert option["kind"] == "CE"
    assert option["key"] == "NFO_BANKNIFTY_20260929_CE_7260000"
    assert option["expiry"] == "20260929"
    assert option["strike"] == "7260000"
    assert option["tick_size"] == "5"
    assert option["underlying"] == "BANKNIFTY"
    future = rows["9"]
    assert future["kind"] == "FUT"
    assert future["key"] == "NFO_NIFTYFPI_20260929_FUT"
    assert future["expiry"] == "20260929"
    assert future["strike"] == "0"


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
    assert equity["kind"] == "EQ"
    assert equity["key"] == "NSE_RELIANCE_EQ"
    assert equity["token"] == "738561"
    assert equity["strike"] == "0"
    assert equity["tick_size"] == "5"
    assert "1234.5" not in equity.values()
    index = rows["NIFTY 50"]
    assert index["kind"] == "IDX"
    assert index["key"] == "NSE_NIFTY_IDX"
    assert index["underlying"] == "NIFTY"
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
    assert named["kind"] == "IDX"
    assert named["underlying"] == "BSESENSEXSIXTY65:35"
    assert named["key"] == "BSE_BSESENSEXSIXTY65:35_IDX"
    assert index["tick_size"] == "0"
    option = rows["BANKNIFTY26SEP72600CE"]
    assert option["expiry"] == "20260929"
    assert option["strike"] == "7260000"
    assert option["key"] == "NFO_BANKNIFTY_20260929_CE_7260000"
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
    assert [row["key"] for row in body] == ["BSE_SBIN_EQ", "NSE_RELIANCE_EQ"]
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


def test_series_split_keeps_a_hyphen_inside_the_symbol() -> None:
    payload = _csv(
        _CASH,
        [
            [
                "NSE",
                "1",
                "1",
                "BAJAJ-AUTO-EQ",
                "Bajaj Auto",
                "INE917I01010",
                "0.05",
                "0",
            ],
            ["NSE", "2", "1", "GATECHDVR-BE", "GACM", "INE000000099", "0.05", "0"],
            ["NSE", "3", "1", "705PFCL41-nan", "PFCL", "INE134E07DS4", "0.01", "0"],
        ],
    )
    rows = {row["trading_symbol"]: row for row in rows_from_firstock("NSE", payload)}
    assert rows["BAJAJ-AUTO-EQ"]["key"] == "NSE_BAJAJ-AUTO_EQ"
    assert rows["BAJAJ-AUTO-EQ"]["underlying"] == "BAJAJ-AUTO"
    assert rows["GATECHDVR-BE"]["key"] == "NSE_GATECHDVR_BE"
    assert rows["705PFCL41-nan"]["key"] == "NSE_705PFCL41_NA"


def test_kite_equity_series_matches_firstock() -> None:
    kite = rows_from_kite(
        _csv(
            _KITE,
            [
                [
                    "1",
                    "2",
                    "BAJAJ-AUTO",
                    "BAJAJ AUTO",
                    "1",
                    "",
                    "0",
                    "0.05",
                    "1",
                    "EQ",
                    "NSE",
                    "NSE",
                ],
                [
                    "3",
                    "4",
                    "GATECHDVR-BE",
                    "GACM",
                    "1",
                    "",
                    "0",
                    "0.05",
                    "1",
                    "EQ",
                    "NSE",
                    "NSE",
                ],
                [
                    "5",
                    "6",
                    "705PFCL41-NA",
                    "PFCL",
                    "1",
                    "",
                    "0",
                    "0.01",
                    "1",
                    "EQ",
                    "NSE",
                    "NSE",
                ],
            ],
        )
    )
    assert [row["key"] for row in kite] == [
        "NSE_BAJAJ-AUTO_EQ",
        "NSE_GATECHDVR_BE",
        "NSE_705PFCL41_NA",
    ]


def test_spot_key_links_the_derivative_to_the_cash_row() -> None:
    equity = rows_from_firstock(
        "NSE",
        _csv(
            _CASH,
            [
                [
                    "NSE",
                    "408065",
                    "1",
                    "INFY-EQ",
                    "Infosys",
                    "INE009A01021",
                    "0.05",
                    "0",
                ],
                ["NSE", "26000", "65", "NIFTY", "Nifty 50", "", "0", "0"],
            ],
        ),
    )
    derivatives = rows_from_firstock(
        "NFO",
        _csv(
            _FO,
            [
                [
                    "NFO",
                    "15771650",
                    "400",
                    "INFY",
                    "INFY26NOVFUT",
                    "INFY",
                    "23-NOV-2026",
                    "FUTSTK",
                    "FUT",
                    "0",
                    "0.05",
                    "0",
                ],
                [
                    "NFO",
                    "36864258",
                    "3175",
                    "RBLBANK",
                    "RBLBANK26DEC430CE",
                    "RBLBANK",
                    "29-DEC-2026",
                    "OPTSTK",
                    "CE",
                    "430",
                    "0.05",
                    "0",
                ],
                [
                    "NFO",
                    "9",
                    "65",
                    "NIFTY",
                    "NIFTY26NOVFUT",
                    "NIFTY",
                    "24-NOV-2026",
                    "FUTIDX",
                    "FUT",
                    "0",
                    "0.05",
                    "0",
                ],
            ],
        ),
    )
    rbl = rows_from_firstock(
        "NSE",
        _csv(
            _CASH,
            [["NSE", "7", "1", "RBLBANK-EQ", "RBL", "INE976G01028", "0.05", "0"]],
        ),
    )
    # INFY is also on BSE. The NFO contract still points at the NSE equity.
    bse = rows_from_firstock(
        "BSE",
        _csv(
            _CASH, [["BSE", "8", "1", "INFY", "Infosys", "INE009A01021", "0.05", "0"]]
        ),
    )
    book = build_book([*equity, *derivatives, *rbl, *bse])
    text = book["csv"].decode()
    assert '"' not in text
    assert "\r" not in text
    assert text.endswith("\n")
    assert text.splitlines()[0] == ",".join(COLUMNS)
    body = list(csv.DictReader(io.StringIO(text)))
    by_key = {row["key"]: row for row in body}
    assert by_key["NFO_INFY_20261123_FUT"]["spot_key"] == "NSE_INFY_EQ"
    assert by_key["NFO_RBLBANK_20261229_CE_43000"]["spot_key"] == "NSE_RBLBANK_EQ"
    assert by_key["NFO_RBLBANK_20261229_CE_43000"]["strike"] == "43000"
    assert by_key["NFO_NIFTY_20261124_FUT"]["spot_key"] == "NSE_NIFTY_IDX"
    assert by_key["NSE_INFY_EQ"]["spot_key"] == ""
    assert by_key["NSE_NIFTY_IDX"]["trading_symbol"] == "NIFTY"
    assert [row["key"] for row in body] == sorted(row["key"] for row in body)
    assert book["omitted_spots"] == []


def test_kite_index_alias_is_the_derivative_underlying() -> None:
    rows = rows_from_kite(
        _csv(
            _KITE,
            [
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
                    "260105",
                    "1016",
                    "NIFTY BANK",
                    "NIFTY BANK",
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
                    "11",
                    "12",
                    "INFY26NOVFUT",
                    "INFY",
                    "0",
                    "2026-11-23",
                    "0",
                    "0.05",
                    "400",
                    "FUT",
                    "NFO-FUT",
                    "NFO",
                ],
                [
                    "13",
                    "14",
                    "NIFTY26NOVFUT",
                    "NIFTY",
                    "0",
                    "2026-11-24",
                    "0",
                    "0.05",
                    "65",
                    "FUT",
                    "NFO-FUT",
                    "NFO",
                ],
            ],
        )
    )
    # INFY has no equity in this dump, so the book cannot be published.
    with pytest.raises(DownloadError, match="no spot instrument"):
        build_book(rows)
    equity = rows_from_kite(
        _csv(
            _KITE,
            [
                [
                    "408065",
                    "1",
                    "INFY",
                    "INFOSYS",
                    "1",
                    "",
                    "0",
                    "0.05",
                    "1",
                    "EQ",
                    "NSE",
                    "NSE",
                ]
            ],
        )
    )
    book = build_book([*rows, *equity])
    body = list(csv.DictReader(io.StringIO(book["csv"].decode())))
    by_key = {row["key"]: row for row in body}
    assert by_key["NSE_NIFTY_IDX"]["trading_symbol"] == "NIFTY 50"
    assert by_key["NSE_NIFTY_IDX"]["underlying"] == "NIFTY"
    assert by_key["NSE_BANKNIFTY_IDX"]["trading_symbol"] == "NIFTY BANK"
    assert by_key["NFO_NIFTY_20261124_FUT"]["spot_key"] == "NSE_NIFTY_IDX"
    assert by_key["NFO_INFY_20261123_FUT"]["spot_key"] == "NSE_INFY_EQ"
    nifty_line = next(
        line
        for line in book["csv"].decode().splitlines()
        if line.startswith("NSE_NIFTY_IDX,")
    )
    assert nifty_line == "NSE_NIFTY_IDX,NSE,256265,NIFTY 50,IDX,NIFTY,0,0,0,0,"
    assert '"' not in book["csv"].decode()


def test_bfo_stock_uses_the_nse_equity_when_bse_has_none() -> None:
    equity = rows_from_firstock(
        "NSE",
        _csv(
            _CASH, [["NSE", "1", "1", "CDSL-EQ", "CDSL", "INE736A01011", "0.05", "0"]]
        ),
    )
    derivative = rows_from_firstock(
        "BFO",
        _csv(
            _FO,
            [
                [
                    "BFO",
                    "9",
                    "325",
                    "CDSL",
                    "CDSL26NOVFUT",
                    "CDSL",
                    "24-NOV-2026",
                    "FUTSTK",
                    "FUT",
                    "0",
                    "0.05",
                    "100",
                ]
            ],
        ),
    )
    book = build_book([*equity, *derivative])
    body = list(csv.DictReader(io.StringIO(book["csv"].decode())))
    future = next(row for row in body if row["kind"] == "FUT")
    assert future["spot_key"] == "NSE_CDSL_EQ"


def test_focit_without_a_published_index_is_left_out() -> None:
    equity = rows_from_firstock(
        "BSE",
        _csv(_CASH, [["BSE", "1", "1", "SENSEX", "Sensex", "", "0", "0"]]),
    )
    derivatives = rows_from_firstock(
        "BFO",
        _csv(
            _FO,
            [
                [
                    "BFO",
                    "2",
                    "20",
                    "SENSEX",
                    "SENSEX26NOVFUT",
                    "SENSEX",
                    "24-NOV-2026",
                    "FUTIDX",
                    "FUT",
                    "0",
                    "0.05",
                    "500",
                ],
                [
                    "BFO",
                    "3",
                    "45",
                    "FOCIT",
                    "FOCIT26NOVFUT",
                    "FOCIT",
                    "24-NOV-2026",
                    "FUTIDX",
                    "FUT",
                    "0",
                    "0.05",
                    "500",
                ],
            ],
        ),
    )
    book = build_book([*equity, *derivatives], omit_without_spot={"FOCIT"})
    keys = [row["key"] for row in csv.DictReader(io.StringIO(book["csv"].decode()))]
    assert "BFO_SENSEX_20261124_FUT" in keys
    assert not any("FOCIT" in key for key in keys)
    assert book["omitted_spots"] == [{"underlying": "FOCIT", "rows": 1}]
    with pytest.raises(DownloadError, match="no spot instrument"):
        build_book([*equity, *derivatives], omit_without_spot=())


def test_duplicate_key_fails_and_writes_nothing() -> None:
    rows = rows_from_firstock(
        "NSE",
        _csv(
            _CASH,
            [
                ["NSE", "1", "0", "NIFTY 100", "Nifty", "", "0", "0"],
                ["NSE", "2", "0", "NIFTY100", "Nifty", "", "0", "0"],
            ],
        ),
    )
    with pytest.raises(DownloadError, match="duplicate key NSE_NIFTY100_IDX"):
        build_book(rows)


def test_fractional_lot_is_rejected() -> None:
    payload = _csv(
        _CASH,
        [["NSE", "1", "1.5", "INFY-EQ", "Infosys", "INE009A01021", "0.05", "0"]],
    )
    with pytest.raises(DownloadError, match="not a whole number"):
        rows_from_firstock("NSE", payload)
