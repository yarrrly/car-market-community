"""The measurement harness must distinguish query counts from HTTP statuses."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "live_acceptance", Path(__file__).parents[1] / "tools/live_acceptance.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize("error,statuses,denied", [
    ("count_mismatch:403/1429", {"200": 15}, False),
    ("page_limit_exceeded; count_mismatch:90/2403", {"200": 3}, False),
    ("page_2:http_403", {"200": 1, "403": 1}, True),
    ("HTTP Error 429: rate limit", {"429": 1}, True),
    ("", {"200": 15, "403": 1}, True),
    ("captcha required", {"200": 1}, True),
])
def test_only_access_evidence_stops_subsequent_cases(error, statuses, denied):
    assert bool(MODULE.access_denied(error, statuses)) is denied
