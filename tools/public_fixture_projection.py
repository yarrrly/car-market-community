"""Project literal contacts and private capture paths without changing runtime code.

This operates on private source fixtures, not collector data. Original captures
must remain private; output bytes are projections, never renewed captures.
"""

import ast
import difflib
import gzip
import hashlib
import html
import json
import re
import subprocess
import warnings
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

BASE = "818a09572cccda2334ecb8a4790a7ff20cda354d"
PHONE_KEY = re.compile(
    r"phone|telefon|telefoon|whatsapp|mobiel|fax|gsm|^mobile$", re.IGNORECASE
)
CONTACT_KEY = re.compile(r"email|e-mail", re.IGNORECASE)
PROTECTED_KEY = re.compile(
    r"^(?:id|external_id|sku|vin|kenteken|plate|registration|year|price|mileage|"
    r"date|pubdate|timestamp|fetched_at|captured_at|observed_at|"
    r"url|uri|final_url|image|images|src|href|street|companyName|seller|name)$",
    re.IGNORECASE,
)
DESCRIPTION_KEY = re.compile(
    r"description|adverttext|fre[e]?text|narrative", re.IGNORECASE
)
SEPARATOR = r"[ \t\u00a0.()/-]*"
INTERNATIONAL = re.compile(
    rf"(?<![\w\d])(?:\+|00)[1-9](?:{SEPARATOR}\d){{7,14}}(?![\w\d])"
)
NATIONAL = re.compile(rf"(?<![\w\d])0[1-9](?:{SEPARATOR}\d){{8,11}}(?![\w\d])")
NL_MOBILE = re.compile(r"(?<![\w\d])06(?:[ \t.-]*\d){8}(?![\w\d])")
PAIRED_MOBILE = re.compile(
    r"(?<![\w\d])06(?:[ \t.-]*\d){8}\s*/\s*06(?:[ \t.-]*\d){8}(?![\w\d])"
)
DATE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}(?:\s|$)")
PREFIX = re.compile(r"^(?:\+|00)(?:31|49|32|43|33|39|41|44|48|90|380|1|7)")
LABEL = re.compile(
    r"\b(?:tel(?:efoon|ephone|efon)?|phone|mobiel|mobile|handy|whats\s*app|bel(?:len)?|anrufen|rufnummer|contact|kontakt|call)\b",
    re.IGNORECASE,
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ASSET_SUFFIXES = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
    "css",
    "js",
    "woff",
    "woff2",
    "ttf",
    "map",
}
LOCAL = re.compile(
    r"(?:/"
    + r"Users/|/"
    + r"private/var/|/"
    + r"var/folders/|[A-Za-z]:\\"
    + r"Users\\)[^\s<>\"']+"
)
PRIVATE_HOME = re.compile(
    r"(?<![\w:/])/" + r"home/(?!collector(?:/|\b)|runner(?:/|\b))[^\s<>\"']+"
)
QUOTED_PAIR = re.compile(r'(?P<key>"[^"\n]+")\s*:\s*(?P<value>"(?:\\.|[^"\\])*")')
JS_PAIR = re.compile(
    r"""(?P<key>"[^"\n]+"|'[^'\n]+'|\b[A-Za-z_$][\w$]*)\s*:\s*(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)
JS_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
PERSONAL_NAME = re.compile(
    r"(?P<intro>(?i:\b(?:mijn naam is|ik heet|ich hei(?:ß|ss)e|mein name ist|my name is)\s+))"
    r"(?P<name>[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+){0,2})"
)
FAMILY_ROLE = re.compile(
    r"\b(?:mijn|onze|my|our|meine[mnr]?|unsere?)\s+(?:vrouw|man|vader|moeder|kind(?:eren)?|familie|zoon|dochter|wife|husband|father|mother|children|family|son|daughter|frau|mann|vater|mutter|kinder|familie)\b",
    re.IGNORECASE,
)
ATTRIBUTE = re.compile(
    r"""(?P<key>[\w:-]+)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))"""
)
JS_QUOTED = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' """, re.VERBOSE)
AUTHORITY_KEY = re.compile(
    r"^(?:auth|stagingAuth|htaccess|session[-_]?id|session[-_]?token|csrf[-_]?token|xsrf[-_]?token|cookie[-_]?value|access[-_]?token|PHPSESSID)$",
    re.IGNORECASE,
)


def html_scalar_projection(literal, before, after):
    """Retain original HTML entity spellings outside changed contact spans."""
    units = []
    at = 0
    while at < len(literal):
        end = at + 1
        if literal[at] == "&":
            entity = re.match(
                r"&(?:#[xX][0-9a-fA-F]+|#\d+|[A-Za-z][A-Za-z0-9]+);", literal[at:]
            )
            if entity:
                end = at + len(entity[0])
        units.extend((at, end) for _ in html.unescape(literal[at:end]))
        at = end
    if len(units) != len(before):
        raise ValueError("HTML scalar encoding cannot be preserved exactly")
    edits = []
    for kind, a, b, c, d in difflib.SequenceMatcher(
        a=before, b=after, autojunk=len(before) > 4096
    ).get_opcodes():
        if kind == "equal":
            continue
        start = units[a][0] if a < len(units) else len(literal)
        end = units[b - 1][1] if b > a else start
        edits.append((start, end, html.escape(after[c:d], quote=True)))
    result = literal
    for start, end, value in reversed(edits):
        result = result[:start] + value + result[end:]
    if html.unescape(result) != after:
        raise ValueError("HTML scalar projection mismatch")
    return result


def sha(body):
    return hashlib.sha256(body).hexdigest()


def is_email(value):
    domain = value.rsplit("@", 1)[-1].lower()
    return domain.rsplit(".", 1)[-1] not in ASSET_SUFFIXES and not domain.endswith(
        ".invalid"
    )


def phone_literal(value):
    return (
        isinstance(value, str)
        and sum(c.isdigit() for c in value) >= 6
        and re.fullmatch(r"(?:tel:)?[+\d \t\u00a0()./-]+", value.strip(), re.IGNORECASE)
        is not None
    )


def frontend_dsn(value):
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.username is not None
            and parsed.hostname is not None
            and (
                parsed.hostname == "sentry.io" or parsed.hostname.endswith(".sentry.io")
            )
            and parsed.path.strip("/").isdigit()
        )
    except ValueError:
        return False


def vehicle_numeric_role(text, start, end):
    """Known automotive adjacent scalar facts, never a whole-text exception."""
    left, right = text[max(0, start - 160) : start], text[start : end + 60]
    if re.search(r"(?:phone|telefon|telefoon|tel)\s*[:=]?\s*$", left, re.IGNORECASE):
        return None
    if re.match(
        r"(0[1-9]|1[012])[./](?:19|20)\d{2}[\s-]+\d+(?:[.,]\d+)?\s*(?:PS|kW)\b", right
    ):
        return "registration_month_year_and_engine_power"
    if re.match(
        r"(0[1-9]|1[012])[./](?:19|20)\d{2}\s+\d{1,3}(?:[.,]\d{3})+\s*km\b",
        right,
        re.IGNORECASE,
    ):
        return "service_month_year_and_mileage"
    if re.match(
        r"(0[1-9]|1[012])/(?:19|20)\d{2}\s*-*\s*\d{1,3}(?:[.,]\d{3})+\s*€", right
    ) and re.search(r"\b(?:EZ|Erstzulassung)\s*:?\s*$", left, re.IGNORECASE):
        return "declared_registration_month_year_and_cash_price"
    return None


def string_literal_projection(literal, before, after):
    """Replace decoded changed spans while retaining unrelated JSON escapes."""
    units = []
    at = 1
    while at < len(literal) - 1:
        start = at
        if literal[at] == "\\":
            at += 6 if literal[at + 1] == "u" else 2
            if (
                literal[start : start + 2] == "\\u"
                and 0xD800 <= int(literal[start + 2 : start + 6], 16) <= 0xDBFF
                and literal[at : at + 2] == "\\u"
            ):
                at += 6
        else:
            at += 1
        decoded = json.loads('"' + literal[start:at] + '"')
        units.extend((start, at) for _ in decoded)
    if len(units) != len(before):
        raise ValueError("Cannot preserve JSON scalar encoding exactly")
    edits = []
    for kind, a, b, c, d in difflib.SequenceMatcher(
        a=before, b=after, autojunk=len(before) > 4096
    ).get_opcodes():
        if kind == "equal":
            continue
        start = units[a][0] if a < len(units) else len(literal) - 1
        end = units[b - 1][1] if b > a else start
        edits.append((start, end, json.dumps(after[c:d], ensure_ascii=False)[1:-1]))
    result = literal
    for start, end, value in reversed(edits):
        result = result[:start] + value + result[end:]
    if json.loads(result) != after:
        raise ValueError("JSON scalar projection verification failed")
    return result


def json_projection_spans(text, before, after):
    """Preserve every byte outside changed string-value spans."""
    decoder = json.JSONDecoder()
    edits = []

    def whitespace(at):
        while at < len(text) and text[at] in " \t\n\r":
            at += 1
        return at

    def visit(at, old, new):
        at = whitespace(at)
        if isinstance(old, dict):
            cursor = whitespace(at + 1)
            while text[cursor] != "}":
                key, cursor = decoder.raw_decode(text, cursor)
                cursor = whitespace(cursor)
                cursor = whitespace(cursor + 1)
                actual, end = decoder.raw_decode(text, cursor)
                # Duplicate keys retain their original syntax; edit only the
                # matching parsed value, never normalize or remove duplicates.
                if key in old and actual == old[key]:
                    visit(cursor, old[key], new[key])
                cursor = whitespace(end)
                if text[cursor] == ",":
                    cursor = whitespace(cursor + 1)
        elif isinstance(old, list):
            cursor = whitespace(at + 1)
            for a, b in zip(old, new, strict=True):
                _, end = decoder.raw_decode(text, cursor)
                visit(cursor, a, b)
                cursor = whitespace(end)
                if text[cursor] == ",":
                    cursor = whitespace(cursor + 1)
        elif isinstance(old, str) and old != new:
            actual, end = decoder.raw_decode(text, at)
            if actual != old or not isinstance(new, str):
                raise ValueError("Unexpected fixture scalar mutation")
            edits.append((at, end, string_literal_projection(text[at:end], old, new)))
        elif old != new:
            raise ValueError("Non-string native fixture value changed")

    visit(0, before, after)
    result = text
    for start, end, value in sorted(edits, reverse=True):
        result = result[:start] + value + result[end:]
    if json.loads(result) != after:
        raise ValueError("Projected JSON structure mismatch")
    return result


class Projection:
    def __init__(self, existing=None):
        self.phones = dict(existing or {})
        self.symbols = {}
        self.counts = Counter()
        self.filename = "synthetic-fixture"

    def symbol(self, role, value):
        if not value or value.startswith(("[private-", "[redacted")):
            return value
        key = (role, value)
        if key not in self.symbols:
            self.symbols[key] = f"[private-{role}-{len(self.symbols) + 1}]"
        self.counts[role + "_literal"] += 1
        return self.symbols[key]

    def phone(self, value):
        if not value or "[redacted" in value or "[private-phone-" in value:
            return value
        if value not in self.phones:
            prefix = PREFIX.match(value.strip())
            self.phones[value] = (
                prefix[0] if prefix else ""
            ) + f"[private-phone-{len(self.phones) + 1}]"
        self.counts["literal_phone"] += 1
        return self.phones[value]

    def contact_text(self, value):
        value = self.local_path(value)

        def number(match):
            token = match[0].rstrip()
            if DATE.match(token) or vehicle_numeric_role(
                value, match.start(), match.end()
            ):
                return match[0]
            return self.phone(token) + match[0][len(token) :]

        value = INTERNATIONAL.sub(number, value)
        # A slash separating two advertised mobile contacts is not part of one
        # telephone value. Retain their exact individual equality classes.
        value = NL_MOBILE.sub(number, value)
        value = NATIONAL.sub(number, value)

        def email(match):
            if not is_email(match[0]):
                return match[0]
            self.counts["literal_email"] += 1
            return "redacted@example.invalid"

        value = EMAIL.sub(email, value)
        value, count = PERSONAL_NAME.subn(
            lambda m: m["intro"] + "[personal name omitted]", value
        )
        self.counts["personal_narrative_name"] += count
        value, count = FAMILY_ROLE.subn("[private owner narrative omitted]", value)
        self.counts["personal_family_role"] += count
        return value

    def local_path(self, value, pointer=""):
        if re.fullmatch(r"https?://\S+", value):
            return value

        def replace(match):
            self.counts["private_capture_path"] += 1
            return "private-evidence:" + self.filename + "#" + pointer

        return PRIVATE_HOME.sub(replace, LOCAL.sub(replace, value))

    def labelled_literal_contacts(self, value):
        """Only adjacent explicit contact labels in partial JS/flight strings."""

        def replace(match):
            left = value[max(0, match.start() - 60) : match.start()]
            if not re.search(
                r"\b(?:tel(?:efoon|efon|ephone)?|phone|mobiel|mobile|email|e-mail)\s*[:=]?\s*$",
                left,
                re.IGNORECASE,
            ):
                return match[0]
            return self.contact_text(match[0])

        for pattern in (INTERNATIONAL, NATIONAL, EMAIL):
            value = pattern.sub(replace, value)
        return value

    def string(self, value, key="", path=""):
        value = self.local_path(value, path)
        if frontend_dsn(value):
            return self.symbol("telemetry-project", value)
        if AUTHORITY_KEY.fullmatch(key):
            return self.symbol("authority", value)
        if key == "sellerName" and path.endswith("/sellerInformation/sellerName"):
            return self.symbol("seller-label", value)
        if value.lstrip().startswith("<"):
            parser = FixtureHTML(self, value)
            parser.feed(value)
            parser.close()
            return parser.projected()
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except ValueError:
                pass
            else:
                projected = self.document(nested, key, path)
                if projected != nested:
                    return json_projection_spans(value, nested, projected)
        if PROTECTED_KEY.fullmatch(key):
            if key.lower() == "name" and PAIRED_MOBILE.search(value):
                # Retained Driveworks repeats two literal contacts in both the
                # own DOM title and JSON-LD name. Project the same spans in both
                # while retaining every vehicle token and strict title binding.
                return self.contact_text(value)
            if key.lower() == "href" and value.lower().startswith("mailto:"):
                return "mailto:redacted@example.invalid"
            if key.lower() == "href" and value.lower().startswith("tel:"):
                return "tel:" + self.phone(value[4:])
            if key.lower() == "street" and LABEL.search(value):
                return self.contact_text(value)
            if key.lower() == "street" and INTERNATIONAL.search(value):
                # Only literal international contact spans are projected; the
                # owning country/address record is never inferred or removed.
                return INTERNATIONAL.sub(lambda m: self.phone(m[0].rstrip()), value)
            return value
        if PHONE_KEY.search(key) or (
            key in {"number", "formattedNumber", "callTo"} and PHONE_KEY.search(path)
        ):
            if phone_literal(value):
                return self.phone(value)
            return self.contact_text(value)
        if CONTACT_KEY.search(key):
            return self.contact_text(value)
        if (
            DESCRIPTION_KEY.search(key)
            or LABEL.search(value)
            or FAMILY_ROLE.search(value)
        ):
            return self.contact_text(value)
        return value

    def document(self, value, key="", path=""):
        if isinstance(value, dict):
            return {
                k: self.document(
                    v, k, path + "/" + k.replace("~", "~0").replace("/", "~1")
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            projected = [
                self.document(v, key, path + f"/{i}") for i, v in enumerate(value)
            ]
            # Native Nuxt uses numeric references. Edit only a direct string cell
            # whose incoming dictionary edges all explicitly name contact fields.
            incoming = {}
            for i, record in enumerate(value):
                if isinstance(record, dict):
                    for label, ref in record.items():
                        if type(ref) is int and 0 <= ref < len(value):
                            incoming.setdefault(ref, []).append(label)
                elif isinstance(record, list):
                    for ref in record:
                        if type(ref) is int and 0 <= ref < len(value):
                            incoming.setdefault(ref, []).append("<array-edge>")
            for ref, labels in incoming.items():
                literal = value[ref]
                if (
                    isinstance(literal, str)
                    and labels
                    and all(PHONE_KEY.search(label) for label in labels)
                    and phone_literal(literal)
                ):
                    projected[ref] = self.phone(literal)
                    self.counts["direct_nuxt_phone_cell"] += 1
            return projected
        if isinstance(value, str):
            return self.string(value, key, path)
        return value

    def text(self, value, filename):
        self.filename = filename
        try:
            document = json.loads(value)
        except ValueError:
            document = None
        if document is not None:
            projected = self.document(document)
            return (
                json_projection_spans(value, document, projected)
                if projected != document
                else value
            )
        if filename.endswith(
            (".html", ".xml", ".html.gz", ".xml.gz")
        ) or value.lstrip().startswith("<"):
            parser = FixtureHTML(self, value)
            parser.feed(value)
            parser.close()
            return parser.projected()
        return self.local_path(value)


class FixtureHTML(HTMLParser):
    def __init__(self, projection, text):
        super().__init__(convert_charrefs=False)
        self.projection = projection
        self.text = text
        self.lines = [0]
        for match in re.finditer("\n", text):
            self.lines.append(match.end())
        self.stack = []
        self.edits = []

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(tag)
        raw = self.get_starttag_text()
        attribute_edits = []
        for key, value in attrs:
            if not value:
                continue
            if value.lower().startswith("tel:"):
                projected = "tel:" + self.projection.phone(value[4:])
            elif (
                key == "value"
                and tag == "input"
                and dict(attrs).get("type", "").lower() in {"button", "submit"}
                and "tel:" in dict(attrs).get("onclick", "").lower()
                and (phone_literal(value) or LABEL.search(value))
            ):
                # Native call button repeats the telephone as its visible value.
                # Other inputs, numeric references and non-contact labels stay.
                projected = (
                    self.projection.phone(value)
                    if phone_literal(value)
                    else self.projection.contact_text(value)
                )
            elif value.lower().startswith("mailto:"):
                projected = "mailto:redacted@example.invalid"
                self.projection.counts["literal_email"] += 1
            elif key in {"onclick", "href", "title"} and "tel:" in value.lower():
                projected = re.sub(
                    r"(?i)(tel:\s*)([+\d][\d \t\u00a0()./-]{5,}\d)",
                    lambda m: m[1] + self.projection.phone(m[2]),
                    value,
                )
            elif key == "title" and LABEL.search(value):
                projected = self.projection.contact_text(value)
            elif (
                key == "title"
                and phone_literal(value)
                or (
                    PHONE_KEY.search(key)
                    and sum(c.isdigit() for c in value) >= 6
                    or key in {"data-tel", "data-telephone", "data-whatsapp"}
                )
            ):
                projected = self.projection.phone(value)
            elif key in {
                "props",
                "data-state",
                "data-props",
                "data-json",
                "data-config",
            } or value.lstrip().startswith(("{", "[")):
                try:
                    native = json.loads(value)
                except ValueError:
                    continue
                projected_doc = self.projection.document(native)
                if projected_doc == native:
                    continue
                projected = json_projection_spans(value, native, projected_doc)
            elif tag == "iframe" and key == "src":
                projected = EMAIL.sub(
                    lambda m: "redacted@example.invalid" if is_email(m[0]) else m[0],
                    value,
                )
                if projected == value:
                    continue
            else:
                continue
            if projected == value:
                continue
            for match in ATTRIBUTE.finditer(raw):
                slot = next(
                    n for n in ("double", "single", "bare") if match[n] is not None
                )
                if match["key"].lower() == key and html.unescape(match[slot]) == value:
                    attribute_edits.append(
                        (
                            match.start(slot),
                            match.end(slot),
                            html_scalar_projection(match[slot], value, projected),
                        )
                    )
        after = raw
        for start, end, replacement in sorted(set(attribute_edits), reverse=True):
            after = after[:start] + replacement + after[end:]
        if after != raw:
            self.edits.append((self.position(), self.position() + len(raw), after))

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[: len(self.stack) - self.stack[::-1].index(tag) - 1]

    def unknown_decl(self, data):
        if not data.startswith("CDATA["):
            raise ValueError("Unsupported fixture markup declaration")
        value = data[6:]
        projected = self.projection.string(value, "description")
        if projected != value:
            start = self.position() + 9
            self.edits.append((start, start + len(value), projected))

    def handle_comment(self, data):
        projected = self.projection.string(data, "description")
        if projected != data:
            start = self.position() + 4
            self.edits.append((start, start + len(data), projected))

    def handle_data(self, data):
        tag = self.stack[-1] if self.stack else ""
        if tag in {"style", "pubdate", "time", "created", "updated", "date"}:
            return
        if tag == "script":
            try:
                before = json.loads(data)
            except ValueError:

                def pair(match):
                    try:
                        key = (
                            ast.literal_eval(match["key"])
                            if match["key"][0] in "\"'"
                            else match["key"]
                        )
                    except (ValueError, SyntaxError):
                        return match[0]
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", SyntaxWarning)
                            warnings.simplefilter("ignore", DeprecationWarning)
                            value = (
                                json.loads(match["value"])
                                if match["value"][0] == '"'
                                else ast.literal_eval(match["value"])
                            )
                    except (ValueError, SyntaxError):
                        return match[0]
                    if key == "children" and (
                        LABEL.search(value)
                        or any(is_email(e) for e in EMAIL.findall(value))
                        or phone_literal(value)
                        and PREFIX.match(value.strip())
                    ):
                        after = self.projection.contact_text(value)
                    elif (
                        key == "number"
                        and phone_literal(value)
                        and PREFIX.match(value.strip())
                    ):
                        after = self.projection.phone(value)
                    elif (
                        PHONE_KEY.search(key)
                        or CONTACT_KEY.search(key)
                        or DESCRIPTION_KEY.search(key)
                        or AUTHORITY_KEY.fullmatch(key)
                        or key.lower().endswith("dsn")
                        or key == "href"
                    ):
                        after = self.projection.string(value, key, "$.script." + key)
                    else:
                        return match[0]
                    if after == value:
                        return match[0]
                    offset = match.start("value") - match.start()
                    if match["value"][0] == "'":
                        if not (
                            AUTHORITY_KEY.fullmatch(key)
                            or PHONE_KEY.search(key)
                            or CONTACT_KEY.search(key)
                            or frontend_dsn(value)
                        ):
                            raise ValueError(
                                "Unreviewed changed single-quoted native value"
                            )
                        return (
                            match[0][:offset]
                            + "'"
                            + after.replace("\\", "\\\\").replace("'", "\\'")
                            + "'"
                        )
                    return match[0][:offset] + string_literal_projection(
                        match["value"], value, after
                    )

                after = JS_PAIR.sub(pair, data)

                # Hydration/flight scripts carry quoted JSON or HTML fragments.
                # Decode string literals only; never execute JavaScript.
                def quoted(match):
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", SyntaxWarning)
                            warnings.simplefilter("ignore", DeprecationWarning)
                            value = (
                                json.loads(match[0])
                                if match[0][0] == '"'
                                else ast.literal_eval(match[0])
                            )
                    except (ValueError, SyntaxError):
                        return match[0]
                    if phone_literal(value) and PREFIX.match(value.strip()):
                        result = self.projection.phone(value)
                    elif frontend_dsn(value):
                        result = self.projection.symbol("telemetry-project", value)
                    elif value.lstrip().startswith(("{", "[", "<")):
                        result = self.projection.string(value)
                    else:
                        # A partial hydration record can contain a description or
                        # labelled text even when it is not a complete JSON doc.
                        result = self.projection.labelled_literal_contacts(
                            JS_PAIR.sub(pair, value)
                        )
                    if result == value:
                        return match[0]
                    if match[0][0] == "'":
                        if not (
                            phone_literal(value)
                            or frontend_dsn(value)
                            or LABEL.search(value)
                        ):
                            raise ValueError(
                                "Unsupported changed single-quoted native string"
                            )
                        return (
                            "'" + result.replace("\\", "\\\\").replace("'", "\\'") + "'"
                        )
                    return string_literal_projection(match[0], value, result)

                after = JS_QUOTED.sub(quoted, after)
            else:
                result = self.projection.document(before)
                after = (
                    json_projection_spans(data, before, result)
                    if result != before
                    else data
                )
        else:
            after = self.projection.contact_text(data)
        if after != data:
            self.edits.append((self.position(), self.position() + len(data), after))

    def projected(self):
        text = self.text
        last = len(text) + 1
        for start, end, replacement in sorted(self.edits, reverse=True):
            if end > last:
                raise ValueError("Overlapping fixture text edits")
            text = text[:start] + replacement + text[end:]
            last = start
        return text


def blobs(repo, pin):
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", pin], cwd=repo, text=True
    ).splitlines()
    for name in names:
        yield name, subprocess.check_output(["git", "show", pin + ":" + name], cwd=repo)


def documents(text):
    try:
        return [json.loads(text)]
    except ValueError:

        class Scripts(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_script = False
                self.parts = []

            def handle_starttag(self, tag, attrs):
                self.in_script = tag == "script"

            def handle_endtag(self, tag):
                if tag == "script":
                    self.in_script = False

            def handle_data(self, data):
                if self.in_script:
                    try:
                        self.parts.append(json.loads(data))
                    except ValueError:
                        pass

        parser = Scripts()
        parser.feed(text)
        return parser.parts


def seed_classes(repo, original_repo):
    ledger = json.loads(
        subprocess.check_output(
            ["git", "show", BASE + ":tests/fixtures/public-contact-projections.json"],
            cwd=repo,
        )
    )
    mapping = {}

    def retain(before, after):
        if phone_literal(before) and isinstance(after, str) and "[redacted" in after:
            if before in mapping and mapping[before] != after:
                raise ValueError(
                    "Existing public phone equality classes disagree; no contact values emitted"
                )
            mapping[before] = after

    def compare(before, after):
        if isinstance(before, dict) and isinstance(after, dict):
            for key in before.keys() & after.keys():
                compare(before[key], after[key])
        elif (
            isinstance(before, list)
            and isinstance(after, list)
            and len(before) == len(after)
        ):
            for a, b in zip(before, after, strict=True):
                compare(a, b)
        else:
            retain(before, after)

    for row in ledger["files"]:
        name = row["file"]
        original = subprocess.check_output(
            ["git", "show", ledger["base"] + ":" + name], cwd=original_repo
        )
        current = subprocess.check_output(["git", "show", BASE + ":" + name], cwd=repo)
        if (
            sha(original) != row["original_file_sha256"]
            or sha(current) != row["projected_file_sha256"]
        ):
            raise ValueError(
                "Exact original/current projection input hash mismatch: " + name
            )
        original = gzip.decompress(original) if name.endswith(".gz") else original
        current = gzip.decompress(current) if name.endswith(".gz") else current
        old_docs, new_docs = documents(original.decode()), documents(current.decode())
        if len(old_docs) == len(new_docs):
            for old, new in zip(old_docs, new_docs, strict=True):
                compare(old, new)

        class TelephoneLinks(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = []

            def handle_starttag(self, tag, attrs):
                for label, value in attrs:
                    if label == "href" and value and value.startswith("tel:"):
                        self.values.append(value[4:])

        old_links, new_links = TelephoneLinks(), TelephoneLinks()
        old_links.feed(original.decode())
        new_links.feed(current.decode())
        if len(old_links.values) == len(new_links.values):
            for old, new in zip(old_links.values, new_links.values, strict=True):
                retain(old, new)
    return mapping


def project_tree(repo, apply=False, original_repo=None, input_pin=BASE):
    if not re.fullmatch(r"[0-9a-f]{40}", input_pin):
        raise ValueError("Projection input must be a full immutable revision")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", input_pin, "HEAD"], cwd=repo, check=True
    )
    existing = seed_classes(repo, original_repo) if original_repo else {}
    if input_pin == "ff442a900ba9a4e667ba734a73af478286995387":
        # Reproduce the reviewed previous mapping in memory from immutable
        # private bytes. Never emit raw contacts or a per-value reverse ledger.
        code = subprocess.check_output(
            ["git", "show", input_pin + ":tools/public_fixture_projection.py"], cwd=repo
        )
        namespace = {"__name__": "private_reviewed_projection"}
        # Fixed reviewed local Git Python source, never provider JS or an
        # arbitrary input revision. Its full file digest is independently bound.
        if (
            sha(code)
            != "aa99aef192bf4cca7b633c93e289f4dd9dee91b19f54b5ffd5295ca57b5bec98"
        ):
            raise ValueError("Reviewed historical projection code hash mismatch")
        exec(compile(code, "reviewed-ff442a-projection", "exec"), namespace)  # noqa: S102
        previous = namespace["Projection"](existing)
        for name, wire in blobs(repo, BASE):
            if not name.startswith(("fixtures/", "tests/fixtures/")):
                continue
            body = gzip.decompress(wire) if name.endswith(".gz") else wire
            previous.text(body.decode(), name)
        existing = previous.phones
    projection = Projection(existing)
    rows = []
    scanned = Counter()
    for name, wire in blobs(repo, input_pin):
        decoded = gzip.decompress(wire) if name.endswith(".gz") else wire
        if name.endswith(".gz"):
            scanned["gzip_crc_checked"] += 1
        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            scanned["binary"] += 1
            continue
        scanned["text"] += 1
        if not name.startswith(("tests/fixtures/", "fixtures/")):
            continue
        before_counts = dict(projection.counts)
        after = projection.text(text, name).encode()
        if after == decoded:
            if apply:
                # A rerun starts from exact BASE bytes, including prior metadata
                # repair outputs. This clone is exclusively owned by this task.
                (repo / name).write_bytes(wire)
            continue
        archive = gzip.compress(after, mtime=0) if name.endswith(".gz") else after
        rows.append(
            {
                "file": name,
                "input_file_sha256": sha(wire),
                "input_decoded_sha256": sha(decoded),
                "projected_file_sha256": sha(archive),
                "projected_decoded_sha256": sha(after),
                "input_bytes": len(wire),
                "projected_bytes": len(archive),
                "changes": {
                    k: v - before_counts.get(k, 0)
                    for k, v in projection.counts.items()
                    if v != before_counts.get(k, 0)
                },
            }
        )
        if apply:
            (repo / name).write_bytes(archive)
    return {
        "base": input_pin,
        "status": "UNREVIEWED_CONTACT_CONTAINMENT",
        "scanned": dict(scanned),
        "changes": dict(projection.counts),
        "rows": rows,
        "limits": [
            "Projection input hashes identify already-projected 818a bytes, not original native full bodies.",
            "Original source/capture clocks and full-body hashes must remain separately retained.",
            "No all-PII, credential or history-clean certificate.",
        ],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--private-original-repo", type=Path)
    parser.add_argument("--input-revision", default=BASE)
    args = parser.parse_args()
    receipt = project_tree(
        args.repo, args.apply, args.private_original_repo, args.input_revision
    )
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "files": len(receipt["rows"]),
                "scanned": receipt["scanned"],
                "changes": receipt["changes"],
            }
        )
    )
