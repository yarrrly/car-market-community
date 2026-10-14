"""Synthetic fail-closed controls; no captured contact values."""

import gzip
import io
import json
import tarfile

import pytest

from tools.check_public_source_privacy import (
    PrivacyHold,
    check_members,
    check_source_and_sdist,
    digest,
    read_sdist,
    scan_member,
)


def test_source_and_sdist_both_inspected_without_mutations():
    body = b'{ "@type": "Car", "id": "06000000000", "price": 19800, "year": 2019, "mileage": 0, "image":"image@2x.png" }'
    expected = {"fixtures/own.json.gz": gzip.compress(body, mtime=0)}
    source = dict(expected, **{"PKG-INFO": b"Name: car-market\n"})
    before = dict(source)
    result = check_source_and_sdist(expected, source, {})
    assert all(
        v["status"] == "PASS_SCOPED_CONTACT_CONTAINMENT" for v in result.values()
    )
    assert result["tracked_source"]["gzip_full_crc_checked"] == 1
    assert result["built_sdist"]["gzip_full_crc_checked"] == 1
    assert source == before


@pytest.mark.parametrize(
    "text",
    [
        "<script>self.__next_f.push([1,"
        + json.dumps('1:{"description":"Bel 06000000000"}')
        + "])</script>",
        "<p>Telefon +49&#32;0000000000</p>",
        "<p>Telefon +49\u200b0000000000</p>",
        "<p>Telefon +49‑0000000000</p>",
        '{"description":"Telefon +49\\u00200000000000"}',
        '{"original_path":"/Users/example/private/raw.html"}',
    ],
)
def test_embedded_normalized_contact_or_private_path_holds_without_logging_value(text):
    row = scan_member(
        "fixtures/control.html.gz", gzip.compress(text.encode(), mtime=0), {}
    )
    assert row["categories"]
    output = json.dumps(row)
    assert "0000000000" not in output and "/Users/example" not in output
    assert "Telefon" not in output


def test_sdist_generated_metadata_is_not_implicitly_exempt_from_private_paths():
    expected = {"LICENSE": b"License"}
    with pytest.raises(PrivacyHold) as error:
        check_source_and_sdist(
            expected,
            dict(expected, **{"PKG-INFO": b"Home: /Users/example/private/file\n"}),
            {},
        )
    assert (
        error.value.receipt["tracked_source"]["status"]
        == "PASS_SCOPED_CONTACT_CONTAINMENT"
    )
    assert error.value.receipt["built_sdist"]["status"] == "HOLD"


def test_corrupt_gzip_and_unknown_binary_hold():
    assert scan_member("fixtures/bad.gz", b"not gzip", {})["categories"] == {
        "invalid_gzip": 1
    }
    assert scan_member("docs/unknown.bin", b"\xff\x00", {})["categories"] == {
        "unreviewed_binary": 1
    }


def test_legacy_whole_file_exception_never_authorizes_a_private_path():
    name, body = "tests/control.py", b'path = "/Users/example/private/file"\n'
    row = scan_member(name, body, {})
    policy = {
        "reviewed_text_exceptions": {
            name: {
                "decoded_sha256": digest(body),
                "categories": row["categories"],
                "reason": "Synthetic nondialable/path control",
            }
        }
    }
    assert scan_member(name, body, policy)["categories"]
    assert scan_member(name, body + b"changed", policy)["categories"]


@pytest.mark.parametrize("kind", ["duplicate", "link", "traversal", "second_root"])
def test_sdist_member_guard(tmp_path, kind):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:

        def add(name):
            entry = tarfile.TarInfo(name)
            entry.size = 2
            archive.addfile(entry, io.BytesIO(b"{}"))

        add("root/fixtures/own.json")
        if kind == "duplicate":
            add("root/fixtures/own.json")
        elif kind == "link":
            entry = tarfile.TarInfo("root/link")
            entry.type, entry.linkname = tarfile.SYMTYPE, "fixtures/own.json"
            archive.addfile(entry)
        else:
            add("root/../foreign" if kind == "traversal" else "other/extra")
    with pytest.raises(ValueError):
        read_sdist(path)


def test_sdist_unexpected_extra_and_changed_source_rejected():
    for source in (
        {"LICENSE": b"changed"},
        {"LICENSE": b"License", "extra.json": b"{}"},
    ):
        with pytest.raises(ValueError):
            check_source_and_sdist({"LICENSE": b"License"}, source, {})


def test_known_payload_masks_are_not_fresh_capture_certificates():
    body = (
        b'{"description":"Telefon +49[private-phone-1]", "fetched_at":"2026-10-07T08:18:49Z", "original_sha256":"'
        + b"a" * 64
        + b'"}'
    )
    assert (
        check_members({"fixtures/own.json": body}, {})["status"]
        == "PASS_SCOPED_CONTACT_CONTAINMENT"
    )
