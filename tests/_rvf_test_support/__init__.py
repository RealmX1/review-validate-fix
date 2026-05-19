"""Shared test-support helpers for the RVF test suite.

Underscore-prefixed so pytest does not collect it as a test module. The
package lives under ``tests/`` and is imported as the top-level
``_rvf_test_support`` because the test entrypoints (``python3 tests/x.py``
and pytest's prepend import mode) both put ``tests/`` on ``sys.path``.
"""

from _rvf_test_support.loader import load_script_module
from _rvf_test_support.repo import templated_repo

__all__ = ["load_script_module", "templated_repo"]
