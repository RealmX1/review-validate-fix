#!/usr/bin/env python3
"""Contract runner for the official-CLI developer installation test slices."""

from __future__ import annotations

from pathlib import Path

import pytest


if __name__ == "__main__":
    tests_root = Path(__file__).resolve().parent
    raise SystemExit(
        pytest.main(
            [
                "-q",
                str(tests_root / "test_official_cli_developer_plugin_installation.py"),
                str(tests_root / "test_official_cli_installation_hooks.py"),
                str(tests_root / "test_rvf_user_local_data_paths.py"),
            ]
        )
    )
