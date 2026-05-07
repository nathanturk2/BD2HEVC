#!/usr/bin/env python3
"""Compatibility wrapper for older imports and scripts."""

from bd2hevc_app.core import *  # noqa: F401,F403
from bd2hevc_app.core import main


if __name__ == "__main__":
    raise SystemExit(main())
