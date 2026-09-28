"""Numeric checks for instrument-master columns.

Zero is a real value in these files: NIFTY, BANKNIFTY, and SENSEX use tick
size 0, and several spot indices use lot size 0. NaN and infinity are not.
"""

from __future__ import annotations

import math

from scorch.snapshot import DownloadError


def parse_finite(value: str, label: str) -> float:
    """Parse `value` as a finite float, or raise `DownloadError`."""
    try:
        number = float(value)
    except ValueError as error:
        raise DownloadError(f"{label} is not numeric: {value!r}") from error
    if not math.isfinite(number):
        raise DownloadError(f"{label} is not finite: {value!r}")
    return number
