# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Tests for drivers/digitalocean/_common.py's fetch_all_pages() --
see specs/digitalocean_pagination.md. This module does no I/O and
imports nothing from urllib, so `fetch` is a plain caller-supplied
callable and none of these tests need any HTTP mocking -- unlike
tests/drivers/test_digitalocean_compute.py's FakeUrlopen harness.
"""

import urllib.parse

import pytest

from drivers.digitalocean._common import fetch_all_pages

RECORDS_URL = "https://api.digitalocean.com/v2/domains/example.com/records"


class FakeFetch:
    """Records the urls fetch_all_pages calls it with, in order, and
    returns one scripted response per call, popped off the front of the
    queue. A scripted response that is an Exception instance is raised
    instead of returned, mirroring FakeUrlopen's convention in
    test_digitalocean_compute.py."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def query(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


class TestModuleConstants:
    def test_default_per_page_and_max_pages_constants(self):
        from drivers.digitalocean._common import DEFAULT_PER_PAGE, MAX_PAGES

        assert DEFAULT_PER_PAGE == 200
        assert MAX_PAGES == 100


class TestSinglePageShapes:
    def test_no_links_key_at_all(self):
        fake = FakeFetch({"domain_records": [{"id": 1}, {"id": 2}]})

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}, {"id": 2}]
        assert len(fake.urls) == 1

    def test_links_present_but_empty(self):
        fake = FakeFetch({"domain_records": [{"id": 1}], "links": {}})

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}]
        assert len(fake.urls) == 1

    def test_links_pages_empty(self):
        fake = FakeFetch({"domain_records": [{"id": 1}], "links": {"pages": {}}})

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}]
        assert len(fake.urls) == 1

    def test_links_pages_with_only_backward_links(self):
        # The shape a multi-page walk's LAST page carries: first/prev but
        # no next -- absence of `next` is the terminator, per the schema's
        # own anyOf[forward_links, backward_links, {}].
        fake = FakeFetch(
            {
                "domain_records": [{"id": 1}],
                "links": {
                    "pages": {
                        "first": f"{RECORDS_URL}?page=1",
                        "prev": f"{RECORDS_URL}?page=1",
                    }
                },
            }
        )

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}]
        assert len(fake.urls) == 1


class TestMultiPage:
    def test_follows_next_accumulating_in_encounter_order(self):
        page2_url = f"{RECORDS_URL}?page=2&per_page=200"
        page3_url = f"{RECORDS_URL}?page=3&per_page=200"
        fake = FakeFetch(
            {
                "domain_records": [{"id": 1}, {"id": 2}],
                "links": {"pages": {"next": page2_url}},
            },
            {
                "domain_records": [{"id": 3}],
                "links": {"pages": {"next": page3_url}},
            },
            {
                "domain_records": [{"id": 4}],
                "links": {"pages": {"first": RECORDS_URL, "prev": page2_url}},
            },
        )

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
        assert len(fake.urls) == 3
        assert fake.urls[1] == page2_url
        assert fake.urls[2] == page3_url


class TestPerPageInjection:
    def test_injected_into_first_url_only_preserving_existing_query(self):
        base_url = f"{RECORDS_URL}?type=A"
        fake = FakeFetch({"domain_records": []})

        fetch_all_pages(fake, base_url, "domain_records")

        q = query(fake.urls[0])
        assert q["type"] == ["A"]
        assert q["per_page"] == ["200"]

    def test_custom_per_page_is_injected(self):
        fake = FakeFetch({"domain_records": []})

        fetch_all_pages(fake, RECORDS_URL, "domain_records", per_page=50)

        assert query(fake.urls[0])["per_page"] == ["50"]

    def test_url_already_specifying_per_page_wins_over_default(self):
        base_url = f"{RECORDS_URL}?per_page=20"
        fake = FakeFetch({"domain_records": []})

        fetch_all_pages(fake, base_url, "domain_records")

        q = query(fake.urls[0])
        assert q["per_page"] == ["20"]

    def test_next_urls_are_followed_verbatim_with_no_per_page_rewriting(self):
        # Deliberately carries no per_page at all, proving the module does
        # not inject or rewrite anything when following `next`.
        next_url = f"{RECORDS_URL}?page=2"
        fake = FakeFetch(
            {"domain_records": [{"id": 1}], "links": {"pages": {"next": next_url}}},
            {"domain_records": [{"id": 2}]},
        )

        fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert fake.urls[1] == next_url


class TestMissingCollectionOrEmptyBody:
    def test_missing_collection_key_contributes_nothing_and_does_not_raise(self):
        fake = FakeFetch({"links": {}})

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == []

    def test_fetch_returning_none_on_first_call_terminates_with_no_items(self):
        fake = FakeFetch(None)

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == []
        assert len(fake.urls) == 1

    def test_fetch_returning_none_on_a_later_call_terminates_and_keeps_prior_items(self):
        next_url = f"{RECORDS_URL}?page=2"
        fake = FakeFetch(
            {"domain_records": [{"id": 1}], "links": {"pages": {"next": next_url}}},
            None,
        )

        result = fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert result == [{"id": 1}]
        assert len(fake.urls) == 2


class TestMaxPages:
    def test_exceeding_max_pages_raises_runtime_error_naming_the_url(self):
        url2 = f"{RECORDS_URL}?page=2"
        url3 = f"{RECORDS_URL}?page=3"
        fake = FakeFetch(
            {"domain_records": [{"id": 1}], "links": {"pages": {"next": url2}}},
            {"domain_records": [{"id": 2}], "links": {"pages": {"next": url3}}},
        )

        with pytest.raises(RuntimeError) as excinfo:
            fetch_all_pages(fake, RECORDS_URL, "domain_records", max_pages=2)

        assert url3 in str(excinfo.value)
        # The offending (max_pages+1)th url is named but never fetched --
        # silent truncation is exactly the bug this module exists to
        # prevent, so it must raise rather than return partial results.
        assert len(fake.urls) == 2

    def test_result_is_never_returned_partially_on_a_runaway_walk(self):
        url2 = f"{RECORDS_URL}?page=2"
        fake = FakeFetch(
            {"domain_records": [{"id": 1}], "links": {"pages": {"next": url2}}},
        )

        with pytest.raises(RuntimeError):
            fetch_all_pages(fake, RECORDS_URL, "domain_records", max_pages=1)


class TestOffHostNextIsRejected:
    def test_next_url_on_a_different_host_raises_value_error_without_following(self):
        evil_url = "https://evil.example.com/v2/domains/example.com/records?page=2"
        fake = FakeFetch({"domain_records": [{"id": 1}], "links": {"pages": {"next": evil_url}}})

        with pytest.raises(ValueError):
            fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert len(fake.urls) == 1

    def test_next_url_with_a_different_scheme_raises_value_error(self):
        http_url = "http://api.digitalocean.com/v2/domains/example.com/records?page=2"
        fake = FakeFetch({"domain_records": [{"id": 1}], "links": {"pages": {"next": http_url}}})

        with pytest.raises(ValueError):
            fetch_all_pages(fake, RECORDS_URL, "domain_records")

        assert len(fake.urls) == 1


class TestFetchExceptionPropagates:
    def test_exception_from_fetch_propagates_untouched(self):
        fake = FakeFetch(RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            fetch_all_pages(fake, RECORDS_URL, "domain_records")

    def test_exception_on_a_later_page_propagates_untouched(self):
        next_url = f"{RECORDS_URL}?page=2"
        fake = FakeFetch(
            {"domain_records": [{"id": 1}], "links": {"pages": {"next": next_url}}},
            ValueError("second page exploded"),
        )

        with pytest.raises(ValueError, match="second page exploded"):
            fetch_all_pages(fake, RECORDS_URL, "domain_records")
