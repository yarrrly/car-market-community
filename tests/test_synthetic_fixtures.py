"""Every regression fixture is a listed synthetic parser case with its current hash."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
REGISTRY = ROOT / "tests/fixtures/SYNTHETIC.json"


def tracked_fixtures():
    files = set()
    for base in ("tests/fixtures", "fixtures"):
        for path in (ROOT / base).rglob("*"):
            if path.is_file() and path != REGISTRY and "__pycache__" not in path.parts:
                files.add(path.relative_to(ROOT).as_posix())
    return files


def test_registry_lists_every_fixture_with_its_current_hash():
    registry = json.loads(REGISTRY.read_text())
    assert registry["policy"] == "docs/fixture-policy.md"
    listed = registry["files"]
    assert set(listed) == tracked_fixtures()
    for name, digest in listed.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
