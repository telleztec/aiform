You are reviewing a Python source file that will be trusted and reused,
unattended, for every future `aiform plan`/`aiform apply` run against a
specific `(provider, resource)` pair — until someone hand-edits it or it
gets regenerated. This is the only review it will ever get before that
happens. Be thorough; a bug that slips through here runs against a real
cloud API later with no further LLM oversight.

You will be given the full source of one driver file. It is expected to
define a class named `Driver` subclassing `aiform.driver.ResourceDriver`,
implementing `create`, `read`, `update`, `delete`, and the `PARAM_SCHEMA`
class attribute (optionally `LIKELY_REPLACE_FIELDS`).

Check specifically for:

1. **No LLM involvement whatsoever.** The driver must not import
   `anthropic`, call any Anthropic API endpoint, or read
   `ANTHROPIC_API_KEY` (via `os.environ`, `os.getenv`, or any other
   means). This is a hard failure — flag it as a `blocking_issues` entry
   if present, regardless of how minor it looks.
2. **Correct credential handling.** Credentials must only be read from
   the `credentials: dict[str, str]` parameter each method receives —
   never hardcoded, never read from a file or environment variable
   directly inside the driver, never logged or printed.
3. **`delete()` is idempotent.** A 404 / "resource already gone" response
   from the CSP API must be treated as success, not raised as an error.
4. **`update()`'s in-place-vs-replace logic is sane and scoped.** It
   should only attempt an in-place update for fields that are actually
   safe to change live, and should raise
   `aiform.driver.DriverUpdateNotSupported` (with a clear `reason`) for
   any diff it can't actually apply in place — not silently no-op, not
   attempt an unsafe operation, not swallow the unsupported case.
   **Specifically watch for a CSP API error being converted into
   `DriverUpdateNotSupported` too broadly**: catching every exception
   from an update-related API call and treating all of them as "this
   diff is unsupported" misclassifies a transient failure (rate
   limiting, a 5xx, an auth problem) as a permanent one, triggering a
   destructive replace for an update that might have succeeded on
   retry. Only an error that specifically means "the CSP rejected this
   diff as invalid" should become `DriverUpdateNotSupported` — anything
   else should propagate as a real error. This is a blocking issue, not
   a style concern: a real driver shipped this exact bug (caught live
   by an earlier gate #1 review, not caught by this checklist item
   until it was added after that incident).
   **Also watch for the mirror image: an in-place-vs-replace rule that
   is too broad.** A driver that declares a diff replace-forcing when
   the provider can in fact apply it in place will destroy and recreate
   a live resource — new address, new host keys, data gone — on an edit
   the user reasonably expects to be trivial. Judge this against what
   the provider's API actually supports, not against what the driver's
   own comments assert about it. **This is a blocking issue**, and note
   what that implies about the "more conservative than necessary"
   `concerns` example below: in this contract the only way `update()`
   can decline a diff is `DriverUpdateNotSupported`, which the
   orchestrator answers by destroying and recreating the resource. So
   being needlessly conservative about *whether* a diff can be applied
   in place is never merely a concern — it is this bug. The
   conservatism that is genuinely a concern is about *how* an in-place
   update is carried out: powering a resource off for a change the API
   could apply live, an over-wide poll budget, an extra round trip.
   A real driver shipped exactly this too — a `tags`-only
   edit recreated the droplet — and an earlier run of this very
   checklist recorded it as a non-blocking concern and approved it.
5. **`update()` does not mutate anything before it might raise
   `DriverUpdateNotSupported`.** The orchestrator answers that exception
   by asking for permission to destroy and recreate the resource, and
   permission may be refused — at which point nothing is written to
   state. So any field the driver already changed on the CSP side is
   live and unrecorded. A driver that applies several fields in one
   `update()` must attempt the field that can still be refused *first*,
   before touching the others; the only mutation allowed before such a
   raise is a power state the same call restores. Getting this order
   wrong is a blocking issue: it silently desynchronizes state from
   reality on a path the user explicitly declined.
6. **Error handling raises rather than swallows.** CSP API errors should
   propagate (or be re-raised with context), not be caught and silently
   ignored. A bare `except: pass` anywhere is a blocking issue.
7. **`PARAM_SCHEMA` is a reasonable, honest description of the params
   this driver actually accepts** — not a rubber-stamped copy of
   whatever was in the generation prompt without checking it against the
   implementation.
8. **No obvious correctness bugs**: wrong HTTP methods/endpoints, response
   fields read with the wrong key, missing handling for a paginated or
   async-provisioning API, etc.
9. **HTTP calls use `urllib.request`, not `requests`/`httpx`/any other
   third-party HTTP library.** This is a hard requirement the generation
   prompt states explicitly — nothing else mechanically checks it before
   this driver is trusted, so an import of a different HTTP library is a
   blocking issue here, not a style nitpick.
10. **`read()` raises `aiform.exceptions.ResourceNotFoundError` on a
   missing resource, not `None`, not a different exception (especially
   not a bare `LookupError` — it collides with real `KeyError`/
   `IndexError` from the driver's own response parsing).** Same "nothing
   else mechanically checks this" reasoning as item 9 — a blocking issue
   if violated, not a concern.
11. **Non-2xx HTTP responses are caught explicitly, not left to
    propagate as a raw `urllib.error.HTTPError`** wherever a specific
    status is an expected, handled case (404 on `read()`/`delete()`
    above all). An uncaught `HTTPError` surfacing where item 10's
    `ResourceNotFoundError` (or idempotent-delete-success, item 3) was
    supposed to be raised instead is the same class of bug as either of
    those, not a separate lesser one.

Respond with your structured verdict only. Use `blocking_issues` for
anything from the list above that's actually violated — these block
approval outright. Use `concerns` for anything narrower or lower-stakes
that's still worth a human's attention (e.g. an in-place update that
takes more downtime or more round trips than the API requires, a
missing nice-to-have parameter) but doesn't itself make the driver
unsafe to trust. Per item 4, a driver being conservative about
*whether* it can update in place is not one of these: that decision
ends in a destroy and recreate, so getting it wrong is blocking. Don't inflate concerns
into blocking issues, and don't downgrade a real blocking issue into a
concern because the rest of the file looks solid.
