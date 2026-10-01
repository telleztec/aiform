# specs/resource_tagging.md — reserved tags and markers for `aiform/driver.py`, `aiform/orchestrator.py` and the three DigitalOcean drivers

**Naming note**: this filename does not follow `specs/README.md`'s one-spec-per-module rule. It adds one small shared
capability to `aiform/driver.py` (specced in `specs/driver.md`), with one
change in `aiform/orchestrator.py` and one integration in each of
`drivers/digitalocean/{compute,firewall,domain}.py` (specced in their own
`specs/digitalocean_*.md`). Each of those specs cross-references this one.

Plan: `plans/resource-tags.md` (#249). Probe session:
`knowledge/drivers/digitalocean_resource_tags/FINDINGS.md`; citations below
are transcript numbers in `probes/transcripts/digitalocean_resource_tags/`.

## Purpose

Every provider resource aiform creates carries the name of the deployment that
created it (#201's `State.deployment`) and an aiform marker. Before this,
a droplet from deployment `a` looked the same on DigitalOcean as one from
deployment `b`, and nothing marked any resource as aiform's. This answers
"what has aiform created in this account, and for which deployment" from the
provider side, independent of a `state.json` that may be lost or never written
(a crash between `create()` and the state write).

## The reserved tags

- `aiform-managed` — the marker.
- `aiform:<name>` — `<name>` is the deployment name, verbatim (no word
  "deployment"; example `aiform:prod`). `PLAN.md` §10's future
  `aiform:<short-uuid>:...` shares the prefix; its first segment is this
  deployment name.

"Reserved" means: exactly `aiform-managed`, or any string beginning
`aiform:`. The check is case-sensitive: DigitalOcean keeps a tag's case and
treats names case-sensitively (`03`, `04`).

## Interface

```python
# aiform/driver.py
AIFORM_MANAGED_TAG = "aiform-managed"
DEPLOYMENT_TAG_PREFIX = "aiform:"


def reserved_tags(deployment: str) -> tuple[str, str]:
    """(AIFORM_MANAGED_TAG, f"aiform:{deployment}")"""


def is_reserved_tag(tag: str) -> bool: ...


class ResourceDriver(ABC):
    def __init__(self, reserved_tags: Sequence[str] = ()) -> None: ...

    # self.reserved_tags: tuple[str, ...]
    # self._deployment_tag: str | None  -- the reserved tag beginning "aiform:"

    def _reject_reserved_tags(self, requested_tags: Sequence[str]) -> None: ...
    def _tags_for_create(self, requested_tags: Sequence[str]) -> list[str]: ...
    def _tags_for_attributes(self, live_tags: Sequence[str]) -> list[str]: ...
```

```python
# aiform/orchestrator.py
def load_driver(provider, resource_type, reserved_tags=()) -> ResourceDriver
```

`load_driver` returns `module.Driver(reserved_tags=reserved_tags)`.

The orchestrator computes `reserved_tags(st.deployment)` and passes it at every
call site that holds a `State`: `_driver_for` (create, update, replace),
`refresh_state`, and `_apply_destroy`. `aiform/observability.py` calls
`load_driver` without it: `health()` and `metrics()` never create, update or
return `tags`. A driver constructed without reserved tags (a test, a script)
attaches nothing, and still strips and rejects: stripping and rejecting depend
only on `is_reserved_tag`, not on the instance's tags.

The base class applies the tags; the orchestrator does no tag logic beyond
computing and passing them. So a new driver inherits the constructor and
cannot forget to receive them. What it still has to do with them is a
per-resource-kind decision (below), and `prompts/review_driver.md` checks it.

`_tags_for_create(requested)`: raises `ValueError` naming the first reserved
tag in `requested` (`_reject_reserved_tags`), else returns
`[*requested, *self.reserved_tags]`.

`_tags_for_attributes(live)`: returns `live` without any reserved tag,
unconditionally. Both reserved tags (not just this deployment's) are stripped,
so a foreign `aiform:other` on a resource never reaches the diff engine either.

## Per resource kind

DigitalOcean can tag only droplets, images, volumes, volume snapshots and
databases. A firewall and a domain cannot carry a tag (`11`, `13`, `14`, `16`,
`17`, `29`; `knowledge/drivers/digitalocean_resource_tags/FINDINGS.md`).

- **Droplet** — carries both tags.
  - `create()` sets `body["tags"] = self._tags_for_create(params.get("tags", []))`
    unconditionally, so the tags are attached even when the user's file never
    mentions `tags`. DigitalOcean auto-creates a tag named in a droplet create
    (documented; not probed here, since a droplet bills).
  - `_flatten()` returns `"tags": self._tags_for_attributes(...)`. `create()`,
    `read()` and `update()` all build attributes through it, so one change
    covers all three. `update()` must not echo `tags` from `desired`/`current`
    (it would discard live state).
  - `update()` computes tag removals from `current["tags"]`, which is
    stripped, so a reserved tag can never be removed. A `tags` value in
    `desired` containing a reserved tag raises `ValueError`, before any
    mutation.
- **Firewall** — cannot carry a tag. aiform puts the deployment name in the
  firewall's *name*, and only for a firewall `create()`s from now on:
  `aiform-<deployment>-<name>`, with `_` in the deployment name written as `-`
  because a firewall name rejects `_` (`23`; `:` is also rejected, `21`; `.`,
  uppercase and a leading digit are accepted, `24`–`26`; 255 characters is the
  limit, `27`, `28`). That mapping is lossy (`a_b` and `a-b` give the same
  name); the name is for a human and for a sweep to read, not to parse back.
  `update()` keeps the live name (`current["name"]`), so a firewall created
  before this change keeps its old name. A firewall's own `tags` parameter is a
  selector for droplets (`09`, `10`), not a label on the firewall, so it is not
  checked for reserved tags: `aiform:prod` is a useful selector for "every
  droplet in deployment prod".
- **Domain** — cannot carry a tag. aiform adds one TXT record at the zone apex,
  `name: "@"`, `data: "aiform:<deployment>"`, when `create()` builds the zone.
  - The marker's TTL equals the TTL of the user's apex TXT records if there are
    any, else 1800 (the zone's default, `17`). A second TXT record at the apex
    makes DigitalOcean rewrite the first one's TTL to match the newer one
    (`18`–`20`); a differing marker TTL would otherwise show the user's own TXT
    as drifted.
  - `read()`, and the records `update()` reconciles against, drop an apex TXT
    record whose data begins `aiform:`, like the SOA and DigitalOcean's own
    nameservers. It never reaches the diff engine, and `update()` never deletes
    or edits it.
  - A user's own apex TXT record whose data begins `aiform:` raises
    `ValueError` naming it: `read()` would drop it, leaving it permanently
    missing from the diff.
  - A zone's name is its hostname, so the deployment name cannot be prefixed
    onto it.

## Behavior

- **Reserved tags never enter the diff engine.** `diff_attributes` compares
  `read()`'s output with the user's raw params. The reserved tags are added
  only on the wire, and stripped from everything a driver returns. A resource
  with the tags looks to the orchestrator as if this feature did not exist.
  Merging them into `resource_spec.params["tags"]` instead was rejected: a
  `tags` diff is applied in place (issue #77), so `plan` would propose
  re-adding a tag the driver strips straight back out, forever.
- **A user-supplied reserved tag raises `ValueError` naming it**, before any
  provider call (droplet `create()` and `update()`, domain `create()` and
  `update()`). Left alone, the tag would be stripped from every read while
  still in the user's params: a permanent `tags` mismatch.
- **Updates never remove a reserved tag or the apex marker.**
- **Replace.** A replace is `delete()` then `create()` on the same instance, so
  the new resource gets the tags.
- **Zero extra calls on the plan/apply hot path.** The tags ride in the same
  droplet create request; the firewall name is part of the same create request;
  the TXT marker is one extra `POST /v2/domains/{name}/records` at zone
  creation. Nothing touches an LLM.

## No backfill

Resources created before this change are not labeled, and nothing labels them
later. `read()` and `update()` do not add a tag, a name or a marker to a
resource that lacks one. A firewall or zone without the marker stays that way;
a droplet created before this change has no reserved tag and `update()` does
not add them. The only way to label an existing resource is to destroy and
recreate it. A sweep or audit built on these tags has no signal for such a
resource.

## Edge cases / errors

- **`update()`'s replace path** calls `create()` with the same
  `desired_params`, which can never legitimately contain a reserved tag (it was
  rejected on the first create), so the check has nothing to trigger on.
- **System test.** `specs/system_test.md`'s own `aiform-system-test` tag is
  neither equal to `aiform-managed` nor prefixed `aiform:`, so it is a
  user-visible tag like any other.
- **A tag DigitalOcean cannot store.** A deployment name matches
  `^[a-z0-9][a-z0-9_-]{0,62}$`, so `aiform:<name>` always fits DigitalOcean's tag
  charset (letters, digits, `_`, `-`, `:`).
- **A driver with no tagging primitive.** It receives the tags and does nothing
  with them; this is a per-driver decision, recorded in that driver's spec.
- **A tag with no resources persists** until deleted (`02`, `29`). What
  DigitalOcean does to a tag when its last droplet is deleted was not observed.
  aiform does not delete reserved tags.

## Out of scope

- The command that deletes orphans by tag.
- Any backfill of existing resources (see above).
- `PLAN.md` §10's `<state-incarnation-no>` and `<owner-id>` segments.
- A CLI surface that lists resources by tag.
