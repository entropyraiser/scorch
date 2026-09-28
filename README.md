# Scorch

Python scripts, auxiliary tooling, and observability for the Scorch trading system.

## Setup

Install [uv](https://docs.astral.sh/uv/) if you do not already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then from the repo root:

```bash
uv sync
```

That creates a local `.venv` from `uv.lock` and installs the project (Python 3.12). Cursor / VS Code should use `.venv/bin/python`.

## Daily commands

```bash
uv sync                         # refresh the env from the lockfile
uv add httpx                    # add a runtime dependency
uv add --dev some-dev-tool      # add a dev dependency
uv run scorch download-symbols --broker firstock  # normalized Firstock instruments.csv
uv run scorch download-symbols --broker kite      # normalized Kite instruments.csv
uv run pytest
uv run ruff check .
uv run ruff format .
```

`uv run` uses the project environment; you do not need to activate `.venv` first.

## Commands

`uv run scorch download-symbols --broker firstock` fetches the public Firstock V1 symbol masters (NSE, BSE, NFO, BFO, Indices) and writes one normalized `instruments.csv` plus a `manifest.json` under `data/firstock/YYYY-MM-DD` (override with `--out`). The date is the Asia/Kolkata calendar day. A run that fails validation leaves the previous snapshot in place. Log timestamps use that same clock. The vendor files are checked in memory and are not stored.

Both brokers publish the same columns, in this order:

```text
exchange,token,trading_symbol,underlying,instrument,expiry,strike,lot_size,tick_size
```

`instrument` is `EQ`, `FUT`, `CE`, `PE`, or `INDEX`. Expiry is `YYYYMMDD`, empty on equities and indexes. Strike is empty except on options. Strike and tick size are integer paise (the vendor's rupee value times 100). Lot size stays a count. `token` is that broker's order token. `underlying` is the contract underlying on futures and options, and the trading symbol on equities and indexes. Zero lot size and zero tick size are valid. A price that is not a whole number of paise is rejected, as are non-finite numbers. `last_price` is not in this file.

Every Firstock Indices `(Exchange, Token)` must appear in NFO or in BFO, and not in both. Those rows are left out of `instruments.csv`, because they repeat those contracts. A Firstock cash row with an empty ISIN is `INDEX`; any other cash row is `EQ`.

The manifest is schema version 3. It records the sha256 of `instruments.csv`, the row count of each exchange, Indices coverage for Firstock, and how many rows have a zero lot size or zero tick size. A same-day rerun is compared with the manifest already in the output directory. The first publish of a dated directory is compared with the previous calendar day's directory when that snapshot exists. An exchange row count or an Indices overlap that moves by more than 40% fails the run. A schema 1 Firstock manifest is compared without its Indices file. A schema 1 Kite manifest is compared on the total row count. One run holds `<day>.lock` until the swap finishes, so a second run of the same directory waits.

`uv run scorch download-symbols --broker kite` fetches the public Kite Connect instrument dump. No API key or access token is sent. It writes the same `instruments.csv` and manifest under `data/kite/YYYY-MM-DD` (override with `--out`), with the same date, schema, continuity check, lock, and all-or-nothing publish. The vendor file is every exchange in one CSV (NSE, BSE, NFO, BFO, MCX, CDS, NCO, and smaller lists). Index rows arrive with segment `INDICES` and are stored as `INDEX`. Currency rows (CDS, BCD) are left out: their tick and strike are finer than one paisa. `last_price` in that dump is not a live quote and is not written.
