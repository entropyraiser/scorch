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
from scorch.snapshot import EXCHANGE_TZ, DownloadError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scorch",
        description="Auxiliary tools for the Scorch trading system.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser(
        "download-symbols",
        help="Download a broker instrument master (Firstock or Kite).",
    )
    download.add_argument(
        "--broker",
        choices=("firstock", "kite"),
        required=True,
        help="Broker to download: firstock or kite.",
    )
    download.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: data/<broker>/<Asia/Kolkata date>).",
    )
    return parser


def kolkata_time(timestamp: float) -> time.struct_time:
    """Format log timestamps on the exchange clock, not the host clock."""
    return datetime.fromtimestamp(timestamp, EXCHANGE_TZ).timetuple()


_STDERR_HANDLER: logging.StreamHandler | None = None


def _configure_logging() -> None:
    """Send INFO logs to stderr with Asia/Kolkata timestamps.

    `basicConfig` does nothing once another library has installed a handler,
    which would leave the CLI on the host clock. This installs one handler
    and keeps its clock on the exchange timezone.
    """
    global _STDERR_HANDLER
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    formatter.converter = kolkata_time
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
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "download-symbols":
        if args.broker == "kite":
            download = download_instruments
        elif args.broker == "firstock":
            download = download_symbols
        else:
            parser.error(f"unknown broker: {args.broker}")
        try:
            out_dir = download(args.out)
        except (DownloadError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"wrote files under {out_dir}")
        return 0
    parser.error(f"unknown command: {args.command}")
