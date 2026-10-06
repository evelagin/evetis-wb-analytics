"""R1: the live v2 publication gate keeps the production contract; v3.1E policy is shadow-only."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import publication_policy as pp
from app.v3 import pilot
from app.v3.registry import load_policy
from app.v3.snapshot import load_snapshot

SNAP = load_snapshot()
ST = SimpleNamespace(v3_knowledge_snapshot_id=SNAP.snapshot_id)
LIVE_VERIFIER = Path(pp.__file__).resolve().parents[1] / "v3" / "verifier_live.py"
MAIN_VERIFIER_BLOB = "52f6ba20f1dd86ff077a0e240db9d9aa10c50d99"   # main 3632bae app/v3/verifier.py

# Texts the shadow verifier deliberately judges differently from production (both directions).
DIVERGENT = [
    ("Крем имеет приятный аромат, рады, что Вам понравилось.", 593111985, "Аромат приятный.", "PASS", "BLOCK"),
    ("Без отдушек.", 535581674, "Отзыв", "PASS", "BLOCK"),
    ("Вы получили не тот заказ.", 252442517, "Можно ли вместо крема для лица?", "PASS", "BLOCK"),
    ("Применение на лице не подтверждено.", 252442517, "Можно на лицо?", "BLOCK", "PASS"),
]


def comm(nm, text):
    return {"nm_id": nm, "text": text, "entity_type": "review"}


def test_live_verifier_is_production_byte_for_byte():
    data = LIVE_VERIFIER.read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == MAIN_VERIFIER_BLOB


def test_live_gate_is_default_and_flag_free(monkeypatch):
    monkeypatch.delenv("V31_ENFORCE_LIVE_PUBLICATION_POLICY", raising=False)
    s = Settings()
    assert s.v31_enforce_live_publication_policy is False
    assert pp.live_publication_validator(s) is pp.validate_live_publication


@pytest.mark.parametrize("r1_on", [False, True])
@pytest.mark.parametrize("answer,nm,src,live,shadow", DIVERGENT)
def test_r1_flags_never_change_live_verdict(r1_on, answer, nm, src, live, shadow):
    # A/B/D: same live verdict with R1 off or on; the shadow verdict differs on purpose.
    settings = SimpleNamespace(v3_knowledge_snapshot_id=SNAP.snapshot_id, v31_quality_shadow_enabled=r1_on,
                               v31_shadow_activation_id="r1", v31_shadow_start_at="2026-10-07T00:00:00+00:00",
                               v31_shadow_end_at="2026-10-21T00:00:00+00:00",
                               v31_shadow_pilot_max_communications="20")
    gate = pp.live_publication_validator(settings)
    assert gate is pp.validate_live_publication
    assert gate(answer, comm(nm, src), settings)["verdict"] == live
    assert pp.validate_for_publication(answer, comm(nm, src), settings)["verdict"] == shadow   # C


def test_live_result_marks_its_gate():
    r = pp.validate_live_publication("Спасибо за отзыв!", comm(252442517, "Отзыв"), ST)
    assert r["gate"] == "LIVE_V2" and r["snapshot"] == SNAP.snapshot_id


def test_enforcing_v31_on_live_path_blocks_r1_activation():
    s = SimpleNamespace(v31_quality_shadow_enabled=True, v31_enforce_live_publication_policy=True,
                        v31_shadow_activation_id="r1", v31_shadow_start_at="2026-10-07T00:00:00+00:00",
                        v31_shadow_end_at="2026-10-21T00:00:00+00:00", v31_shadow_pilot_max_communications="20")
    assert pp.live_publication_validator(s) is pp.validate_for_publication
    assert pilot.activation(s) == (None, pilot.OPERATOR_SURFACES_ON)


def _publish(r1_on, answer, nm, src):
    """E: the unchanged verified publisher, driven through its normal callback path."""
    from app.services.pipeline import handle_update, run_poll
    from tests.conftest import SAMPLE_FEEDBACK, make_deps
    fb = copy.deepcopy(SAMPLE_FEEDBACK)
    fb.update(text=src, productDetails={**fb["productDetails"], "nmId": nm, "supplierArticle": str(nm)})
    extra = dict(v31_quality_shadow_enabled=True, v31_shadow_activation_id="r1",
                 v31_shadow_start_at="2026-10-07T00:00:00+00:00",
                 v31_shadow_end_at="2026-10-21T00:00:00+00:00") if r1_on else {}

    class Fixed:
        def generate_answer(self, *a):
            from app.domain.models import GenerationResult
            return GenerationResult(answer, "fake", "reviews_v1", {}, 0, "")
    d = make_deps([fb], openai=Fixed(), **extra)
    d.publication_validator = None                       # the real production adapter
    run_poll(d)
    doc_id = next(iter(d.repo.docs))
    version = d.repo.get(doc_id)["generation_number"]
    cq = {"id": "cq", "from": {"id": 302044578}, "data": f"pub:{doc_id}:{version}",
          "message": {"message_id": int(d.repo.get(doc_id)["telegram_message_id"]), "chat": {"id": 302044578}}}
    result = handle_update(d, {"update_id": 1, "callback_query": cq})
    return result, d


@pytest.mark.parametrize("answer,nm,src,live,shadow", DIVERGENT)
def test_live_publish_path_uses_production_gate_with_r1_on_and_off(answer, nm, src, live, shadow):
    # The publish outcome follows the production verdict in both directions, never the shadow one.
    expected = ("published", 1) if live == "PASS" else ("policy_blocked", 0)
    for r1_on in (False, True):
        result, d = _publish(r1_on, answer, nm, src)
        assert (result.get("status"), len(d.wb.published)) == expected


def test_owner_override_and_auto_publish_stay_off(monkeypatch):
    monkeypatch.delenv("V31_OWNER_OVERRIDE_ENABLED", raising=False)
    assert Settings().v31_owner_override_enabled is False                      # F
    assert load_policy()["runtime"]["auto_publish"] is False                   # G


def test_owner_override_requires_live_v31_policy():
    from app.services.owner_override import enabled
    base = dict(v31_owner_override_enabled=True)
    assert enabled(SimpleNamespace(**base)) is False                           # R1: production live gate
    assert enabled(SimpleNamespace(**base, v31_enforce_live_publication_policy=True)) is True
