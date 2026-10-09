"""R2.3: the authorised owner has the final content word on R2.2 cards. v3.1E proposes, checks
and warns; a content BLOCK (or a policy outage) can be published past only by a listed owner
through two explicit taps bound to the exact text. The real verdict stays recorded, serious
safety allows only operator text, and every technical publication guarantee is unchanged."""
import pytest

from app.domain.exceptions import WBPublishOutcomeUnknown
from app.services.pipeline import handle_update, run_poll
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import FixedV2, buttons, doc_of, fb, last_trace, policies, tap  # noqa: F401
from tests.quality.test_r21_primary_operator import anna, card, edit
from tests.quality.test_r22_v31_only import RESTOCK, question

OWNER, OPERATOR = 302044578, 868383129
CLAIM = "Людмила, спасибо! Масла ши и миндаля питают кожу рук."          # unsupported claim → V-CLAIM BLOCK
SAFE_SERIOUS = "Ольга, нам очень жаль. Пожалуйста, прекратите использование крема."
SERIOUS = "После крема опух язык и тяжело дышать"


class WBApi:
    """Read-before-write / one POST / read-back double (as in test_feedback_publication)."""
    def __init__(self):
        self.answer, self.visible, self.error, self.calls = None, True, None, []

    def iter_unanswered_feedbacks(self):
        return []

    def get_feedback(self, fid, **kwargs):
        self.calls.append("GET")
        return {"id": fid, "answer": self.answer}

    def publish_answer(self, fid, text):
        self.calls.append("POST")
        if self.visible:
            self.answer = {"text": text, "state": "wbRu"}
        if self.error:
            raise self.error
        return {"status_code": 204, "response": None}


def owner_deps(feedback=None, owners=(OWNER,), **extra):
    extra.setdefault("openai", FixedV2())
    d = make_deps([feedback or fb()], v31_only_operator_enabled=True, v31_owner_override_enabled=True,
                  telegram_allowed_user_ids={str(OWNER), str(OPERATOR)}, **extra)
    d.settings.v31_owner_override_user_ids = {str(u) for u in owners}
    d.publication_validator = None
    return d


def cb(d, data, n, user=OWNER):
    doc_id, doc = doc_of(d)
    q = {"id": f"cq{n}", "from": {"id": user}, "data": data,
         "message": {"message_id": int(doc.get("telegram_message_id") or 1001), "chat": {"id": user}}}
    return handle_update(d, {"update_id": n, "callback_query": q})


def blocked_operator_text(d, text=CLAIM):
    """Operator edits, taps ✅, the v3.1E policy BLOCKs. Returns the owner-decision callback."""
    run_poll(d)
    doc = edit(d, text, n=20)
    doc_id = doc_of(d)[0]
    assert cb(d, f"pub:{doc_id}:{doc['generation_number']}", 30)["status"] == "policy_blocked"
    keyboard = d.telegram.edit_markups[-1][1]
    ov = [c for c in buttons(d, keyboard) if c.startswith("ov:")]
    return doc_id, ov


def confirm(d, n):
    nonce = doc_of(d)[1]["owner_override_pending"]["nonce"]
    return cb(d, f"oc:{doc_of(d)[0]}:{nonce}", n)


# normal path ------------------------------------------------------------------------------
@pytest.mark.parametrize("text, rating", [("Очень хороший крем для рук. Руки мягкие.", 5),
                                          ("Крем не понравился, слишком жирный.", 2)])
def test_normal_reviews_publish_without_owner_step(text, rating):
    d = owner_deps(feedback=fb(text=text, rating=rating))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert not any(c.startswith("ov:") for c in buttons(d))
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"
    assert last_trace(d)["publication_mode"] == "v31" and "owner_override" not in last_trace(d)


def test_restock_question_publishes_normally(monkeypatch):
    d = question("Здравствуйте когда появится крем 438775437 ?", monkeypatch=monkeypatch)
    d.settings.v31_owner_override_enabled = True
    d.settings.v31_owner_override_user_ids = {str(OWNER)}
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["final_answer"] == RESTOCK and not any(c.startswith("ov:") for c in buttons(d))
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"


# owner final authority on a content BLOCK --------------------------------------------------
def test_owner_publishes_blocked_operator_text_with_two_taps(policies):
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    assert ov and "Ничего не опубликовано" in d.telegram.edits[-1][1] and d.wb.published == []
    assert cb(d, ov[0], 40)["status"] == "override_confirmation_required"
    message = d.telegram.sent[-1][1]
    assert "Решение владельца" in message and "V-CLAIM" in message and "Масла ши и миндаля" in message
    assert d.wb.published == []                                         # first tap writes nothing
    assert confirm(d, 41)["status"] == "published"
    assert [t for _, t in d.wb.published] == [CLAIM]
    trace, after = last_trace(d), d.repo.get(doc_id)
    assert trace["policy"]["verdict"] == "BLOCK"                       # never rewritten into PASS
    assert trace["owner_override"]["override"] is True and trace["owner_override"]["override_by"] == str(OWNER)
    assert trace["publication_mode"] == "v31" and policies.live.calls == []
    audit = after["owner_override_audit"][-1]
    assert audit["answer_hash"] and audit["communication_context_sha256"] and audit["policy_version"]
    assert after["status"] == "published" and after["verified_at"]
    assert confirm(d, 42)["status"] == "stale" and len(d.wb.published) == 1   # double confirmation


@pytest.mark.parametrize("user", [OPERATOR, 999])
def test_only_listed_owner_can_decide(user):
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    assert cb(d, ov[0], 40, user=user)["status"] == "unauthorized"
    assert cb(d, ov[0], 41)["status"] == "override_confirmation_required"   # the owner opens it
    nonce = doc_of(d)[1]["owner_override_pending"]["nonce"]
    assert cb(d, f"oc:{doc_id}:{nonce}", 43, user=user)["status"] == "unauthorized"
    assert d.wb.published == []


@pytest.mark.parametrize("enabled, owners", [(False, (OWNER,)), (True, ())])
def test_no_owner_step_without_flag_and_explicit_owner_list(enabled, owners):
    d = owner_deps(owners=owners)
    d.settings.v31_owner_override_enabled = enabled
    doc_id, ov = blocked_operator_text(d)
    assert ov == []
    gen = doc_of(d)[1]["generation_number"]
    assert cb(d, f"ov:{doc_id}:{gen}:{gen}", 40)["status"] == "unauthorized"
    assert d.wb.published == []


def test_independent_of_enforce_flag():
    d = owner_deps()
    assert d.settings.v31_enforce_live_publication_policy is False
    _, ov = blocked_operator_text(d)
    assert ov


@pytest.mark.parametrize("change", ["generation", "text", "expiry", "customer"])
def test_stale_or_changed_confirmation_writes_nothing(change):
    from datetime import datetime, timedelta, timezone
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    cb(d, ov[0], 40)
    doc = d.repo.docs[doc_id]
    if change == "generation":
        doc["generation_number"] += 1
    elif change == "text":
        doc["final_answer"] = "Другой текст."
    elif change == "expiry":
        doc["owner_override_pending"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    else:
        doc["text"] = "Совсем другой отзыв"
    assert confirm(d, 41)["status"] == "stale" and d.wb.published == []


def test_policy_change_between_taps_requires_reconfirmation(monkeypatch):
    import app.services.owner_override as oo
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    cb(d, ov[0], 40)
    monkeypatch.setattr(oo, "fingerprint", lambda policy: "changed")
    assert confirm(d, 41)["status"] == "override_reconfirmation_required" and d.wb.published == []


def test_repeated_callback_update_is_processed_once():
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    cb(d, ov[0], 40)
    nonce = doc_of(d)[1]["owner_override_pending"]["nonce"]
    first = cb(d, f"oc:{doc_id}:{nonce}", 41)
    again = cb(d, f"oc:{doc_id}:{nonce}", 41)                         # Telegram redelivers update 41
    assert first["status"] == "published" and again["status"] != "published"
    assert len(d.wb.published) == 1


# validator outage: manual work never locked forever ----------------------------------------
def test_policy_outage_owner_can_still_publish_operator_text(monkeypatch):
    import app.services.publication_policy as pp
    d = owner_deps()
    run_poll(d)
    text = "Людмила, спасибо за отзыв!"
    doc = edit(d, text, n=20)
    doc_id = doc_of(d)[0]
    monkeypatch.setattr(pp, "validate_for_publication",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot unavailable")))
    assert cb(d, f"pub:{doc_id}:{doc['generation_number']}", 30)["status"] == "policy_check_failed"
    ov = [c for c in buttons(d, d.telegram.edit_markups[-1][1]) if c.startswith("ov:")]
    assert ov and cb(d, ov[0], 40)["status"] == "override_confirmation_required"
    assert "Проверка правил 3.1E недоступна" in d.telegram.sent[-1][1]
    assert confirm(d, 41)["status"] == "published"
    assert last_trace(d)["policy"]["verdict"] == "ERROR" and last_trace(d)["owner_override"]["override"] is True
    assert [t for _, t in d.wb.published] == [text]


# safety -------------------------------------------------------------------------------------
def test_serious_machine_text_never_overridable():
    d = owner_deps(feedback=fb(text=SERIOUS, rating=1))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert not any(c.startswith("ov:") for c in buttons(d))
    assert cb(d, f"pub:{doc_id}:{doc['generation_number']}", 30)["status"] == "human_review_required"
    gen = doc["generation_number"]
    assert cb(d, f"ov:{doc_id}:{gen}:{gen}", 31)["status"] == "stale" and d.wb.published == []


def test_serious_operator_text_owner_decision_under_human_safety():
    d = owner_deps(feedback=fb(text=SERIOUS, rating=1))
    doc_id, ov = blocked_operator_text(d, text="Ольга, нам очень жаль. Масла ши и миндаля питают кожу рук.")
    assert last_trace(d)["publication_mode"] == "v31_human_safety" and ov
    cb(d, ov[0], 40)
    assert confirm(d, 41)["status"] == "published"
    trace = last_trace(d)
    assert trace["publication_mode"] == "v31_human_safety" and trace["policy"]["verdict"] == "BLOCK"
    assert trace["policy"]["gate"] == "V31_HUMAN_SAFETY" and trace["owner_override"]["override"] is True


def test_serious_safe_operator_text_needs_no_owner_step():
    d = owner_deps(feedback=fb(text=SERIOUS, rating=1))
    run_poll(d)
    doc = edit(d, SAFE_SERIOUS, n=20)
    assert cb(d, f"pub:{doc_of(d)[0]}:{doc['generation_number']}", 30)["status"] == "published"


def test_irritation_complaint_machine_draft_publishes_and_operator_text_can_be_decided():
    d = owner_deps(feedback=fb(text="После крема покраснение и зуд на руках", rating=2))
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["v31_draft"]["status"] == "READY" and doc["v31_draft"]["attention"] == "SAFETY_TEMPLATE"
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"


ORDINARY = "Анна, спасибо! Масла ши и миндаля питают кожу рук."   # set: V-CLAIM/V-FACT, not RED


def test_moderate_case_owner_decision_on_operator_text():
    d = owner_deps(feedback=anna())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id = doc_of(d)[0]
    assert cb(d, f"pa:{doc_id}:{doc['generation_number']}", 30)["status"] == "published"


# technical guarantees under an owner decision ------------------------------------------------
def test_existing_answer_on_wb_is_never_overwritten():
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    d.wb = WBApi()
    d.wb.answer = {"text": "Ответ, данный в кабинете WB", "state": "wbRu"}
    cb(d, ov[0], 40)
    assert confirm(d, 41)["status"] == "answered_externally" and d.wb.calls == ["GET"]


@pytest.mark.parametrize("lands", [True, False])
def test_uncertain_api_result_never_resends(lands):
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    d.wb = WBApi()
    d.wb.visible, d.wb.error = lands, WBPublishOutcomeUnknown("unknown", status_code=None)
    cb(d, ov[0], 40)
    assert confirm(d, 41)["status"] == ("published" if lands else "publish_unknown")
    confirm(d, 42)
    run_poll(d)
    assert d.wb.calls.count("POST") == 1


def test_read_back_mismatch_is_not_published():
    d = owner_deps()
    doc_id, ov = blocked_operator_text(d)
    d.wb = WBApi()
    d.wb.visible = False                                                  # WB accepted, not visible yet
    cb(d, ov[0], 40)
    assert confirm(d, 41)["status"] == "publish_accepted" and d.repo.get(doc_id)["status"] != "published"


def test_technical_limit_is_never_overridden():
    d = owner_deps()
    run_poll(d)
    doc_id = doc_of(d)[0]
    long_text = "Спасибо! " * 120
    d.repo.docs[doc_id].update(final_answer=long_text, status="policy_blocked")
    d.repo.docs[doc_id]["answer_versions"].append(
        {"text": long_text, "source": "manual", "generation_number": d.repo.docs[doc_id]["generation_number"] + 1})
    d.repo.docs[doc_id]["generation_number"] += 1
    from app.services.owner_override import offer
    assert offer(d.repo.get(doc_id), d.settings) is None


def test_v2_never_returns_through_the_owner_path():
    d = owner_deps()
    run_poll(d)
    doc_id = doc_of(d)[0]
    doc = d.repo.docs[doc_id]
    doc.update(status="policy_blocked", final_answer=CLAIM, ai_answer=CLAIM)
    doc["answer_versions"].append({"text": CLAIM, "source": "ai", "generation_number": doc["generation_number"] + 1})
    doc["generation_number"] += 1
    from app.services.owner_override import offer
    assert offer(d.repo.get(doc_id), d.settings) is None
    gen = doc["generation_number"]
    assert cb(d, f"ov:{doc_id}:{gen}:{gen}", 40)["status"] == "stale" and d.wb.published == []


def test_legacy_override_contract_untouched():
    from types import SimpleNamespace
    from app.services.owner_override import enabled, owner_authority
    assert enabled(SimpleNamespace(v31_owner_override_enabled=True)) is False
    assert owner_authority(SimpleNamespace(v31_owner_override_enabled=True, v31_owner_override_user_ids=set())) is False
    assert owner_authority(SimpleNamespace(v31_owner_override_enabled=True, v31_owner_override_user_ids={"1"})) is True


# old pending cards → R2.2 flow -------------------------------------------------------------------
def test_old_r21_card_migrates_without_any_publication():
    from app.services.pipeline import migrate_to_v31_only, migration_plan
    d = make_deps([fb()], openai=FixedV2(), v31_primary_operator_enabled=True)     # an old R2.1 card
    d.publication_validator = None
    run_poll(d)
    doc_id, doc = doc_of(d)
    assert doc["operator_mode"] == "v31_primary" and migration_plan(doc) == "ELIGIBLE"
    d.settings.v31_only_operator_enabled = True                                     # R2.2 now active
    old_message = doc["telegram_message_id"]
    result = migrate_to_v31_only(d, doc_id)
    after = d.repo.get(doc_id)
    assert result["status"] == "migrated" and after["operator_mode"] == "v31_only"
    assert after["answer_versions"][-1]["source"] == "v31e" and after["status"] == "pending_approval"
    assert after["telegram_message_id"] != old_message and "Рекомендуемый ответ 3.1E" in card(d)
    assert any(str(mid) == str(old_message) for mid, _ in d.telegram.edits)       # old card retired
    assert d.wb.published == []
    assert migrate_to_v31_only(d, doc_id) == {"status": "not_eligible", "reason": "ALREADY_V31_ONLY"}


@pytest.mark.parametrize("status", ["published", "publishing", "publish_unknown", "publish_accepted",
                                    "answered_externally", "skipped"])
def test_processed_records_are_never_migrated(status):
    from app.services.pipeline import migrate_to_v31_only
    d = make_deps([fb()], openai=FixedV2(), v31_primary_operator_enabled=True)
    run_poll(d)
    doc_id, doc = doc_of(d)
    d.repo.docs[doc_id]["status"] = status
    d.settings.v31_only_operator_enabled = True
    before = dict(d.repo.get(doc_id))
    assert migrate_to_v31_only(d, doc_id)["status"] == "not_eligible"
    assert d.repo.get(doc_id) == before and d.wb.published == []


def test_migration_skips_records_with_a_known_wb_answer():
    from app.services.pipeline import migration_plan
    assert migration_plan({"status": "publish_failed", "verified_at": "2026-10-08T10:00:00+00:00"}) == \
        "EXCLUDED_ALREADY_ANSWERED"


# R2.3 proactive preflight: the finding is on the card BEFORE the owner acts --------------------
def pf_of(d):
    return doc_of(d)[1]["publication_preflight"]


def test_pass_card_has_persisted_preflight_and_one_click_publish(policies):
    d = owner_deps()
    run_poll(d)
    doc_id, doc = doc_of(d)
    pf = doc["publication_preflight"]
    import hashlib
    assert pf["verdict"] == "PASS" and pf["generation"] == doc["generation_number"] and pf["mode"] == "v31"
    assert pf["text_sha256"] == hashlib.sha256(doc["final_answer"].encode()).hexdigest()
    assert pf["policy_version"] and pf["snapshot"] and pf["checked_at"] and pf["rules"] == [] and not pf["red"]
    keyboard = d.telegram.sent[-1][2]["inline_keyboard"]
    assert [b["text"] for row in keyboard for b in row] == ["✅ Опубликовать", "✏️ Изменить", "🔄 Другой вариант",
                                                            "⏭ Пропустить"]
    assert "не рекомендует" not in card(d)
    assert tap(d, f"pub:{doc_id}:{doc['generation_number']}")["status"] == "published"   # one click
    assert d.wb.published and "owner_override" not in last_trace(d)


def test_ordinary_finding_is_shown_before_action_and_owner_publishes_in_one_tap(policies):
    d = owner_deps(feedback=anna())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id, gen = doc_of(d)[0], doc["generation_number"]
    pf = pf_of(d)
    assert pf["verdict"] == "BLOCK" and set(pf["rules"]) >= {"V-CLAIM"} and pf["spans"] and not pf["red"]
    assert "Система не рекомендует публикацию без проверки" in card(d) and "«питают»" in card(d)
    texts = [b["text"] for row in d.telegram.sent[-1][2]["inline_keyboard"] for b in row]
    assert texts == ["✅ Опубликовать как есть", "✨ Безопасная альтернатива", "✏️ Изменить",
                     "🔄 Другой вариант", "⏭ Пропустить"]
    assert cb(d, f"pa:{doc_id}:{gen}", 30, user=OPERATOR)["status"] == "unauthorized" and d.wb.published == []
    assert cb(d, f"pa:{doc_id}:{gen}", 31)["status"] == "published"
    trace = last_trace(d)
    assert trace["policy"]["verdict"] == "BLOCK" and trace["owner_override"]["override"] is True
    assert d.repo.get(doc_id)["owner_override_audit"][-1]["confirmed_by_card"] is True
    assert [t for _, t in d.wb.published] == [ORDINARY] and policies.live.calls == []
    assert cb(d, f"pa:{doc_id}:{gen}", 32)["status"] == "stale" and len(d.wb.published) == 1


def test_finding_card_without_owner_authority_offers_no_publish():
    d = owner_deps(feedback=anna(), owners=())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id = doc_of(d)[0]
    assert not any(c.startswith(("pa:", "ov:", "pub:")) for c in buttons(d))
    assert f"sa:{doc_id}:{doc['generation_number']}" in buttons(d)
    assert cb(d, f"pa:{doc_id}:{doc['generation_number']}", 30)["status"] == "unauthorized"


def test_red_finding_requires_two_steps():
    d = owner_deps()
    run_poll(d)
    doc = edit(d, CLAIM, n=20)                                     # V-CONFLICT → RED
    doc_id, gen = doc_of(d)[0], doc["generation_number"]
    assert pf_of(d)["red"] and "HIGH_RISK_RULE" in pf_of(d)["red_reasons"]
    assert "Высокий риск" in card(d)
    assert not any(c.startswith("pa:") for c in buttons(d))
    ov = [c for c in buttons(d) if c.startswith("ov:")]
    assert cb(d, f"pa:{doc_id}:{gen}", 30)["status"] == "stale" and d.wb.published == []   # no one-tap
    assert cb(d, ov[0], 31)["status"] == "override_confirmation_required" and d.wb.published == []
    assert confirm(d, 32)["status"] == "published"


def test_policy_outage_at_card_time_is_red(monkeypatch):
    import app.services.publication_policy as pp
    d = owner_deps()
    run_poll(d)
    monkeypatch.setattr(pp, "validate_for_publication",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("snapshot unavailable")))
    doc = edit(d, "Людмила, спасибо за отзыв!", n=20)
    pf = pf_of(d)
    assert pf["verdict"] == "ERROR" and pf["red"] and pf["red_reasons"] == ["POLICY_UNAVAILABLE"]
    assert "Проверка правил 3.1E сейчас недоступна" in card(d)
    ov = [c for c in buttons(d) if c.startswith("ov:")]
    assert ov and not any(c.startswith(("pa:", "pub:")) for c in buttons(d))
    cb(d, ov[0], 30)
    assert confirm(d, 31)["status"] == "published" and last_trace(d)["policy"]["verdict"] == "ERROR"


def test_policy_change_between_card_and_tap_refreshes_card(monkeypatch):
    import app.services.owner_override as oo
    d = owner_deps(feedback=anna())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id = doc_of(d)[0]
    real = oo.fingerprint
    monkeypatch.setattr(oo, "fingerprint", lambda policy: real(policy) + "-new")
    assert cb(d, f"pa:{doc_id}:{doc['generation_number']}", 30)["status"] == "override_reconfirmation_required"
    assert d.wb.published == [] and "не рекомендует" in d.telegram.edits[-1][1]


def test_safe_alternative_restores_a_passing_3_1e_answer():
    d = owner_deps(feedback=anna())
    run_poll(d)
    v31_text = doc_of(d)[1]["final_answer"]
    doc = edit(d, ORDINARY, n=20)
    doc_id = doc_of(d)[0]
    result = cb(d, f"sa:{doc_id}:{doc['generation_number']}", 30)
    after = d.repo.get(doc_id)
    assert result["status"] == "alternative" and after["final_answer"] == v31_text
    assert after["answer_versions"][-1]["source"] == "v31e" and after["publication_preflight"]["verdict"] == "PASS"
    assert "✅ Опубликовать" in [b["text"] for row in d.telegram.edit_markups[-1][1]["inline_keyboard"] for b in row]
    assert d.wb.published == []
    assert cb(d, f"sa:{doc_id}:{doc['generation_number']}", 31)["status"] == "stale"


def test_safe_alternative_unavailable_changes_nothing(monkeypatch):
    import app.services.pipeline as pl
    d = owner_deps(feedback=anna())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id = doc_of(d)[0]
    monkeypatch.setattr(pl, "_passes_policy", lambda *a: False)
    monkeypatch.setattr(pl, "_v31_draft", lambda *a, **k: {"status": "HUMAN_REVIEW"})
    before = d.repo.get(doc_id)
    assert cb(d, f"sa:{doc_id}:{doc['generation_number']}", 30)["status"] == "no_alternative"
    after = d.repo.get(doc_id)
    assert after["final_answer"] == before["final_answer"] and after["generation_number"] == before["generation_number"]
    assert after["status"] == "pending_approval"


@pytest.mark.parametrize("case", ["external", "uncertain", "readback", "stale", "length"])
def test_publish_as_is_never_bypasses_technical_stops(case):
    d = owner_deps(feedback=anna())
    run_poll(d)
    doc = edit(d, ORDINARY, n=20)
    doc_id, gen = doc_of(d)[0], doc["generation_number"]
    d.wb = WBApi()
    if case == "external":
        d.wb.answer = {"text": "Ответ из кабинета WB", "state": "wbRu"}
        assert cb(d, f"pa:{doc_id}:{gen}", 30)["status"] == "answered_externally" and d.wb.calls == ["GET"]
    elif case == "uncertain":
        d.wb.visible, d.wb.error = False, WBPublishOutcomeUnknown("unknown", status_code=None)
        assert cb(d, f"pa:{doc_id}:{gen}", 30)["status"] == "publish_unknown"
        cb(d, f"pa:{doc_id}:{gen}", 31)
        assert d.wb.calls.count("POST") == 1
    elif case == "readback":
        d.wb.visible = False
        assert cb(d, f"pa:{doc_id}:{gen}", 30)["status"] == "publish_accepted"
        assert d.repo.get(doc_id)["status"] != "published"
    elif case == "stale":
        assert cb(d, f"pa:{doc_id}:{gen - 1}", 30)["status"] == "stale" and d.wb.calls == []
    else:
        long_text = "Спасибо! " * 120
        d.repo.docs[doc_id]["final_answer"] = long_text
        d.repo.docs[doc_id]["answer_versions"][-1]["text"] = long_text
        assert cb(d, f"pa:{doc_id}:{gen}", 30)["status"] == "stale" and d.wb.calls == []
