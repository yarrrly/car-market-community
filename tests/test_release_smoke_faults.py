"""Disposable-volume cleanup still runs when its proof disk is full."""

import errno
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_receipt_enospc_preserves_primary_error_and_attempts_owned_cleanup(
    tmp_path, monkeypatch, capsys, cleanup_failure
):
    root = Path(__file__).resolve().parents[1]
    smoke = runpy.run_path(str(root / "release/smoke_image.py"))["main"]
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--image",
            "fixture:image",
            "--architecture",
            "amd64",
            "--expected-version",
            "0.3.0rc1",
            "--output",
            str(tmp_path / "proof"),
        ],
    )
    calls = []
    created = []

    def docker(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            body = json.dumps([{"Architecture": "amd64", "Id": "controlled-image"}])
        elif argv[:3] == ["docker", "volume", "create"]:
            created.append(argv[-1])
            body = argv[-1] + "\n"
        elif argv[:3] == ["docker", "volume", "inspect"]:
            assert argv[-1] == created[0]
            if cleanup_failure:
                raise OSError("cleanup transport unavailable")
            body = json.dumps([{"Labels": {"car-market.release-smoke": "owned"}}])
        else:
            assert argv == ["docker", "volume", "rm", created[0]]
            body = ""
        return SimpleNamespace(returncode=0, stdout=body, stderr="")

    monkeypatch.setattr(smoke.__globals__["subprocess"], "run", docker)
    original = Path.write_text
    fault = OSError(errno.ENOSPC, "proof disk full")

    def write(path, *args, **kwargs):
        if path.name == "volume.stdout":
            raise fault
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(OSError) as failure:
        smoke()
    assert failure.value is fault
    assert (
        calls[-1][:3] == ["docker", "volume", "inspect"]
        if cleanup_failure
        else calls[-1] == ["docker", "volume", "rm", created[0]]
    )
    assert not (tmp_path / "proof/RESULT.json").exists()
    if cleanup_failure:
        assert "cleanup transport unavailable" in capsys.readouterr().err
