"""R2 operator assist: a v3.1E draft next to the v2 draft, published only by an explicit
tap through the unchanged verified publisher; never automatically."""
import copy
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.exceptions import InvalidTransition
from app.domain.models import GenerationResult
from app.services.pipeline import handle_update, run_poll
from app.v3 import pilot
from tests.conftest import SAMPLE_FEEDBACK, make_deps

V2_TEXT = "Людмила, спасибо! Масла ши и миндаля питают кожу рук."   # blocked by policy (unsupported claim)


class FixedV2:
    def __init__(self, text=V2_TEXT):
        self.text = text

    def generate_answer(self, *a):
        return GenerationResult(self.text, "fake-v2", "reviews_v1", {}, 0, "")


def fb(text="Очень хороший крем для рук. Руки мягкие, липкости нет.", nm=252442517, rating=5):
    f = copy.deepcopy(SAMPLE_FEEDBACK)
    f.update(text=text, productValuation=rating, userName="Людмила",
             productDetails={**f["productDetails"], "nmId": nm, "supplierArticle": str(nm)})
    return f


def deps(on=True, feedback=None, **extra):
    flags = dict(v31_operator_draft_enabled=True, v31_enforce_live_publication_policy=True) if on else {}
    d = make_deps([feedback or fb()], openai=FixedV2(), **flags, **extra)
    d.publication_validator = None          # the real production adapter (live_publication_validator)
    return d


def doc_of(d):
    doc_id = next(iter(d.repo.docs))
    return doc_id, d.repo.get(doc_id)


def tap(d, data, n=1):
    doc_id, doc = doc_of(d)
    cq = {"id": f"cq{n}", "from": {"id": 302044578}, "data": data,
          "message": {"message_id": int(doc["telegram_message_id"]), "chat": {"id": 302044578}}}
    return handle_update(d, {"update_id": n, "callback_query": cq})


def buttons(d):
    return [b["callback_data"] for row in d.telegram.sent[-1][2]["inline_keyboard"] for b in row]

# default OFF ------------------------------------------------------------------------------
def test_default_off_card_and_keyboard_unchanged(monkeypatch):
    monkeypatch.delenv("V31_OPERATOR_DRAFT_ENABLED", raising=False)
    assert Settings().v31_operator_draft_enabled is False
    d = deps(on=False)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert "v31_draft" not in doc
    assert "Вариант 3.1E" not in d.telegram.sent[-1][1]
    assert not any(c.startswith("p31:") for c in buttons(d))

# ON: draft prepared, shown, bound to generation --------------------------------------------
def test_card_shows_both_drafts_and_bound_buttons():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    draft = doc["v31_draft"]
    assert draft["status"] == "READY" and draft["text"] and draft["hard_verdict"] != "BLOCK"
    card = d.telegram.sent[-1][1]
    assert "Проект ответа" in card and "Вариант 3.1E" in card and draft["text"].split(",")[0] in card
    gen = doc["generation_number"]
    assert f"p31:{doc_id}:{gen}" in buttons(d) and f"pub:{doc_id}:{gen}" in buttons(d)
    assert d.wb.published == []                                      # nothing auto-published


def test_publish_v31_goes_through_verified_publisher():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    result = tap(d, f"p31:{doc_id}:{doc['generation_number']}")
    after = d.repo.get(doc_id)
    assert result["status"] == "published" and after["status"] == "published"
    assert len(d.wb.published) == 1 and d.wb.published[0][1] == doc["v31_draft"]["text"]
    assert after["final_answer"] == doc["v31_draft"]["text"]
    assert after["answer_versions"][-1]["source"] == "v31e"
    assert after["answer_versions"][0]["text"] == V2_TEXT              # v2 draft kept in history


def test_v2_button_still_uses_policy_and_blocks_unsupported_claim():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    result = tap(d, f"pub:{doc_id}:{doc['generation_number']}")
    assert result["status"] == "policy_blocked" and d.wb.published == []


def test_stale_card_and_double_tap_never_write_twice():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert tap(d, f"p31:{doc_id}:{gen - 1}")["status"] == "stale"        # card older than the doc
    assert d.wb.published == []
    assert tap(d, f"p31:{doc_id}:{gen}", n=2)["status"] == "published"
    assert tap(d, f"p31:{doc_id}:{gen}", n=3)["status"] == "stale"      # second tap on the same card
    assert len(d.wb.published) == 1


@pytest.mark.parametrize("status", ["publishing", "publish_unknown", "published", "skipped"])
def test_adoption_refused_outside_draftable_states(status):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    d.repo.docs[doc_id]["status"] = status
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == "stale"
    assert d.wb.published == [] and d.repo.get(doc_id)["final_answer"] == V2_TEXT


def test_adoption_requires_exact_stored_text():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    with pytest.raises(InvalidTransition):
        d.repo.adopt_v31_draft(doc_id, doc["generation_number"],
                               GenerationResult("другой текст", "x", "y", {}, 0, ""))


def test_human_review_route_offers_no_v31_button():
    d = deps(feedback=fb(text="После крема опух язык и тяжело дышать", rating=1))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "HUMAN_REVIEW" and not doc["v31_draft"].get("text")
    assert not any(c.startswith("p31:") for c in buttons(d))
    assert "нужна проверка человеком" in d.telegram.sent[-1][1]


def test_draft_failure_never_blocks_v2_card(monkeypatch):
    import app.response_quality.core as core
    monkeypatch.setattr(core, "prepare", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "ERROR" and len(d.telegram.sent) == 1
    assert not any(c.startswith("p31:") for c in buttons(d))


def test_poll_budget_skips_draft_and_regenerate_fills_it():
    d = deps(v31_operator_draft_budget_seconds=-1)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "SKIPPED_BUDGET"
    assert not any(c.startswith("p31:") for c in buttons(d))
    tap(d, f"regen:{doc_id}")
    assert d.repo.get(doc_id)["v31_draft"]["status"] == "READY"


def test_disabled_flag_rejects_old_v31_button():
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    d.settings.v31_operator_draft_enabled = False
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == "stale"
    assert d.wb.published == []


def test_r1_pilot_does_not_run_alongside_r2():
    s = SimpleNamespace(v31_quality_shadow_enabled=True, v31_operator_draft_enabled=True,
                        v31_shadow_activation_id="r1", v31_shadow_start_at="2026-10-07T00:00:00+00:00",
                        v31_shadow_end_at="2026-10-21T00:00:00+00:00", v31_shadow_pilot_max_communications="20")
    assert pilot.activation(s) == (None, pilot.OPERATOR_SURFACES_ON)


def test_owner_override_and_auto_publish_stay_off():
    from app.v3.registry import load_policy
    from app.services.owner_override import enabled
    assert enabled(SimpleNamespace(v31_enforce_live_publication_policy=True)) is False
    assert load_policy()["runtime"]["auto_publish"] is False


class _Snap:
    def __init__(self, d): self.exists, self._d = d is not None, d
    def to_dict(self): return copy.deepcopy(self._d)


class _Ref:
    def __init__(self, c): self.c = c
    def get(self, transaction=None): return _Snap(self.c.doc)


class _Tx:
    def __init__(self, c): self.c, self.updates = c, []
    def update(self, ref, fields): self.updates.append(dict(fields)); self.c.doc.update(fields)


class _Client:
    def __init__(self, doc): self.doc, self.tx = doc, None
    def transaction(self): self.tx = _Tx(self); return self.tx


def test_firestore_adoption_is_one_transaction(monkeypatch):
    from google.cloud import firestore
    from app.services.repository import FirestoreRepository
    monkeypatch.setattr(firestore, "transactional", lambda f: f)
    client = _Client({"status": "pending_approval", "generation_number": 2, "final_answer": V2_TEXT,
                      "answer_versions": [], "v31_draft": {"text": "Вариант 3.1E"}})
    repo = FirestoreRepository.__new__(FirestoreRepository)
    monkeypatch.setattr(repo, "_lazy", lambda: client)
    monkeypatch.setattr(repo, "_doc", lambda _: _Ref(client))
    gen = GenerationResult("Вариант 3.1E", "v3.1E", "v", {}, 0, "")
    with pytest.raises(InvalidTransition):
        repo.adopt_v31_draft("id", 1, gen)
    assert client.tx.updates == []
    after = repo.adopt_v31_draft("id", 2, gen)
    assert len(client.tx.updates) == 1 and after["generation_number"] == 3 and after["final_answer"] == "Вариант 3.1E"
    assert "status" not in client.tx.updates[0]                      # publishing lease is the publisher's job


def test_question_card_and_publish_via_question_publisher(monkeypatch):
    import app.services.pipeline as pl
    monkeypatch.setattr(pl.time, "sleep", lambda *_: None)
    q = {"id": "Q1", "createdDate": "2026-10-07T12:00:00Z", "text": "В какой стране изготовлен крем?",
         "productDetails": {"productName": "Крем для лица", "supplierArticle": "438775617", "nmId": 438775617,
                            "imtId": 1, "brandName": "EVETIS"}}
    d = make_deps(feedbacks=[], questions=[q], wb_questions_enabled=True, wb_question_publish_enabled=True,
                  openai=FixedV2("Страна — Китай, крем лечит акне."),
                  v31_operator_draft_enabled=True, v31_enforce_live_publication_policy=True)
    d.publication_validator = None
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["entity_type"] == "question" and doc["v31_draft"]["text"] == "Страна производства — Китай."
    assert "Вариант 3.1E" in d.telegram.sent[-1][1]
    result = tap(d, f"p31:{doc_id}:{doc['generation_number']}")
    assert result["status"] == "published"
    assert [t for _, t, _ in d.wb.published_questions] == ["Страна производства — Китай."]


def test_closed_publish_gate_changes_nothing():
    d = deps(wb_publish_enabled=False)
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == "publish_disabled"
    after = d.repo.get(doc_id)
    assert after["final_answer"] == V2_TEXT and after["generation_number"] == doc["generation_number"]
