"""Read-only, fail-closed source/SDIST contact-containment gate.

Findings contain file/category/count only. This is not a universal PII or
credential certificate. Exact reviewed exceptions never authorize new bytes.
"""

import argparse
import ast
import gzip
import hashlib
import html
import json
import re
import subprocess
import tarfile
import unicodedata
import warnings
from collections import Counter
from datetime import date, time
from pathlib import Path, PurePosixPath

from tools.public_fixture_projection import (
    AUTHORITY_KEY,
    EMAIL,
    FAMILY_ROLE,
    JS_PAIR,
    Projection,
    is_email,
    vehicle_numeric_role,
)


def normalize(value):
    for _ in range(5):
        old = value
        value = html.unescape(value)
        value = re.sub(
            r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})",
            lambda m: chr(int(m[1] or m[2], 16)),
            value,
        )
        value = value.replace(r"\/", "/").replace(r"\"", '"')
        if value == old:
            break
    value = unicodedata.normalize("NFKC", value)
    value = re.sub("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", value)
    return value.translate(str.maketrans({c: "-" for c in "‐‑‒–—―−"}))


PRIVATE_PATH = re.compile(
    r"(?<![\w])(?:file:/{2})?(?:/"
    + r"Users/|/"
    + r"private/var/|/"
    + r"var/folders/|[A-Za-z]:[\\/]+"
    + r"Users[\\/]+)"
    r"[^\s\"'<>/\\]+[/\\]+[^\s\"'<>]+|(?<![\w:/])/" + r"home/(?!collector/|runner/)"
    r"[^\s\"'<>/]+/[^\s\"'<>]+"
)
PHONE = re.compile(
    r"(?<![\w.])(?:\+|00)[1-9](?:[\s()./-]*\d){7,14}(?!\w)|(?<![\w.])0[1-9](?:[\s()./-]*\d){7,10}(?!\w)"
)
CUE = re.compile(
    r"tel(?:efoon|efon|ephone)?|phone|whats\s*app|handy|anruf|bellen|bereikbaar|erreichbar",
    re.IGNORECASE,
)
DESCRIPTION = re.compile(
    r'"(?:description|adDescription|shortDescription|sellerDescription|freeText)"\s*:\s*("(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)
FAMILY = re.compile(
    r"\b(?:mijn|onze|my|our|meine?|unsere?)\s+(?:vrouw|man|kind(?:eren)?|wife|husband|children|family|frau|kinder)\b",
    re.IGNORECASE,
)
PERSON_FIELD = re.compile(
    r"^(?:contactName|firstName|lastName|fullName|sellerName|streetAddress|addressLine1|postalAddress)$",
    re.IGNORECASE,
)
SESSION_FIELD = AUTHORITY_KEY
STRING_FIELD = re.compile(r'"(?P<key>[^"\n]+)"\s*:\s*(?P<value>"(?:\\.|[^"\\])*")')


def date_or_native_role(text, match):
    """Exclude only exact typed/date-power roles, never a whole body/category."""
    # RSS date fields cannot be authorized by an unrelated nearby phone cue.
    for tag in re.finditer(
        r"<(pubdate|created|updated|timestamp|date)>([^<>]*)</\1>",
        text[max(0, match.start() - 80) : match.end() + 80],
        re.IGNORECASE,
    ):
        base = max(0, match.start() - 80)
        if base + tag.start(2) <= match.start() and match.end() <= base + tag.end(2):
            value = re.fullmatch(
                r"\s*(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?\s*",
                tag[2],
            )
        else:
            value = None
        if value is not None:
            try:
                # Recognize both retained calendar renderings, without
                # converting the field or asserting an evidence clock.
                try:
                    date(int(value[3]), int(value[2]), int(value[1]))
                except ValueError:
                    date(int(value[3]), int(value[1]), int(value[2]))
                if value[4] is not None:
                    time(int(value[4]), int(value[5]), int(value[6] or 0))
            except ValueError:
                pass
            else:
                return "typed_rss_capture_date"
    numeric_role = vehicle_numeric_role(text, match.start(), match.end())
    if numeric_role:
        return numeric_role
    # ID/URI fields retain exact native values even beside a contact field.
    for field in STRING_FIELD.finditer(
        text, max(0, match.start() - 500), match.end() + 500
    ):
        if not (field.start("value") < match.start() < field.end("value")):
            continue
        key = field["key"].lower()
        if key in {
            "id",
            "external_id",
            "sku",
            "vin",
            "kenteken",
            "plate",
            "registration",
        }:
            return "typed_native_identity"
        if key in {"url", "uri", "final_url", "src", "image", "images"}:
            try:
                value = json.loads(field["value"])
            except ValueError:
                continue
            if value.startswith(("https://", "http://")):
                return "typed_publication_or_image_uri"
    return None


def placeholder(value):
    return not value.strip() or bool(
        re.fullmatch(
            r"\[(?:redacted|private|projected)[^\]]*\]", value.strip(), re.IGNORECASE
        )
    )


def structured_role_loci(text):
    """Return exact role loci; unknown personal/session roles remain held."""
    rows = []
    for field in STRING_FIELD.finditer(normalize(text)):
        if not (
            PERSON_FIELD.fullmatch(field["key"])
            or SESSION_FIELD.fullmatch(field["key"])
        ):
            continue
        try:
            value = json.loads(field["value"])
        except ValueError:
            continue
        if not placeholder(value):
            rows.append(
                {
                    "category": "unreviewed_session_literal"
                    if SESSION_FIELD.fullmatch(field["key"])
                    else "unreviewed_name_address_role",
                    "offset": field.start(),
                    "field": field["key"],
                }
            )
    # Native bootstrap objects also use single-quoted/unquoted keys. Their
    # authority literals receive the same admission policy as JSON strings.
    normal = normalize(text)
    for field in JS_PAIR.finditer(normal):
        key = field["key"].strip("\"'")
        if not SESSION_FIELD.fullmatch(key):
            continue
        if field["key"].startswith('"') and field["value"].startswith('"'):
            continue  # Already covered by the JSON field lexer above.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                value = ast.literal_eval(field["value"])
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str) and not placeholder(value):
            rows.append(
                {
                    "category": "unreviewed_session_literal",
                    "offset": field.start(),
                    "field": key,
                }
            )
    # HTML metadata is a delivered literal even outside fixture directories.
    from html.parser import HTMLParser

    class Metadata(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            name = values.get("name", "")
            content = values.get("content", "")
            if (
                tag == "meta"
                and (SESSION_FIELD.fullmatch(name) or PERSON_FIELD.fullmatch(name))
                and not placeholder(content)
            ):
                rows.append(
                    {
                        "category": "unreviewed_html_sensitive_metadata",
                        "line": self.getpos()[0],
                        "field": name,
                    }
                )

    reader = Metadata()
    reader.feed(text)
    return rows


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pointer_value(document, pointer):
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def original_proof(metadata_file, pointer):
    """Exact baseline value, never a length-only original-hash assertion."""
    repo = Path(__file__).resolve().parents[1]
    bindings = json.loads(
        (repo / "tests/fixtures/private-capture-proof-bindings.json").read_text()
    )
    if bindings["baseline_pin"] != "818a09572cccda2334ecb8a4790a7ff20cda354d":
        raise ValueError("Unexpected private baseline proof pin")
    return bindings["files"][metadata_file]["protected_values"][pointer]


def check_original_proofs(files):
    name = "tests/fixtures/private-capture-proof-bindings.json"
    bindings = json.loads(files[name])
    for filename, row in bindings["files"].items():
        document = json.loads(files[filename])
        for pointer, value in row["protected_values"].items():
            if pointer_value(document, pointer) != value:
                raise ValueError(
                    "Original capture hash/clock changed: " + filename + " " + pointer
                )


def scan_member(name, wire, policy):
    counts = Counter()
    body = wire
    if name.endswith(".gz"):
        try:
            body = gzip.decompress(wire)
        except (OSError, EOFError):
            return {
                "file": name,
                "file_sha256": digest(wire),
                "categories": {"invalid_gzip": 1},
            }
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        allowed = policy.get("reviewed_binary", {}).get(name)
        if allowed != digest(wire):
            counts["unreviewed_binary"] += 1
        return {
            "file": name,
            "file_sha256": digest(wire),
            "decoded_sha256": digest(body),
            "gzip": name.endswith(".gz"),
            "categories": dict(counts),
        }
    normal = normalize(text)
    approval = policy.get("reviewed_loci", {}).get(name, {})
    admitted = (
        approval.get("loci", [])
        if approval.get("decoded_sha256") == digest(body)
        else []
    )
    reviewed = Counter()

    def authorize(category, offset, field=None, token_offset=None, line=None):
        for locus in admitted:
            if (
                locus["category"] == category
                and locus["offset"] == offset
                and (field is None or locus.get("field") == field)
                and (token_offset is None or locus.get("token_offset") == token_offset)
                and (line is None or locus.get("line") == line)
            ):
                reviewed[locus["role"]] += 1
                return True
        return False

    for match in PRIVATE_PATH.finditer(normal):
        if not authorize("private_filesystem_path", match.start()):
            counts["private_filesystem_path"] += 1
    if name.startswith(("tests/fixtures/", "fixtures/")) or name.endswith(
        (".html", ".xml")
    ):
        projection = Projection()
        try:
            projected = projection.text(text, name)
        except (ValueError, TypeError, RecursionError):
            counts["uninspectable_embedded_text"] += 1
        else:
            if projected != text:
                counts.update({k: v for k, v in projection.counts.items() if v})
    # Every delivered text channel is inspected, including docs, source literals
    # and generated SDIST metadata. A directory never authorizes contact text.
    role_loci = structured_role_loci(text)
    for locus in role_loci:
        if not authorize(
            locus["category"],
            locus.get("offset", -1),
            locus["field"],
            line=locus.get("line"),
        ):
            counts[locus["category"]] += 1
    for match in FAMILY_ROLE.finditer(normal):
        if not authorize("personal_family_narrative", match.start()):
            counts["personal_family_narrative"] += 1
    for match in EMAIL.finditer(normal):
        if is_email(match[0]) and not authorize(
            "delivered_email_literal", match.start()
        ):
            counts["delivered_email_literal"] += 1
    suppressed = Counter()
    for m in PHONE.finditer(normal):
        context = normal[max(0, m.start() - 100) : m.end() + 80]
        if not CUE.search(context):
            continue
        role = date_or_native_role(normal, m)
        if role:
            suppressed[role] += 1
        else:
            if not authorize("normalized_contact_context", m.start()):
                counts["normalized_contact_context"] += 1
    for m in DESCRIPTION.finditer(normal):
        try:
            value = normalize(json.loads(m[1]))
        except ValueError:
            continue
        for token in PHONE.finditer(value):
            role = date_or_native_role(value, token)
            if role:
                suppressed[role] += 1
            else:
                if not authorize(
                    "description_phone", m.start(), token_offset=token.start()
                ):
                    counts["description_phone"] += 1
    counts = Counter({k: v for k, v in counts.items() if v})
    row = {
        "file": name,
        "file_sha256": digest(wire),
        "decoded_sha256": digest(body),
        "gzip": name.endswith(".gz"),
        "categories": dict(counts),
    }
    if suppressed:
        row["noncontact_typed_roles"] = dict(suppressed)
    if role_loci:
        row["role_loci"] = role_loci
    if reviewed:
        row["reviewed_locus_roles"] = dict(reviewed)
    return row


def check_members(files, policy):
    rows = [scan_member(name, body, policy) for name, body in sorted(files.items())]
    findings = [r for r in rows if r["categories"]]
    return {
        "status": "HOLD" if findings else "PASS_SCOPED_CONTACT_CONTAINMENT",
        "members": len(rows),
        "gzip_full_crc_checked": sum(r.get("gzip", False) for r in rows),
        "findings": findings,
        "manifest": rows,
        "limits": "Recorded contact/path/narrative patterns and exact reviewed binary/type exceptions only; no all-PII/secret, history or rights certificate.",
    }


def read_sdist(path):
    files, seen, root = {}, set(), None
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            p = PurePosixPath(member.name)
            if p.is_absolute() or ".." in p.parts or "\\" in member.name or not p.parts:
                raise ValueError("Unsafe SDIST member name")
            if member.name in seen:
                raise ValueError("Duplicate SDIST member")
            seen.add(member.name)
            if root is None:
                root = p.parts[0]
            if p.parts[0] != root or not (member.isdir() or member.isfile()):
                raise ValueError("Unexpected SDIST root/member type")
            if member.isfile():
                name = "/".join(p.parts[1:])
                if not name:
                    raise ValueError("Missing SDIST relative member name")
                files[name] = archive.extractfile(member).read()
    return files


def check_source_and_sdist(expected, source_files, policy):
    if not all(source_files.get(k) == v for k, v in expected.items()):
        raise ValueError("SDIST missing or changed tracked source bytes")
    generated = {
        "PKG-INFO",
        "setup.cfg",
        "car_market.egg-info/PKG-INFO",
        "car_market.egg-info/SOURCES.txt",
        "car_market.egg-info/dependency_links.txt",
        "car_market.egg-info/entry_points.txt",
        "car_market.egg-info/requires.txt",
        "car_market.egg-info/top_level.txt",
    }
    if set(source_files) - set(expected) - generated:
        raise ValueError("Unexpected SDIST extra files")
    if "tests/fixtures/private-capture-proof-bindings.json" in expected:
        check_original_proofs(expected)
        check_original_proofs(source_files)
    results = {
        "tracked_source": check_members(expected, policy),
        "built_sdist": check_members(source_files, policy),
    }
    if any(r["status"] == "HOLD" for r in results.values()):
        raise PrivacyHold(results)
    return results


class PrivacyHold(ValueError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(
            "Public source privacy HOLD; categories/paths only, no values emitted"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    names = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=args.repo)
        .decode()
        .split("\0")
    )
    expected = {name: (args.repo / name).read_bytes() for name in names if name}
    if "tests/fixtures/private-capture-proof-bindings.json" in expected:
        check_original_proofs(expected)
    policy = json.loads(
        (args.repo / "tools/public-source-privacy-policy.json").read_text()
    )
    if args.sdist:
        try:
            result = check_source_and_sdist(expected, read_sdist(args.sdist), policy)
        except PrivacyHold as error:
            result = error.receipt
    else:
        result = {"tracked_source": check_members(expected, policy)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: {
                    "status": v["status"],
                    "members": v["members"],
                    "gzip": v["gzip_full_crc_checked"],
                    "finding_files": len(v["findings"]),
                }
                for k, v in result.items()
            }
        )
    )
    return 1 if any(r["status"] == "HOLD" for r in result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
