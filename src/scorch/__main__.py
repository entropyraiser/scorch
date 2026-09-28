"""Allow `python -m scorch`."""

from __future__ import annotations

import sys

from scorch.cli import main

if __name__ == "__main__":
    sys.exit(main())
