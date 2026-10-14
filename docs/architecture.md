# Architecture

`portable.py` provides public commands. `configuration.py` defines the source
registry, validation and database profile binding. `search_profile.py`
classifies listings by their own facts. `configured_collect.py` connects it
to the existing collector.

The core layers retain established contracts: `fetch.py` retrieves responses
with timeouts, rate limits and a standard isolated Chromium browser;
`parse.py` and native adapters extract each listing's own data;
`db.py`, `evidence.py` and `provenance.py` retain observations and provenance;
`identity*.py` merge verified identities; `vehicle_catalog.py` reads the shared
catalog; `coverage.py` and `quality.py` explain gaps.

## Profile

```json
{
  "make": "BMW",
  "make_aliases": [],
  "model_terms": [["218i", "218 i"], ["Active Tourer", "ActiveTourer"]],
  "exclude_terms": ["Gran Tourer", "Gran Coupe", "218d"],
  "generation_terms": [],
  "fuels": ["petrol"],
  "transmissions": [],
  "year_from": 2014
}
```

Alternatives within a group use OR; groups use AND. Empty `fuels` or
`transmissions` allow any value. Matching uses token boundaries and ignores
case, diacritics and hyphens. Missing evidence cannot become a match.

Normalization does not create facts. Country comes from the listing's own
data or its seller's address. Domains and query parameters do not prove
country. Generation comes from the vehicle's own model and structured fields;
advertisements for other cars and model year do not prove generation.
Strict engine/body requirements need their own `model_terms` or exclusions;
there is no built-in universal body or engine reference catalog.

The country/selector binding is stored with the selector's SHA-256. The CLI
checks it before opening a write connection. Search URLs and intervals can
change within the same search; its scope cannot.

## Add a source

1. Retain original native responses privately with URL, timestamp, status
   and SHA-256. Distribute only a policy-reviewed minimal projection or clearly
   labelled synthetic parser case; see [fixture policy](fixture-policy.md).
   Do not copy credentials, contacts or local home paths into public fixtures.
2. Implement a parser returning `Listing`/`Page`. Verify the listing's own
   route/payload identity, pages, counters, currency and full purchase price.
3. Add country, hosts and pagination to `ADAPTERS`. Generalize BMW/Mercedes
   filters before using them in the generic profile.
4. Test native response replay, model/country conflicts, recommendations for
   other cars, 403/429, repeated pages, partial passes and price updates.
5. Bound time, pages and details. Reconcile unique parsed publication IDs with
   the exact query's source-reported counter, retaining filter/counter drift,
   repeated placements and source page caps as limitations. Separate this
   enumeration from selector matches, country proof and vehicle identity.

The registry deliberately includes only adapters connected to the generic
profile. An existing specialized module does not prove support for arbitrary
makes.

`coverage.py` reports a descriptive query fraction, including parsed cards
outside the selector and cards awaiting country proof. A complete query does
not prove that the chosen filters include every target publication. There is
no national inventory denominator; whole-market coverage remains unknown.

`configured_collect.py` retains unresolved country evidence in the discovery
queue. A later accepted own detail response can resolve a publication without
replacing its original search membership evidence or timestamps. Gaspedaal
discoveries are excluded from automatic detail requests because no own detail
contract is accepted; fragment/search links and outbound redirects are not
country or identity proof. AutoTrack resolution requires the publication's
own vehicle and dealer evidence, not related inventory.

`quality.py` measures field presence and confirmations from each field's
original `observed_at`. Listing `last_seen` alone cannot renew inherited facts.
Stored raw paths and hashes are diagnostic metadata; verifying the saved bytes
is a separate acceptance check. See [reliability.md](reliability.md).

## Origins

Extracted on 2026-10-10 from an earlier private research collector. Historical BMW/Mercedes modules and public fixtures
remain to test earlier contracts. `sources*.json` in `tests/fixtures/legacy-config/`
are historical regression configurations, not settings for a new user.
The public CLI creates its own `search.json`.
The operational services from the earlier research still use their original
code and external data; they were not migrated to this repository.

## Query and detail evidence boundaries

Generic AutoScout searches bind explicit inventory filters and their native
route before crediting publication IDs or completeness. Pagination retains
that binding. A redirect is checked against the original requested filters as
well as the final own response; rejected roots retain raw evidence and cannot
certify an empty result. This does not establish a national inventory census.

Specialized BMW detail getters share each run's full/fast allowance with the
generic detail stage. Accepted cached detail evidence retains its exact fetch
ID and original clock, requires verified raw bytes and current parser cutoffs,
and cannot renew its TTL through an unaccepted identical response. Persisted
403/429 backoff covers the actual requested/final host across restarts. Disabling
details prevents fresh specialized detail requests while keeping native search
IDs. Persistent source rotation gives each source a turn when the shared detail
budget is small. New conclusive country, make, model or VIN contradictions
remain unresolved or excluded instead of inheriting an older target decision.

These specialized adapters are not added to the generic source registry by this
change. Arbitrary-model support still follows the declared registry and native
evidence contracts above.

Configured detail planning serves four eligible target slots for every one
confirmation slot while both classes have work. An exhausted class yields its
slots to the other. Each class keeps a durable source cursor, and the verification
phase advances only after an actual response attempt. A one-attempt budget still
serves confirmation within five eligible attempts across restarts. Cache, access
backoff and full/fast/shared limits apply before selection. The complete candidate
map and unknown-country pending records keep their own original evidence.

Generic detail dispatch uses one request in flight so denial or interruption can
settle the next slot against actual completed attempts. This reduces detail
concurrency when workers is greater than one. Search and legacy detail dispatch
retain their existing concurrency. Recorded response attempts checkpoint source
cursors and verification debt before parsing, including failed responses.
