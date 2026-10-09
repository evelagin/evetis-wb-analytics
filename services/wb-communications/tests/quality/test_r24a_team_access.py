"""R2.4A Telegram team access + operator identity. Bootstrap env users stay OWNER; ACTIVE team
members of the moderation chat get every moderation button (incl. R2.3 owner decisions); only
OWNER manages the team; Firestore failure fails closed for non-bootstrap users."""
import json

import pytest

from app.services.pipeline import handle_update, run_poll
from tests.conftest import make_deps
from tests.quality.test_r2_operator_draft import FixedV2, fb
from tests.quality.test_r21_primary_operator import anna

OWNER, OWNER2, MARIA, ALEX, STRANGER = "302044578", "868383129", "555000111", "555000222", "999"
GROUP = "-1009999"
CLAIM = "Людмила, спасибо! Масла ши и миндаля питают кожу рук."          # hand cream: RED (V-CONFLICT)
ORDINARY = "Анна, спасибо! Масла ши и миндаля питают кожу рук."          # set: ordinary finding


class Exploding:
    """A team store whose every call fails (Firestore outage)."""
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("firestore unavailable")
        return boom


def team_deps(enabled=True, feedback=None, **extra):
    d = make_deps([feedback or fb()], openai=FixedV2(), v31_only_operator_enabled=True,
                  v31_owner_override_enabled=True, telegram_chat_id=GROUP,
                  telegram_allowed_user_ids={OWNER, OWNER2}, telegram_dynamic_access_enabled=enabled, **extra)
    d.settings.v31_owner_override_user_ids = {OWNER, OWNER2}
    d.publication_validator = None
    return d


_n = iter(range(1, 10**6))


def say(d, text, user, chat=GROUP, **who):
    frm = {"id": int(user), "first_name": who.get("first_name", "Мария"), "is_bot": who.get("is_bot", False)}
    if who.get("username"):
        frm["username"] = who["username"]
    return handle_update(d, {"update_id": next(_n), "message": {"chat": {"id": int(chat)}, "from": frm, "text": text}})


def press(d, data, user, chat=GROUP, message_id=1):
    q = {"id": f"cq{next(_n)}", "from": {"id": int(user)}, "data": data,
         "message": {"message_id": message_id, "chat": {"id": int(chat)}}}
    return handle_update(d, {"update_id": next(_n), "callback_query": q})


def doc(d):
    doc_id = next(iter(d.repo.docs))
    return doc_id, d.repo.get(doc_id)


def buttons(d):
    markup = d.telegram.sent[-1][2] or {"inline_keyboard": []}
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def approve(d, user=MARIA, by=OWNER):
    say(d, "/apply", user, first_name="Мария", username="maria")
    return press(d, f"ta:{user}", by)


def edit_as(d, user, text):
    doc_id, _ = doc(d)
    assert press(d, f"edit:{doc_id}", user)["status"] == "editing_started"
    reply = {"update_id": next(_n), "message": {"chat": {"id": int(GROUP)}, "from": {"id": int(user)}, "text": text,
                                                "reply_to_message": {"message_id": d.telegram._id}}}
    assert handle_update(d, reply)["status"] == "edited"
    return d.repo.get(doc_id)


def events(d):
    return [r["payload"] for r in d.repo.outbox.values()]


# A. flag off ---------------------------------------------------------------------------------
def test_flag_off_keeps_current_auth_and_never_touches_the_team_store():
    d = team_deps(enabled=False)
    d.repo.team = Exploding()                                             # any access would raise
    assert say(d, "/apply", MARIA)["status"] == "unauthorized"
    run_poll(d)
    doc_id, current = doc(d)
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", MARIA)["status"] == "unauthorized"
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", OWNER)["status"] == "published"
    assert press(d, f"ta:{MARIA}", OWNER)["status"] == "ignored"


# B–F. /apply ---------------------------------------------------------------------------------
def test_unknown_user_cannot_moderate():
    d = team_deps()
    run_poll(d)
    doc_id, current = doc(d)
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", STRANGER)["status"] == "unauthorized"
    assert d.wb.published == []


def test_apply_creates_one_pending_request_and_owner_card():
    d = team_deps()
    assert say(d, "/apply@EvetisBot", MARIA, first_name="Мария", username="maria")["status"] == "requested"
    req = d.repo.team.get_request(GROUP, MARIA)
    assert req["status"] == "PENDING" and req["attempt_count"] == 1 and req["username"] == "maria"
    assert "Запрос доступа отправлен" in d.telegram.sent[-2][1]
    assert "Запрос доступа EVETIS" in d.telegram.sent[-1][1] and "Telegram ID: 555000111" in d.telegram.sent[-1][1]
    assert buttons(d) == [f"ta:{MARIA}", f"tr:{MARIA}"]
    assert say(d, "/apply", MARIA)["status"] == "already_pending"                       # E
    assert len(d.repo.team.requests) == 1 and d.repo.team.get_request(GROUP, MARIA)["attempt_count"] == 1
    assert [a["action"] for a in d.repo.team.audit] == ["ACCESS_REQUESTED"]


def test_apply_in_wrong_chat_or_by_bot_is_refused():
    d = team_deps()
    assert say(d, "/apply", MARIA, chat="-100777")["status"] == "unauthorized"
    assert say(d, "/apply", MARIA, is_bot=True)["status"] == "unauthorized"
    assert d.repo.team.requests == {}


def test_active_user_applying_is_told_so():
    d = team_deps()
    assert say(d, "/apply", OWNER)["status"] == "already_active"                        # bootstrap
    approve(d)
    assert say(d, "/apply", MARIA)["status"] == "already_active"                        # F


# G–I. approval -------------------------------------------------------------------------------
def test_bootstrap_owner_approves_operator():
    d = team_deps()
    assert approve(d)["status"] == "approved"
    member = d.repo.team.get_member(GROUP, MARIA)
    assert member["status"] == "ACTIVE" and member["role"] == "OPERATOR" and member["approved_by"] == OWNER
    assert d.repo.team.get_request(GROUP, MARIA)["status"] == "APPROVED"
    assert any("доступ активирован" in m[1] for m in d.telegram.sent)
    assert any("Доступ выдан" in e[1] for e in d.telegram.edits)


@pytest.mark.parametrize("presser", [ALEX, STRANGER])
def test_operator_or_stranger_cannot_approve(presser):
    d = team_deps()
    if presser == ALEX:
        approve(d, ALEX)
    say(d, "/apply", MARIA)
    assert press(d, f"ta:{MARIA}", presser)["status"] == "unauthorized"
    assert press(d, f"tr:{MARIA}", presser)["status"] == "unauthorized"
    assert d.repo.team.get_request(GROUP, MARIA)["status"] == "PENDING" and d.repo.team.get_member(GROUP, MARIA) is None


# J. a dynamic operator has every moderation button -------------------------------------------
def test_operator_publishes_edits_regenerates_and_skips():
    d = team_deps()
    approve(d)
    run_poll(d)
    doc_id, current = doc(d)
    assert press(d, f"regen:{doc_id}", MARIA)["status"] == "regenerated"
    current = edit_as(d, MARIA, "Людмила, спасибо за отзыв! Рады, что крем понравился.")
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", MARIA)["status"] == "published"
    d2 = team_deps()
    approve(d2)
    run_poll(d2)
    assert press(d2, f"skip:{doc(d2)[0]}", MARIA)["status"] == "skipped"


def test_operator_publish_as_is_on_ordinary_finding():                                # W
    d = team_deps(feedback=anna())
    approve(d)
    run_poll(d)
    doc_id, _ = doc(d)
    current = edit_as(d, MARIA, ORDINARY)
    assert f"pa:{doc_id}:{current['generation_number']}" in buttons(d)
    assert press(d, f"pa:{doc_id}:{current['generation_number']}", MARIA)["status"] == "published"
    after = d.repo.get(doc_id)
    assert after["owner_override_audit"][-1]["override_by"] == MARIA
    assert after["publish_trace"][-1]["policy"]["verdict"] == "BLOCK"
    assert press(d, f"pa:{doc_id}:{current['generation_number']}", MARIA)["status"] == "stale"   # Y: once
    assert len(d.wb.published) == 1


def test_operator_red_two_step_confirmation():                                         # X
    d = team_deps()
    approve(d)
    run_poll(d)
    doc_id, _ = doc(d)
    edit_as(d, MARIA, CLAIM)
    ov = [c for c in buttons(d) if c.startswith("ov:")]
    assert ov and press(d, ov[0], MARIA)["status"] == "override_confirmation_required"
    nonce = d.repo.get(doc_id)["owner_override_pending"]["nonce"]
    assert press(d, f"oc:{doc_id}:{nonce}", ALEX)["status"] == "unauthorized"             # stranger to the team
    assert press(d, f"oc:{doc_id}:{nonce}", MARIA)["status"] == "published"
    assert len(d.wb.published) == 1


# K–P. team administration ---------------------------------------------------------------------
def test_operator_cannot_manage_team():
    d = team_deps()
    approve(d)
    approve(d, ALEX)
    assert say(d, "/team", MARIA)["status"] == "unauthorized"
    assert say(d, "/requests", MARIA)["status"] == "unauthorized"
    assert press(d, f"tx:{ALEX}", MARIA)["status"] == "unauthorized"
    assert d.repo.team.get_member(GROUP, ALEX)["status"] == "ACTIVE"


def test_owner_team_list_and_member_card():
    d = team_deps()
    say(d, "/me", OWNER, first_name="Евгений")
    approve(d)
    assert say(d, "/team", OWNER, first_name="Евгений")["status"] == "team"
    text = d.telegram.sent[-1][1]
    assert "Евгений — OWNER · bootstrap" in text and text.count("OWNER · bootstrap") == 2
    assert "Мария — OPERATOR" in text and buttons(d) == [f"tm:{MARIA}"]
    assert press(d, f"tm:{MARIA}", OWNER)["status"] == "member"


def test_revoke_with_confirmation_blocks_the_next_callback():
    d = team_deps()
    approve(d)
    run_poll(d)
    doc_id, current = doc(d)
    assert press(d, f"tv:{MARIA}", OWNER)["status"] == "revoke_confirmation_required"
    assert d.repo.team.get_member(GROUP, MARIA)["status"] == "ACTIVE"                  # nothing until confirmed
    assert press(d, f"tx:{MARIA}", OWNER)["status"] == "revoked"
    member = d.repo.team.get_member(GROUP, MARIA)
    assert member["status"] == "REVOKED" and member["revoked_by"] == OWNER
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", MARIA)["status"] == "unauthorized"   # N
    assert d.wb.published == []


def test_duplicate_admin_actions_are_idempotent():
    d = team_deps()
    approve(d)
    assert press(d, f"ta:{MARIA}", OWNER)["status"] == "already"
    say(d, "/apply", ALEX)
    assert press(d, f"tr:{ALEX}", OWNER)["status"] == "rejected"
    assert press(d, f"tr:{ALEX}", OWNER)["status"] == "already"
    assert press(d, f"tx:{MARIA}", OWNER)["status"] == "revoked"
    assert press(d, f"tx:{MARIA}", OWNER2)["status"] == "already"
    assert [a["action"] for a in d.repo.team.audit] == [
        "ACCESS_REQUESTED", "ACCESS_APPROVED", "ACCESS_REQUESTED", "ACCESS_REJECTED", "ACCESS_REVOKED"]
    assert say(d, "/apply", ALEX)["status"] == "requested"                               # rejected may re-apply
    assert d.repo.team.get_request(GROUP, ALEX)["attempt_count"] == 2


def test_bootstrap_owner_cannot_be_revoked():
    d = team_deps()
    assert press(d, f"tv:{OWNER2}", OWNER)["status"] == "bootstrap_protected"
    assert press(d, f"tx:{OWNER2}", OWNER)["status"] == "bootstrap_protected"
    assert d.repo.team.audit == []


# Q–S. failure modes ----------------------------------------------------------------------------
def test_firestore_outage_bootstrap_works_team_fails_closed():
    d = team_deps()
    approve(d)
    run_poll(d)
    doc_id, current = doc(d)
    d.repo.team = Exploding()
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", MARIA)["status"] == "unauthorized"
    assert say(d, "/apply", ALEX)["status"] == "unavailable"
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", OWNER)["status"] == "published"


def test_forged_callbacks_change_nothing():
    d = team_deps()
    say(d, "/apply", MARIA)
    assert press(d, f"ta:{MARIA}", STRANGER)["status"] == "unauthorized"
    assert press(d, f"ta:{MARIA}", OWNER, chat="-100777")["status"] == "unauthorized"     # other chat
    assert press(d, "ta:@maria", OWNER)["status"] == "bad_request"
    assert press(d, f"tx:{MARIA}", OWNER)["status"] == "not_found"
    assert d.repo.team.get_request(GROUP, MARIA)["status"] == "PENDING"


def test_failed_approval_transaction_leaves_no_half_state():
    d = team_deps()
    say(d, "/apply", MARIA)
    d.repo.team.fail_on_commit = True
    assert press(d, f"ta:{MARIA}", OWNER)["status"] == "failed"
    assert d.repo.team.get_request(GROUP, MARIA)["status"] == "PENDING"
    assert d.repo.team.get_member(GROUP, MARIA) is None
    assert [a["action"] for a in d.repo.team.audit] == ["ACCESS_REQUESTED"]


# T–U. audit and operator identity --------------------------------------------------------------
def test_team_audit_records_actor_target_action():
    d = team_deps()
    approve(d)
    approved = d.repo.team.audit[-1]
    assert approved["action"] == "ACCESS_APPROVED" and approved["actor_user_id"] == OWNER
    assert approved["target_user_id"] == MARIA and approved["after_status"] == "ACTIVE"
    assert approved["role"] == "OPERATOR" and approved["chat_id"] == GROUP and approved["event_id"]


def test_moderation_events_carry_operator_identity():
    d = team_deps()
    approve(d)
    run_poll(d)
    doc_id, _ = doc(d)
    press(d, f"regen:{doc_id}", MARIA)
    current = edit_as(d, MARIA, "Людмила, спасибо за отзыв! Рады, что крем понравился.")
    press(d, f"pub:{doc_id}:{current['generation_number']}", MARIA)
    rows = events(d)
    by_type = {}
    for row in rows:
        by_type.setdefault(row["event_type"], []).append(row)
    for event_type, action in [("regenerated", "regenerate"), ("edit_started", "edit_start"),
                               ("manually_edited", "edit_commit"), ("published", "publish")]:
        row = by_type[event_type][-1]
        actor = json.loads(row["payload_json"])["actor"]
        assert row["telegram_user_id"] == MARIA and actor["user_id"] == MARIA, event_type
        assert actor["role"] == "OPERATOR" and actor["source"] == "dynamic" and actor["action"] == action
    assert d.repo.get(doc_id)["publish_trace"][-1]["actor"]["user_id"] == MARIA


def test_actor_metadata_with_flag_off_uses_env_only():
    d = team_deps(enabled=False)
    d.repo.team = Exploding()
    run_poll(d)
    doc_id, current = doc(d)
    press(d, f"pub:{doc_id}:{current['generation_number']}", OWNER)
    row = [r for r in events(d) if r["event_type"] == "published"][-1]
    actor = json.loads(row["payload_json"])["actor"]
    assert actor == {"user_id": OWNER, "role": "OWNER", "source": "bootstrap", "action": "publish"}


# /me, /help, /requests --------------------------------------------------------------------------
def test_me_help_and_requests():
    d = team_deps()
    say(d, "/me", STRANGER)
    assert "доступа нет" in d.telegram.sent[-1][1]
    say(d, "/apply", MARIA)
    say(d, "/me", MARIA)
    assert "PENDING" in d.telegram.sent[-1][1]
    say(d, "/me", OWNER)
    assert "Роль: OWNER" in d.telegram.sent[-1][1] and "bootstrap" in d.telegram.sent[-1][1]
    say(d, "/help", STRANGER)
    assert "/apply" in d.telegram.sent[-1][1] and "/team" not in d.telegram.sent[-1][1]
    say(d, "/help", OWNER)
    assert "/team" in d.telegram.sent[-1][1]
    assert say(d, "/requests", OWNER)["count"] == 1 and buttons(d) == [f"ta:{MARIA}", f"tr:{MARIA}"]
    press(d, f"ta:{MARIA}", OWNER)
    say(d, "/help", MARIA)
    assert "/team" not in d.telegram.sent[-1][1] and "/me" in d.telegram.sent[-1][1]
    assert say(d, "/requests", OWNER)["count"] == 0


def test_display_names_never_authorize():
    d = team_deps()
    say(d, "/apply", STRANGER, first_name="Евгений", username="owner")
    run_poll(d)
    doc_id, current = doc(d)
    assert press(d, f"pub:{doc_id}:{current['generation_number']}", STRANGER)["status"] == "unauthorized"


def test_team_pagination_is_bounded():
    d = team_deps()
    for i in range(12):
        uid = str(700000 + i)
        say(d, "/apply", uid, first_name=f"Сотрудник{i}")
        press(d, f"ta:{uid}", OWNER)
    assert say(d, "/team", OWNER)["page"] == 0
    assert "tl:1" in buttons(d) and len([c for c in buttons(d) if c.startswith("tm:")]) <= 8
    assert press(d, "tl:1", OWNER)["page"] == 1
