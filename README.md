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
uv run scorch download-symbols --broker firstock --out /var/lib/instruments/firstock/YYYYMMDD
uv run scorch download-symbols --broker kite --out /var/lib/instruments/kite/YYYYMMDD
uv run pytest
uv run ruff check .
uv run ruff format .
```

`uv run` uses the project environment; you do not need to activate `.venv` first.

## Commands

`uv run scorch download-symbols --broker firstock --out <directory>` fetches the public Firstock V1 symbol masters named for that broker in `config/universe.toml` (NSE, BSE, NFO, BFO, Indices) and writes one normalized `instruments.csv` plus a `manifest.json` in that directory. `--out` is required. Scorch has no default directory. Name the directory `YYYYMMDD` for the Asia/Kolkata calendar day so a rerun can compare it with the previous day's sibling. A run that fails validation leaves the previous snapshot in place. Log timestamps use that same clock. The vendor files are checked in memory and are not stored.

## Daily download

`scripts/download-instruments` is the command the morning job runs. It takes the output directory, reads today's session date and the broker list from `config/universe.toml`, and downloads each broker:

```bash
scripts/download-instruments /var/lib/instruments
```

That writes:

```text
/var/lib/instruments/firstock/YYYYMMDD/instruments.csv
/var/lib/instruments/kite/YYYYMMDD/instruments.csv
```

A failed broker leaves that broker's previous snapshot in place. The script still tries the other broker. Both runs are appended to `/var/lib/instruments/download.log`.

`deploy/cron.d/scorch-instruments` is a copy of that job kept in the repo so it can be installed again. Cron does not read the `deploy/` folder. Install it into your user crontab:

```bash
crontab deploy/cron.d/scorch-instruments
```

`crontab -l` then shows the job. It runs at 08:00 Asia/Kolkata and passes `/var/lib/instruments` to the script. `CRON_TZ` matches `timezone` in `config/universe.toml`.

## Universe

`config/universe.toml` is the book this download is willing to write, and the limits on a short or drifting file. The morning job sets `SCORCH_UNIVERSE` to that path. A checkout also finds the file by walking up from the package.

The file holds the exchanges, and for each derivative the cash exchange that holds its spot plus an optional second cash exchange when the home listing is absent. It holds the equity series codes and the index display names that fold onto one underlying. Per broker, it holds the underlyings that may be dropped when that broker publishes the derivative and not the spot (Firstock lists `FOCIT`; Kite lists none), and either one file with a minimum row count (Kite) or the files to fetch (Firstock). Firstock's Indices file is fetched and checked, and `publish = false` keeps it out of `instruments.csv`. The continuity limit (40%) and the old segment names that are no longer compared live here too, with the shared HTTP timeout and retries.

A broker that does not publish an exchange has no rows for it. Listing an exchange does not require every broker to have it. Vendor headers and instrument-type maps stay in the broker modules. The `instruments.csv` columns and the `key` format stay in code.

Both brokers publish the same columns, in this order:

```text
key,exchange,token,trading_symbol,kind,underlying,expiry,strike,lot_size,tick_size,spot_key
```

The file is UTF-8, LF, one header, and plain commas. Nothing is quoted. `trading_symbol` is the broker's own symbol and may contain spaces; no other field may. The C++ process reads the file as-is. A failed check writes nothing and leaves the previous snapshot in place. The publish writes a temp file and renames it into place.

`kind` is `EQ`, `IDX`, `FUT`, `CE`, or `PE`. Expiry is `YYYYMMDD`, or `0` when there is none. Strike is integer paise, or `0` except on options. Tick size is integer paise (the vendor's rupee value times 100). Lot size and token are integers, `0` when the vendor has no value. Index rows are not tradable, so their lot size is `0`; the contract lot stays on the future and the option. `spot_key` is the `key` of the spot instrument for a derivative and empty for equities and indexes. That key is always another row in the same file.

`key` is broker-neutral, uppercase, and has no spaces. The same contract has the same key in both files. An equity key carries the series (`NSE_RELIANCE_EQ`); the script supplies `EQ` when the vendor symbol has no series suffix, and it does not treat a hyphen inside the symbol (`BAJAJ-AUTO`) as a series. An index key uses the derivative underlying (`NSE_NIFTY_IDX`), so Kite's `NIFTY 50` and Firstock's `NIFTY` are one index. A future is `NFO_INFY_20261123_FUT`. An option is `NFO_RBLBANK_20261229_CE_43000`, with the strike in paise. Spaces are stripped from the symbols that go into `key` and `underlying`.

The exchanges that are written are the ones in `config/universe.toml`. That file lists NSE, BSE, NFO, and BFO. Rows are sorted by `key`. The run fails on a duplicate `key`, a duplicate `(exchange, token)`, or a derivative whose spot is not in the file. NFO contracts point at NSE. BFO contracts point at BSE, or at NSE when that stock is not listed on BSE. Firstock publishes FOCIT derivatives and does not publish the BSE Focused IT index, so those rows are left out and listed in the manifest as `omitted_spots`. Kite publishes the index, so its FOCIT rows stay.

Every Firstock Indices `(Exchange, Token)` must appear in NFO or in BFO, and not in both. Those rows are left out of `instruments.csv`, because they repeat those contracts. A Firstock cash row with an empty ISIN is `IDX`; any other cash row is `EQ`.

The manifest is schema version 5. It records the sha256 of `instruments.csv`, the row count of each exchange, Indices coverage for Firstock, how many rows have a zero lot size or zero tick size, and any derivatives left out for lack of a spot. A same-day rerun is compared with the manifest already in the output directory. The first publish of a dated directory is compared with the previous calendar day's directory when that snapshot exists. An exchange row count or an Indices overlap that moves by more than the continuity limit in `config/universe.toml` fails the run. That limit is 40%. A schema 1 Firstock manifest is compared without its Indices file. A schema 1 Kite manifest is compared on the total row count. Segments named there as retired (Indices, and Kite's MCX, NCO, CDS, BCD, GLOBAL, NSEIX) are not part of that comparison. One run holds `<day>.lock` until the swap finishes, so a second run of the same directory waits.

`uv run scorch download-symbols --broker kite` fetches the public Kite Connect instrument dump. No API key or access token is sent. It writes the same `instruments.csv` and manifest into the required `--out` directory, with the same schema, continuity check, lock, and all-or-nothing publish. The vendor file is every exchange in one CSV. Index rows arrive with segment `INDICES` and are stored as `IDX`. Exchanges other than the ones in `config/universe.toml` are left out. `last_price` in that dump is not a live quote and is not written.
