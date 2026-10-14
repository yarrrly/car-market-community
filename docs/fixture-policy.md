# Test fixtures

The regression fixtures in `tests/fixtures/` and `fixtures/` are small
synthetic parser cases. Each one keeps only the markup or JSON members that its
tests read: the element a selector targets, a counter, a price field, an
identifier, a pagination link. Everything else is absent, and descriptive text
that a test does not assert is replaced with neutral placeholder words.

The fixtures contain no complete provider pages, seller contacts, listing
descriptions or photos. Seller and dealer names, street addresses, phone
numbers and e-mail addresses are placeholders such as `lorem ipsum`,
`Musterstraße 12` or `redacted@example.invalid`. A few short listing titles
and dealer phrases remain where a test checks their exact text, for example a
trim name the model matcher must recognise. Dates, prices, mileage and
identifiers are scenario values for the tests. They are not current market
facts, and the dates in manifests are test clocks rather than proof that a page
looked like this on that day.

Manifest hashes bind each fixture file to its tests, so a changed fixture must
come with updated hashes. `tests/test_synthetic_fixtures.py` checks that every
fixture is listed in `tests/fixtures/SYNTHETIC.json` with its current SHA-256.

## Adding a fixture

1. Write the smallest document that reproduces the provider's declared
   contract: the same tag structure, attribute names or JSON keys the parser
   uses, with placeholder values.
2. Do not save or commit a captured response. If you need a real page to
   understand a contract, keep it on your own machine.
3. Add the file and its SHA-256 to `tests/fixtures/SYNTHETIC.json`.
4. Run the privacy check and the test suite:

```sh
python3 -m tools.check_public_source_privacy --repo . --output /tmp/privacy.json
uv run --no-sync pytest tests -q
```

The privacy check looks for phone numbers, e-mail addresses, absolute home
paths and session or token fields. A passing check does not prove that a file
is free of personal data, so review the bytes you commit.

## Provider terms

Several marketplaces restrict copying their content and automated collection
(for example [Kleinanzeigen §5](https://themen.kleinanzeigen.de/nutzungsbedingungen/),
[AutoScout24 DE §§7–8](https://www.autoscout24.de/unternehmen/verbraucher-agb/)
and [Marktplaats I.7](https://www.marktplaats.nl/i/help/over-marktplaats/voorwaarden-en-privacybeleid/algemene_voorwaarden_marktplaats.pdf?v=6)).
That is why this repository ships synthetic fixtures instead of saved pages.
If you run Car Market against a site, you are responsible for complying with
that site's terms. The project's MIT license covers its own code and
documentation only.
