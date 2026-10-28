"""Test suite for the billing sample project.

Test naming drives the SDD classification used by the context manager:

* ``test_regression_*``  -> regression tests. They encode behaviour that exists
  today and must survive every change.
* any other ``test_*``   -> functional tests for the behaviour introduced by
  ``SPEC-BILL-001``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
