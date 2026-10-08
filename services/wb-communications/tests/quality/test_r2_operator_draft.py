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
    # First R2 release: V31_ENFORCE_LIVE_PUBLICATION_POLICY stays OFF (v2 publication unchanged).
    flags = dict(v31_operator_draft_enabled=True) if on else {}
    extra.setdefault("openai", FixedV2())
    d = make_deps([feedback or fb()], **flags, **extra)
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


def buttons(d, keyboard=None):
    keyboard = keyboard or d.telegram.sent[-1][2]
    return [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]


def last_trace(d):
    return doc_of(d)[1]["publish_trace"][-1]


def frozen(doc):
    return {k: copy.deepcopy(doc.get(k)) for k in ("final_answer", "generation_number", "status",
                                                    "answer_versions", "publication_state")}


class Recorder:
    """Wraps a real policy function and records every call (publication text included)."""
    def __init__(self, fn, verdicts=None):
        self.fn, self.calls, self.verdicts = fn, [], list(verdicts or [])

    def __call__(self, text, doc, settings, **kw):
        self.calls.append(text)
        if self.verdicts:
            verdict = self.verdicts.pop(0)
            if isinstance(verdict, Exception):
                raise verdict
            return {"verdict": verdict, "violations": [], "text_sha256": "x"}
        return self.fn(text, doc, settings, **kw)


@pytest.fixture
def policies(monkeypatch):
    import app.services.publication_policy as pp
    live, v31 = Recorder(pp.validate_live_publication), Recorder(pp.validate_for_publication)
    monkeypatch.setattr(pp, "validate_live_publication", live)
    monkeypatch.setattr(pp, "validate_for_publication", v31)
    return SimpleNamespace(live=live, v31=v31)

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
                  v31_operator_draft_enabled=True)
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


# policy isolation: the action decides the policy, never the R2 flag -------------------------
def test_enforce_flag_stays_off_for_first_r2_release(monkeypatch):
    monkeypatch.delenv("V31_ENFORCE_LIVE_PUBLICATION_POLICY", raising=False)
    assert Settings().v31_enforce_live_publication_policy is False
    assert deps().settings.v31_enforce_live_publication_policy is False


def test_v2_button_uses_live_v2_gate_only(policies):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    tap(d, f"pub:{doc_id}:{doc['generation_number']}")
    trace = last_trace(d)
    assert trace["publication_mode"] == "live_v2" and trace["policy"]["gate"] == "LIVE_V2"
    assert policies.live.calls == [V2_TEXT] and policies.v31.calls == []


def test_manual_edit_uses_live_v2_gate_only(policies):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert tap(d, f"edit:{doc_id}")["status"] == "editing_started"
    edited = "Людмила, спасибо за отзыв! Рады, что крем понравился."
    reply = {"update_id": 9, "message": {"chat": {"id": 302044578}, "from": {"id": 302044578}, "text": edited,
                                         "reply_to_message": {"message_id": d.telegram._id}}}
    assert handle_update(d, reply)["status"] == "edited"
    doc = d.repo.get(doc_id)
    tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=10)
    trace = last_trace(d)
    assert trace["publication_mode"] == "live_v2" and trace["policy"]["gate"] == "LIVE_V2"
    assert policies.live.calls == [edited] and policies.v31.calls == []


def test_p31_uses_v31_policy_in_preflight_and_again_in_publisher(policies):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    v31_text = doc["v31_draft"]["text"]
    policies.v31.calls.clear()                                # generation-time checks are not publication
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert policies.v31.calls == [v31_text, v31_text]         # preflight + publisher re-check
    assert policies.live.calls == []
    trace = last_trace(d)
    assert trace["publication_mode"] == "v31" and "gate" not in trace["policy"]


# preflight: a draft the current policy blocks is never adopted -------------------------------
@pytest.mark.parametrize("verdict, status", [("BLOCK", "v31_preflight_blocked"),
                                             (RuntimeError("policy down"), "v31_preflight_failed"),
                                             ("NONSENSE", "v31_preflight_failed")])
def test_preflight_block_changes_nothing(policies, verdict, status):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "READY"              # READY when it was generated
    before, sent = frozen(d.repo.get(doc_id)), len(d.telegram.sent)
    policies.v31.verdicts = [verdict]                         # ...but the current policy says no
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == status
    after = d.repo.get(doc_id)
    assert frozen(after) == before and "publish_trace" not in after
    assert d.wb.published == [] and d.telegram.edits == []
    assert len(d.telegram.sent) == sent + 1 and "не проходит правила" in d.telegram.sent[-1][1]
    # the card stays usable: same generation, the V2 button still works on the live gate
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}", n=2)["status"] == "policy_blocked"


def test_publisher_rechecks_policy_before_wb_write(policies):
    d = deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    policies.v31.verdicts = ["PASS", "BLOCK"]                 # preflight passes, the publisher's check blocks
    assert tap(d, f"p31:{doc_id}:{doc['generation_number']}")["status"] == "policy_blocked"
    assert d.wb.published == []


# visibility: never a publish button for text the operator cannot see ------------------------
LONG_V31 = ("Людмила, спасибо за подробный отзыв! " + "Нам очень приятно, что крем стал частью ухода. " * 20)[:990].strip()


class _Plan:
    route = "STANDARD"


def _long_prepare(*a, **k):
    return SimpleNamespace(status="READY", text=LONG_V31, final_policy={"verdict": "PASS"}, plan=_Plan(),
                           quality=SimpleNamespace(verdict="PASS"))


def long_deps(monkeypatch, policies):
    import app.response_quality.core as core
    monkeypatch.setattr(core, "prepare", _long_prepare)
    policies.v31.verdicts = ["PASS", "PASS"]                  # isolate visibility from policy content
    review = fb(text="Пользуюсь кремом уже месяц, впечатления подробные. " * 60)
    review.update(pros="Мягкость, запах, тюбик. " * 25, cons="Хотелось бы объём побольше. " * 20)
    return deps(feedback=review, openai=FixedV2(("Людмила, спасибо! " + "Рады, что крем подошёл. " * 60)[:990]))


def test_long_card_has_no_publish_button_for_invisible_text(monkeypatch, policies):
    d = long_deps(monkeypatch, policies)
    run_poll(d)
    doc_id, doc = doc_of(d)
    card, keyboard = d.telegram.sent[-1][1], d.telegram.sent[-1][2]
    assert len(card) <= 3800 and LONG_V31 not in card       # 3.1E cut by the Telegram limit
    gen = doc["generation_number"]
    assert f"s31:{doc_id}:{gen}" in buttons(d, keyboard)
    assert not any(c.startswith("p31:") for c in buttons(d, keyboard))
    # «Показать полностью» (full review) still cannot fit the 3.1E text: still no publish button
    tap(d, f"show:{doc_id}")
    shown, markup = d.telegram.edits[-1][1], d.telegram.edit_markups[-1][1]
    assert LONG_V31 not in shown and not any(c.startswith("p31:") for c in buttons(d, markup))


def test_full_view_shows_exact_text_then_publishes(monkeypatch, policies):
    d = long_deps(monkeypatch, policies)
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert tap(d, f"s31:{doc_id}:{gen}")["status"] == "v31_shown"
    full = d.telegram.sent[-1][1]
    assert "Вариант 3.1E полностью" in full and LONG_V31 in full
    assert buttons(d) == [f"p31:{doc_id}:{gen}"]               # publish bound to the same generation
    assert d.repo.get(doc_id)["final_answer"] != LONG_V31     # viewing changes nothing
    full_view = {"id": "cq7", "from": {"id": 302044578}, "data": f"p31:{doc_id}:{gen}",
                 "message": {"message_id": d.telegram._id, "chat": {"id": 302044578}}}
    assert handle_update(d, {"update_id": 7, "callback_query": full_view})["status"] == "published"
    assert [t for _, t in d.wb.published] == [LONG_V31]


def test_full_view_rejects_stale_generation(monkeypatch, policies):
    d = long_deps(monkeypatch, policies)
    run_poll(d)
    doc_id, doc = doc_of(d)
    gen = doc["generation_number"]
    assert tap(d, f"s31:{doc_id}:{gen - 1}")["status"] == "stale"
    assert tap(d, f"s31:{doc_id}:{gen}", n=2)["status"] == "v31_shown"
    tap(d, f"regen:{doc_id}", n=3)                            # the card moves on
    assert d.repo.get(doc_id)["generation_number"] > gen
    assert tap(d, f"p31:{doc_id}:{gen}", n=4)["status"] == "stale"   # the full view is now stale
    assert tap(d, f"s31:{doc_id}:{gen}", n=5)["status"] == "stale"
    assert d.wb.published == []


def test_short_card_publish_button_only_when_text_visible():
    from app.services.pipeline import _v31_visible
    text = "Спасибо & до встречи!"
    assert _v31_visible("✨ <b>Вариант 3.1E:</b>\nСпасибо &amp; до встречи!\n\n<i>id</i>", text)
    assert not _v31_visible("✨ <b>Вариант 3.1E:</b>\nСпасибо &amp; до вс…", text)
    assert not _v31_visible("Комментарий: Спасибо &amp; до встречи!", text)   # not under its own label
    assert not _v31_visible(None, text)
