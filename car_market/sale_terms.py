"""Separate a displayed amount from a confirmed full Dutch retail price."""

import re

from .parse import number


def _partial_description(description, price):
    text = " ".join((description or "").split())
    # The condition must apply to the displayed amount: finance templates also
    # mention a down payment and monthly installment beside a normal full price.
    if re.search(
        r"(?i)\b(?:dit|dat|het\s+(?:getoonde|vermelde|geadverteerde))\s+bedrag\s+"
        r"(?:is|betreft)\s+(?:(?:uitsluitend|alleen|slechts|een|de)\s+)*aanbetaling\b",
        text,
    ):
        return True
    amount = r"(\d[\d.,]*(?: \d{3})*)"
    currency = r"(?:€\s*|EUR\s*)?"
    patterns = (
        r"betaal\s+nu\s+\d+(?:[.,]\d+)?\s*%\s*" + currency + amount,
        r"\baanbetaling\s*[:=]?\s*" + currency + amount,
        r"(?:€\s*|EUR\s*)" + amount + r"\s*(?:per|/)\s*m(?:aand|nd)\b",
    )
    return price is not None and any(
        number(match.group(1)) == price
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    )


def classify(item):
    if item.source.endswith("_de") or item.raw.get("market_context_country") == "DE":
        from .germany import classify_german_price

        return classify_german_price(item)
    text = item.title + " " + (item.description or "")
    warning = None
    if re.search(
        r"(?i)export\s*prijs|export\s+(?:ex|excl)\.?\s*bpm|(?:ex|excl|exclusief)\.?\s*bpm", text
    ):
        warning = "export_ex_bpm"
    elif re.search(
        r"(?i)(?:overige|extra|exclusief|excl\.?)\s+importkosten|duits(?:e)?\s+kenteken", text
    ):
        warning = "nl_import_costs_pending"
    elif re.search(
        r"(?i)betaal\s+nu\s+\d+\s*%|aanbetaling|(?:per|/)\s*m(?:aand|nd)\b", item.title
    ) or _partial_description(item.description, item.price):
        warning = "conditional_or_instalment_price"
    if warning:
        item.raw["price_warning"] = warning
        item.raw["advertised_amount_eur"] = item.price
        if item.price_type in ("advertised_price", "FIXED"):
            item.price_type = "export_price" if warning == "export_ex_bpm" else "conditional_price"
        quoted = re.search(
            r"(?i)prijs\s+(?:voor\s+)?(?:NL|Nederland)\s*[:=]?\s*€\s*([\d., ]+)", text
        )
        if quoted:
            item.raw["seller_stated_nl_price_eur"] = number(quoted.group(1))
    return item
