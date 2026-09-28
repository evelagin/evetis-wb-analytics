"""WP3 — deterministic product resolver."""
from __future__ import annotations

import pytest

from app.v3.resolver import resolve

SINGLES = {"305101361": "EVT-FS-ACNE-30", "305101272": "EVT-FS-MOIST-30", "438775617": "EVT-FC-ACNE-50",
           "438775437": "EVT-FC-MOIST-50", "535581675": "EVT-FT-ACNE-150", "535581674": "EVT-FT-MOIST-150",
           "535580776": "EVT-EP-ENZYME-75", "252442517": "EVT-HC-HAND-300", "593111986": "EVT-HC-CHERRY-300",
           "593111985": "EVT-HC-AMBER-300"}
BUNDLES = {"252441968": {"EVT-HC-HAND-300", "EVT-HC-BODY-300"},
           "909951444": {"EVT-EP-ENZYME-75", "EVT-FS-ACNE-30", "EVT-FC-ACNE-50"},
           "868597351": {"EVT-EP-ENZYME-75", "EVT-FT-ACNE-150", "EVT-FS-ACNE-30", "EVT-FC-ACNE-50"},
           "910584041": {"EVT-FT-ACNE-150", "EVT-FS-ACNE-30", "EVT-FC-ACNE-50"},
           "952068582": {"EVT-FT-ACNE-150", "EVT-FC-ACNE-50"}, "567668635": {"EVT-FS-ACNE-30", "EVT-FC-ACNE-50"},
           "1083392113": {"EVT-FT-ACNE-150", "EVT-FS-ACNE-30"},
           "773170316": {"EVT-FT-MOIST-150", "EVT-FS-MOIST-30", "EVT-FC-MOIST-50"},
           "773170315": {"EVT-FT-MOIST-150", "EVT-FC-MOIST-50"}, "567668636": {"EVT-FS-MOIST-30", "EVT-FC-MOIST-50"},
           "910330849": {"EVT-FT-MOIST-150", "EVT-FS-MOIST-30"},
           "930334397": {"EVT-HC-CHERRY-300", "EVT-HC-AMBER-300"},
           "930334396": {"EVT-HC-HAND-300", "EVT-HC-AMBER-300"}, "930334395": {"EVT-HC-HAND-300", "EVT-HC-CHERRY-300"}}


@pytest.mark.parametrize("nm,pid", SINGLES.items())
def test_singles(snap, nm, pid):
    r = resolve(snap, nm_id=nm)
    assert (r.status, r.product_id, r.kind, r.method) == ("VERIFIED", pid, "single", "wb_nm_id")


@pytest.mark.parametrize("nm,comps", BUNDLES.items())
def test_bundles_resolve_to_components(snap, nm, comps):
    r = resolve(snap, nm_id=nm)
    assert r.kind == "bundle" and set(r.components) == comps
    assert snap.product(r.product_id)["facts"] == []   # component facts are never copied into bundles


def test_252442341_is_restricted(snap):
    r = resolve(snap, nm_id="252442341")
    assert r.status == "RESTRICTED" and r.product_id == "EVT-HC-BODY-300"


def test_252441968_is_a_bundle_not_a_hand_cream(snap):
    r = resolve(snap, nm_id="252441968")
    assert r.product_id == "EVT-SET-HAND-BODY" and r.restricted_components == ["EVT-HC-BODY-300"]


def test_unknown_identifier_never_resolves_by_name(snap):
    r = resolve(snap, nm_id="999999999", supplier_article="???")
    assert r.status == "PRODUCT_NOT_VERIFIED" and r.product_id is None


def test_vendor_code_fallback(snap):
    r = resolve(snap, nm_id=None, supplier_article="Крем Акне")
    assert r.product_id == "EVT-FC-ACNE-50" and r.method == "wb_vendor_code"


def test_conflicting_identifiers_are_not_guessed(snap):
    r = resolve(snap, nm_id="305101361", supplier_article="Крем Акне")
    assert r.status == "PRODUCT_NOT_VERIFIED" and "IDENTIFIER_CONFLICT" in r.reason


def test_other_marketplace_not_resolved(snap):
    assert resolve(snap, nm_id="305101361", marketplace="OZON").status == "PRODUCT_NOT_VERIFIED"
