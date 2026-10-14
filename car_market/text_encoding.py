"""Preserve malformed source Unicode in JSON while keeping display text valid."""

import json
import re


def json_text(value, **options):
    # A source may truncate a UTF-16 emoji between its surrogate code units.
    # Escaping preserves that exact value; ordinary payload hashes stay unchanged.
    content = json.dumps(value, ensure_ascii=False, **options)
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, ensure_ascii=True, **options)
    return content


def scalar_text(value):
    """Replace invalid display code points; original source values remain in JSON."""
    return re.sub(r"[\ud800-\udfff]", "\ufffd", value) if isinstance(value, str) else value
