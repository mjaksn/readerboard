"""Comparing a service's own description against the surface this client offers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from relayclient import catalogue
from relayclient.skew import UnreadableDescription, compare, paths_in, version_in

OPENAPI = Path(REPO_ROOT) / "docs" / "openapi.json"


def current() -> dict:
    """Return the description this checkout's service produces."""
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


def test_the_service_in_this_checkout_matches_the_client_in_this_checkout():
    difference = compare(current(), catalogue.OPERATIONS)
    assert difference.matches
    assert difference.uncallable == ()
    assert difference.unknown == ()


def test_the_summary_names_the_version_it_found():
    difference = compare(current(), catalogue.OPERATIONS)
    assert difference.version == current()["info"]["version"]
    assert difference.version in difference.summary()
    assert "surface matches" in difference.summary()


def test_an_older_service_missing_an_endpoint_is_reported_as_one_this_client_offers():
    document = current()
    del document["paths"]["/v2/enumerations/text-positions"]
    difference = compare(document, catalogue.OPERATIONS)
    assert not difference.matches
    assert ("GET", "/v2/enumerations/text-positions") in difference.unknown
    assert difference.uncallable == ()
    assert "1 this client offers that it does not have" in difference.summary()


def test_a_newer_service_with_an_extra_endpoint_is_reported_as_one_it_cannot_call():
    document = current()
    document["paths"]["/v2/sign/brightness"] = {"post": {}}
    difference = compare(document, catalogue.OPERATIONS)
    assert not difference.matches
    assert ("POST", "/v2/sign/brightness") in difference.uncallable
    assert difference.unknown == ()
    assert "1 it has that this client cannot call" in difference.summary()


def test_both_directions_are_reported_at_once():
    document = current()
    del document["paths"]["/health"]
    document["paths"]["/v2/sign/brightness"] = {"post": {}}
    difference = compare(document, catalogue.OPERATIONS)
    assert difference.unknown == (("GET", "/health"),)
    assert difference.uncallable == (("POST", "/v2/sign/brightness"),)


def test_the_detail_says_what_will_actually_go_wrong():
    document = current()
    del document["paths"]["/health"]
    detail = compare(document, catalogue.OPERATIONS).detail()
    assert "GET /health" in detail
    assert "404" in detail
    assert "Nothing is broken by this" in detail


def test_a_matching_detail_says_so_rather_than_listing_nothing():
    detail = compare(current(), catalogue.OPERATIONS).detail()
    assert "nothing is missing" in detail


def test_a_service_that_answered_something_else_entirely_is_refused():
    with pytest.raises(UnreadableDescription):
        paths_in({"nothing": "useful"})
    with pytest.raises(UnreadableDescription):
        paths_in("<html>not json</html>")
    with pytest.raises(UnreadableDescription):
        paths_in(None)


def test_a_description_without_a_version_still_compares():
    document = current()
    del document["info"]["version"]
    difference = compare(document, catalogue.OPERATIONS)
    assert difference.version == ""
    assert difference.summary().startswith("the service")


def test_the_version_is_read_from_wherever_it_is_and_nowhere_else():
    assert version_in({"info": {"version": "9.9.9"}}) == "9.9.9"
    assert version_in({"info": {}}) == ""
    assert version_in({}) == ""
    assert version_in("nonsense") == ""


def test_the_description_path_is_not_in_the_catalogue():
    # It is not part of the described surface, so listing it as an operation
    # would break the endpoint diff in test_catalogue.py for a path the
    # document never declares.
    assert "/openapi.json" not in {operation.path for operation in catalogue.OPERATIONS}
