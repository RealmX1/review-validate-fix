#!/usr/bin/env python3
"""Contract runner for official-CLI local-deploy verifier tests."""

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
                    / "test_official_cli_local_deploy_verification.py"
                ),
            ]
        )
    )
