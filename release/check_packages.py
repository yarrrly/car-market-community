"""Validate source distributions and installed-code wheels against an exact Git tree."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    pin = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True)
    if dirty:
        raise ValueError("Build and check a clean, frozen checkout")
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", pin], text=True
    ).splitlines()
    expected = {name: Path(name).read_bytes() for name in files}
    for name, body in expected.items():
        path = PurePosixPath(name)
        assert not path.is_absolute() and ".." not in path.parts, name
        assert not path.parts[0] in {
            "workspace",
            "reports",
            "exports",
            "backups",
            "dist",
            ".venv",
        }, name
        assert path.name not in {".env", ".DS_Store"} and not path.name.startswith(
            ".env."
        ), name
        assert not name.endswith(
            (".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log")
        ), name
        # Report paths only, never print a possible secret value.
        assert not re.search(
            rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|ghp_[A-Za-z0-9]{36}\b|github_pat_[A-Za-z0-9_]{70,}",
            body,
        ), name
    dist = Path(args.dist)
    wheels = list(dist.glob("*.whl"))
    sources = list(dist.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1
    with tarfile.open(sources[0]) as archive:
        source_files = {}
        for entry in archive:
            path = PurePosixPath(entry.name)
            assert not path.is_absolute() and ".." not in path.parts
            assert not entry.issym() and not entry.islnk()
            if entry.isfile():
                source_files["/".join(path.parts[1:])] = archive.extractfile(
                    entry
                ).read()
    assert not set(expected) - set(source_files), sorted(
        set(expected) - set(source_files)
    )
    assert all(source_files[name] == body for name, body in expected.items())
    # The existing Verify workflow invokes this gate after uv build. Never
    # project or mutate fixture bytes during validation: inspect both delivered
    # channels and fail before a package can be accepted.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.check_public_source_privacy import (
        PrivacyHold,
        check_source_and_sdist,
        read_sdist,
    )

    privacy_policy = json.loads(expected["tools/public-source-privacy-policy.json"])
    try:
        privacy = check_source_and_sdist(
            expected, read_sdist(sources[0]), privacy_policy
        )
    except PrivacyHold as error:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(error.receipt, indent=2) + "\n")
        raise
    runtime = {
        name: body for name, body in expected.items() if name.startswith("car_market/")
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        assert archive.testzip() is None
        wheel_names = archive.namelist()
        assert all(
            not PurePosixPath(name).is_absolute()
            and ".." not in PurePosixPath(name).parts
            for name in wheel_names
        )
        wheel_runtime = {
            name: archive.read(name)
            for name in wheel_names
            if name.startswith("car_market/")
        }
        assert wheel_runtime == runtime
        assert not any(
            name.startswith(
                ("tests/", "fixtures/", "docs/", "workspace/", "tools/", "release/")
            )
            for name in wheel_names
        )
        metadata = [
            name for name in wheel_names if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata) == 1
        text = archive.read(metadata[0]).decode()
        assert ("\nVersion: " + version + "\n") in text
        assert ("car-market = car_market.portable:main") in archive.read(
            metadata[0].rsplit("/", 1)[0] + "/entry_points.txt"
        ).decode()
        licenses = [name for name in wheel_names if name.endswith("/LICENSE")]
        assert any(archive.read(name) == expected["LICENSE"] for name in licenses)
        assert "car_market/assets/fonts/OFL.txt" in wheel_names
    result = {
        "status": "PASS",
        "source_pin": pin,
        "version": version,
        "tracked_source_files": len(expected),
        "wheel_runtime_assets": len(runtime),
        "privacy": {
            key: {k: value[k] for k in ("status", "members", "gzip_full_crc_checked")}
            for key, value in privacy.items()
        },
        "artifacts": {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in [wheels[0], sources[0]]
        },
        "scope": "Exact tracked bytes, traversal/state/key-shape guards, entrypoint/version and license/font contents. Not an all-history secret audit or live market certificate.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
