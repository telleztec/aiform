# specs/digitalocean_pagination.md — shared page-following helper for DigitalOcean drivers (`drivers/digitalocean/_common.py`)

**Naming note**: like `specs/resource_tagging.md`, this filename deliberately
doesn't follow `specs/README.md`'s strict per-module mirroring rule
(`drivers/digitalocean/_common.py` → `specs/digitalocean__common.md`, which is
both ugly and uninformative). That rule assumes a spec maps to one
implementation module named after a resource; this one adds a small shared
capability used by every DigitalOcean driver. Named for the feature instead,
and cross-referenced from `specs/digitalocean_domain.md`, its first consumer.

## Purpose

Fetch every item of a paginated DigitalOcean collection, following
`links.pages.next` to exhaustion, so a driver never silently sees a truncated
list.

## Why this exists as a shared module

`GET /v2/domains/$DOMAIN_NAME/records` returns `per_page=20` by default
(maximum 200) — verified against `digitalocean/openapi`,
`specification/shared/parameters.yml`. A driver that issues one naive GET and
reads `payload["domain_records"]` sees the first 20 records of a 34-record zone
and reports the other 14 as absent. Routed through `read()`, that becomes a
plan proposing to **delete 14 live DNS records**, and an `apply` would execute
it.

Every future DigitalOcean driver hits this the moment its resource has a
collection, and `drivers/digitalocean/compute.py` — the only existing driver —
has no pagination anywhere to copy, because a single droplet GET needs none.
So this is written once, specced and tested on its own, rather than
re-implemented (and re-broken) per driver.

**Why not a Python SDK.** `pydo`/`requests`/`httpx` are all excluded:
`prompts/review_driver.md` item 9 makes any non-`urllib.request` HTTP library a
**blocking** gate #1 failure — for a freshly-drafted driver reviewed inside
`driver_gen.py`. That rule exists so tests can mock one known stdlib seam
(`urllib.request.urlopen`) instead of guessing which library a driver reached
for — see `specs/digitalocean_compute.md`'s HTTP client convention. It is
enforced for a hand-authored module like this one only via `PROCESS.md`'s
PR-time `/code-review`: issue #119 removed gate #1's `plan`/`apply`-time
re-review of a hand-edited driver entirely, so there is no automated review
of hand-authored driver code left at all any more. Before #119 that
re-review reached only a driver's single top-level file (`domain.py`, on a
hash mismatch) and never this helper — the bug #119 fixed. An earlier
version of this note reasoned that the exemption was because this helper
"performs no I/O itself"; the real reason was always mechanical (this
module was never the file gate #1 hashed), and after #119 the whole
question is moot for every hand-authored file, I/O or not.

**Why not a method on `ResourceDriver`.** `links.pages.next` is DigitalOcean's
convention, not a universal one — AWS uses `NextToken`, others use `Link`
headers or cursors. Putting a DO-shaped paginator on the provider-agnostic base
class in `aiform/driver.py` would be an abstraction built for a generality that
doesn't exist yet, which `CLAUDE.md` explicitly forbids. Provider-shared
helpers live under `drivers/<provider>/`; this spec establishes that pattern.

**Why the leading underscore in `_common.py`.** `orchestrator.load_driver()`
resolves a driver as `drivers/<provider>/<resource_type>.py`, and
`resource_type` is validated against `models.RESOURCE_OR_PROVIDER_PATTERN`
(`^[a-z][a-z0-9_]*$`), which does not permit a leading underscore. So no
`.aiform.md` can ever name `_common` as a resource kind and have this module
loaded as a driver. The underscore is load-bearing, not stylistic.

## Interface

```python
# drivers/digitalocean/_common.py

DEFAULT_PER_PAGE = 200
MAX_PAGES = 100


def fetch_all_pages(
    fetch: Callable[[str], dict | None],
    url: str,
    collection: str,
    *,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> list[Any]:
    """Return every item across all pages of a DigitalOcean collection."""
```

- **`fetch`** — a callable taking one absolute URL and returning the parsed JSON
  body (or `None` for an empty body). The caller supplies it, typically as
  `lambda u: self._request("GET", u, credentials)`. **This module performs no
  I/O and imports nothing from `urllib`.** That is what keeps it trivially
  unit-testable with a plain list of dicts, keeps credentials out of it
  entirely, and keeps it outside the `urllib.request`-only rule's scope.
- **`url`** — the first page's URL, without pagination query parameters.
- **`collection`** — the response key holding the list, e.g. `"domain_records"`
  or `"droplets"`. Passed explicitly rather than inferred, because DO's key
  names don't derive mechanically from the path.
- **`per_page`** — appended to `url` as a query parameter. Defaults to
  `DEFAULT_PER_PAGE = 200`, DigitalOcean's documented maximum, so the common
  case costs one round trip instead of ten.
- **Returns** a plain `list`, not a generator. A generator would let a caller
  abandon iteration midway holding a half-read collection, which for `read()`
  is the exact truncation bug this module exists to prevent.

## Behavior

- **Single page.** A response with no `links` key, an empty `links`, an empty
  `links.pages`, or a `links.pages` carrying only backward links (`first`,
  `prev`) returns that page's items and makes exactly one call. All four shapes
  occur — the `page_links` schema's `pages` is `anyOf: [forward_links,
  backward_links, {}]`, so absence of `next` is the terminator, and the last
  page of a multi-page walk carries backward links specifically.
- **Multiple pages.** Follows `links.pages.next` — an absolute URI, per the
  schema's own examples — accumulating `payload[collection]` in encounter
  order, until no `next` is present. Order is preserved exactly as returned;
  this module never sorts.
- **`per_page` injection.** Appended to `url` before the first request using
  `urllib.parse` to parse and re-encode the query string, never string
  concatenation, so a `url` that already carries query parameters (for example
  the `?type=A` filter DO's record listing supports) keeps them. If `url`
  already specifies `per_page`, the caller's value wins and nothing is
  injected. `next` URLs are followed **verbatim** — DO already encodes the
  correct `per_page` and `page` into them, and rewriting them risks desyncing
  the walk.
- **Missing collection key.** A page whose payload lacks `collection`
  contributes nothing rather than raising, using `payload.get(collection) or
  []`. DO returns an omitted or null list for an empty collection, and a
  `KeyError` there would surface as an opaque failure inside `read()`.
- **Empty body.** `fetch` returning `None` (the `_request` convention for a
  body-less response) terminates the walk and contributes nothing.

## Edge cases / errors

- **Runaway pagination.** The walk stops after `max_pages` (default 100 —
  20,000 items at the default `per_page`) and raises `RuntimeError` naming the
  URL and the cap. Guards against a malformed or self-referential `next` that
  would otherwise loop forever inside a `plan`, holding the run open against a
  live API with no ceiling. A cap that trips is a bug worth surfacing loudly,
  not a limit to silently truncate at — which is why it raises rather than
  returning what it has, the one behavior this module must never exhibit.
- **A `next` pointing off-host.** Any `next` URL whose scheme+host is not
  `https://api.digitalocean.com` raises `ValueError` rather than being
  followed. The response body is attacker-influencable in principle (it echoes
  record data), and blindly following a URL out of a JSON body would forward
  the caller's `Authorization: Bearer` token to an arbitrary host. Cheap to
  check, and the check is the reason `fetch` receives a full URL rather than
  this module building one.
- **HTTP errors propagate untouched.** `fetch` raising
  `urllib.error.HTTPError` (or anything else) is not caught here. Translating a
  404 into `ResourceNotFoundError` is the *driver's* job, on the first request,
  where it knows which resource is missing — see
  `specs/digitalocean_domain.md`'s `read()`. Swallowing errors here would break
  `prompts/review_driver.md` item 6.
- **`per_page` out of range.** Not validated. DO's own documented bounds are
  1–200 and it rejects anything else with a 422 that propagates as a normal
  HTTP error; a local re-check would be a second source of truth for a
  constraint the API already enforces.

## Out of scope

- **Migrating `drivers/digitalocean/compute.py` to use this.** That driver
  makes no paginated calls today, so there is nothing to migrate. Lifting its
  `_request()` into this module so both drivers share one HTTP helper is a real
  follow-up, deliberately not bundled here: `compute.py` is already merged and
  reviewed, and re-opening it would widen this PR past `PROCESS.md`'s one
  module boundary.
- **A generic, provider-agnostic paginator.** DigitalOcean only, by design —
  see "Why not a method on `ResourceDriver`" above. A second provider with a
  different pagination shape is the point to reconsider, informed by a real
  second case rather than a guessed one.
- **Rate-limit handling, retries, or backoff.** No driver in this repo retries
  anything yet (`specs/system_test.md` notes the same for `compute.py`); a 429
  propagates as an ordinary `HTTPError`. Adding retry semantics here alone
  would make paginated calls behave differently from every other call a driver
  makes, which is worse than uniform absence.
- **Backward (`prev`/`first`) traversal, or resuming mid-collection.** Nothing
  needs it; `read()` always wants the whole collection.
