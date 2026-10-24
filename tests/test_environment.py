# tests/test_environment.py
"""
Checks that the baseline libraries are importable the way scripts really run (Python's isolated mode, which ignores environment variables
and the user's site folder). On a developer machine without them these are skipped; in CI they run inside the built image, which is the
point: they prove the Linux environment itself, not just the harness.
"""
from __future__ import annotations

import importlib.util

import pytest

from test_run_job import run

LIBRARIES = ["numpy", "pandas", "requests", "dateutil", "pytz", "yaml", "bs4", "lxml", "openpyxl", "pydantic", "jsonschema", "docx", "reportlab", "pypdf"]
MISSING = [m for m in LIBRARIES if importlib.util.find_spec(m) is None]

pytestmark = pytest.mark.skipif(bool(MISSING), reason=f"baseline libraries not installed here: {MISSING}")


@pytest.mark.parametrize("module", LIBRARIES)
def test_baseline_library_imports_in_a_script(module: str) -> None:
    out = run({"mode": "script", "code": f"import {module}\nresult = '{module}'"})
    assert out["ok"] is True and out["result"] == module


def test_a_spreadsheet_round_trip_works() -> None:
    code = (
        "import pandas as pd\n"
        "pd.DataFrame({'a': [1, 2], 'b': [3, 4]}).to_excel('t.xlsx', index=False)\n"
        "result = int(pd.read_excel('t.xlsx')['b'].sum())"
    )
    out = run({"mode": "script", "code": code})
    assert out["ok"] is True and out["result"] == 7 and "t.xlsx" in out["output_files"]
