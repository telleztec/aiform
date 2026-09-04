# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Shared page-following helper for DigitalOcean drivers -- see
specs/digitalocean_pagination.md. Performs no I/O itself: `fetch` is a
caller-supplied callable, so this module stays outside the
urllib.request-only rule's scope and is trivially unit-testable with
plain dicts.
"""

import urllib.parse
from collections.abc import Callable
from typing import Any

DEFAULT_PER_PAGE = 200
MAX_PAGES = 100

# The only host `next` is ever allowed to point at. A `next` URL comes
# from the response body, which is attacker-influencable in principle;
# following it blindly would forward the caller's Authorization: Bearer
# token to whatever host the body names.
_ALLOWED_HOST = "https://api.digitalocean.com"


def _inject_per_page(url: str, per_page: int) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if "per_page" in query:
        return url
    query["per_page"] = [str(per_page)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _check_host(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != _ALLOWED_HOST:
        raise ValueError(f"refusing to follow a next url off {_ALLOWED_HOST}: {url}")


def fetch_all_pages(
    fetch: Callable[[str], dict | None],
    url: str,
    collection: str,
    *,
    per_page: int = DEFAULT_PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> list[Any]:
    """Return every item across all pages of a DigitalOcean collection."""
    current_url = _inject_per_page(url, per_page)
    items: list[Any] = []
    pages_fetched = 0

    while True:
        payload = fetch(current_url)
        pages_fetched += 1
        if payload is None:
            break

        items.extend(payload.get(collection) or [])

        next_url = ((payload.get("links") or {}).get("pages") or {}).get("next")
        if not next_url:
            break

        _check_host(next_url)
        if pages_fetched >= max_pages:
            raise RuntimeError(
                f"fetch_all_pages exceeded max_pages={max_pages} while trying to "
                f"fetch {next_url}; refusing to return a partial result"
            )
        current_url = next_url

    return items
