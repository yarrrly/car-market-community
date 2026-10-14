"""The real SQLite snapshot reader must not invent extra export artifacts."""

import ast
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_installed_probe_counts_export_artifacts_before_opening_snapshot(tmp_path):
    source = Path(__file__).resolve().parents[1] / "release/smoke_image.py"
    programs = [
        node.value
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "'snapshot':list(snap.iterdump())" in node.value
    ]
    assert len(programs) == 1
    root = tmp_path / "workspace with spaces"
    (root / "data").mkdir(parents=True)
    compact = root / "exports/compact"
    compact.mkdir(parents=True)
    for n in range(10):
        (compact / ("export-" + str(n) + ".csv")).write_text("synthetic fixture\n")
    with sqlite3.connect(root / "data/market.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE retained(id INTEGER PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO retained VALUES (1,'unchanged fixture')")
        db.commit()
        with sqlite3.connect(compact / "market.sqlite3") as snapshot:
            db.backup(snapshot)
    names = sorted(p.name for p in compact.iterdir())
    assert len(names) == 11
    archive_sha = hashlib.sha256((compact / "market.sqlite3").read_bytes()).hexdigest()

    class Workspace(ast.NodeTransformer):
        def visit_Constant(self, node):
            if node.value == "/workspace":
                return ast.copy_location(ast.Constant(str(root)), node)
            return node

    program = ast.unparse(Workspace().visit(ast.parse(programs[0])))
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    proof = json.loads(result.stdout)
    assert proof["compact"] == names
    assert proof["sql"] == proof["snapshot"]
    assert sorted(p.name for p in compact.iterdir()) == names
    assert (
        hashlib.sha256((compact / "market.sqlite3").read_bytes()).hexdigest()
        == archive_sha
    )
