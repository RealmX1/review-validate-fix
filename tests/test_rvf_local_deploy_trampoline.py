#!/usr/bin/env python3
"""Contract runner for local-main source-gate and deploy-runner tests."""

from __future__ import annotations

from pathlib import Path

import pytest


if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [
                "-q",
                str(
                    Path(__file__).resolve().parent
                    / "test_developer_local_main_installation.py"
                ),
            ]
        )
    )
