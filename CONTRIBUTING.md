# Contributing

Use Python 3.12 and English for code, comments and documentation.

```sh
uv sync --frozen --group dev --extra browser --extra reports
uv run --no-sync pytest tests -q
uv build
```

Keep a change focused on an observed failure. Include its trigger, the resulting
behavior and the checks you ran. Preserve publication identity, original field
observation times and response hashes. Missing or blocked data cannot establish
that a vehicle was sold, that a field is fresh or that a market was fully collected.

For a new source or payload contract, add a small synthetic regression fixture:
only the markup or JSON members the parser reads, with neutral placeholder text.
Never commit a captured page, seller contacts, descriptions, photos, session
material or tracking parameters. See the [fixture policy](docs/fixture-policy.md). Declare supported
country, hosts and pagination in `configuration.py`; a historical model-specific
parser alone is insufficient for the generic registry. Cover identity binding,
country/price evidence, malformed data, repeated pages and access denial.

Live acceptance is opt-in and uses new Docker volumes. See
[docs/reliability.md](docs/reliability.md). Do not commit databases, raw live runs,
accounts, browser profiles, tokens or generated reports. Respect source refusal
and backoff; login, CAPTCHA solving and stealth are outside this collector.

Before opening a pull request, run the privacy check used by CI:

```sh
python3 -m tools.check_public_source_privacy --repo . --output /tmp/privacy.json
```

Project code uses MIT; bundled Lato fonts retain their OFL license.
