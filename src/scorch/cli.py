"""Command-line interface for Scorch auxiliary tools."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from scorch.firstock.symbols import download_symbols
from scorch.kite.instruments import download_instruments
from scorch.snapshot import DownloadError, exchange_zone, session_stamp
from scorch.universe import ConfigError, universe


def _downloaders() -> dict[str, object]:
    """Broker name to download function.

    Built on each call so a test can replace either function. The universe
    file chooses which of these run; it does not supply the vendor client.
    """
    return {
        "firstock": download_symbols,
        "kite": download_instruments,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scorch",
        description="Auxiliary tools for the Scorch trading system.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    configured = tuple(universe().brokers)
    known = _downloaders()
    missing = [name for name in configured if name not in known]
    if missing:
        joined = ", ".join(missing)
        raise ConfigError(f"universe broker has no downloader: {joined}")
    brokers = configured
    download = sub.add_parser(
        "download-symbols",
        help="Download a broker instrument master.",
    )
    download.add_argument(
        "--broker",
        choices=brokers,
        required=True,
        help=f"Broker to download: {', '.join(brokers)}.",
    )
    download.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Directory to write. The directory name should be YYYYMMDD.",
    )
    sub.add_parser(
        "list-brokers",
        help="Print the brokers in the universe file, one per line.",
    )
    sub.add_parser(
        "calendar-day",
        help="Print today's session date as YYYYMMDD.",
    )
    return parser


def session_time(timestamp: float) -> time.struct_time:
    """Format log timestamps in the session timezone from the universe file."""
    return datetime.fromtimestamp(timestamp, exchange_zone()).timetuple()


_STDERR_HANDLER: logging.StreamHandler | None = None


def _configure_logging() -> None:
    """Send INFO logs to stderr with session-timezone timestamps.

    `basicConfig` does nothing once another library has installed a handler,
    which would leave the CLI on the host clock. This installs one handler
    and keeps its clock on the session timezone.
    """
    global _STDERR_HANDLER
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    formatter.converter = session_time
    root = logging.getLogger()
    if _STDERR_HANDLER is None or _STDERR_HANDLER.stream is not sys.stderr:
        if _STDERR_HANDLER is not None:
            root.removeHandler(_STDERR_HANDLER)
        _STDERR_HANDLER = logging.StreamHandler(sys.stderr)
        root.addHandler(_STDERR_HANDLER)
    _STDERR_HANDLER.setFormatter(formatter)
    root.setLevel(logging.INFO)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    try:
        parser = build_parser()
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    args = parser.parse_args(argv)
    if args.command == "list-brokers":
        for name in universe().brokers:
            print(name)
        return 0
    if args.command == "calendar-day":
        print(session_stamp())
        return 0
    if args.command == "download-symbols":
        download = _downloaders()[args.broker]
        try:
            out_dir = download(args.out)
        except (DownloadError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"wrote files under {out_dir}")
        return 0
    parser.error(f"unknown command: {args.command}")
