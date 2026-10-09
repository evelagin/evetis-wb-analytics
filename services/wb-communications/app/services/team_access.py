"""R2.4A Telegram team access: the ONE authorization source for the moderation chat.

authorization = bootstrap env users (TELEGRAM_ALLOWED_USER_IDS ∪ V31_OWNER_OVERRIDE_USER_IDS,
always OWNER, never revocable, never needs Firestore) ∪ ACTIVE Firestore members of the
configured moderation chat (only with TELEGRAM_DYNAMIC_ACCESS_ENABLED=true). Identity is
Telegram from.id; names are display metadata only. A lookup failure fails closed for every
non-bootstrap user. Works with bot privacy mode on: only /commands and callbacks are used.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services import team_store as ts
from app.utils.text import escape_html

PAGE = 8
NONE, UNAVAILABLE = "NONE", "UNAVAILABLE"
TEAM_ACTIONS = {"ta", "tr", "tm", "tv", "tx", "tl", "tq"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    chat_id: str
    role: str | None
    status: str          # ACTIVE | REVOKED | NONE | UNAVAILABLE
    source: str          # bootstrap | dynamic | none

    @property
    def can_moderate(self) -> bool:
        return self.status == ts.ACTIVE

    @property
    def can_policy_override(self) -> bool:      # every authorised operator has every moderation button
        return self.can_moderate

    @property
    def can_manage_team(self) -> bool:
        return self.can_moderate and self.role == ts.OWNER

    def as_actor(self) -> dict:
        return {"user_id": self.user_id, "role": self.role, "source": self.source}


def enabled(settings) -> bool:
    return bool(getattr(settings, "telegram_dynamic_access_enabled", False))


def bootstrap_ids(settings) -> set[str]:
    return ({str(u) for u in getattr(settings, "telegram_allowed_user_ids", set()) or set()}
            | {str(u) for u in getattr(settings, "v31_owner_override_user_ids", set()) or set()})


def moderation_chat(settings) -> str:
    return str(getattr(settings, "telegram_chat_id", "") or "")


def _chat_ok(settings, chat_id) -> bool:
    allowed = getattr(settings, "allowed_chat_ids", set()) or set()
    return not allowed or str(chat_id) in allowed


def resolve(deps, chat_id, user_id) -> Principal:
    """Re-read the authoritative access state for this chat and Telegram user id."""
    uid, cid = str(user_id or ""), str(chat_id or "")
    if not uid or not _chat_ok(deps.settings, cid):
        return Principal(uid, cid, None, NONE, "none")
    if uid in bootstrap_ids(deps.settings):
        return Principal(uid, cid, ts.OWNER, ts.ACTIVE, "bootstrap")
    if not enabled(deps.settings) or not moderation_chat(deps.settings) or cid != moderation_chat(deps.settings):
        return Principal(uid, cid, None, NONE, "none")
    try:
        member = deps.repo.team.get_member(cid, uid)
    except Exception:  # noqa: BLE001 — an outage never grants access
        return Principal(uid, cid, None, UNAVAILABLE, "none")
    if member and member.get("status") == ts.ACTIVE and member.get("role") in (ts.OWNER, ts.OPERATOR):
        return Principal(uid, cid, member["role"], ts.ACTIVE, "dynamic")
    return Principal(uid, cid, member.get("role") if member else None,
                     ts.REVOKED if member and member.get("status") == ts.REVOKED else NONE, "none")


def actor_for(deps, chat_id, user_id) -> dict:
    """Moderation actor metadata. With the flag off: env classification only, no Firestore."""
    uid = str(user_id or "")
    if not enabled(deps.settings):
        return {"user_id": uid, "role": ts.OWNER if uid in bootstrap_ids(deps.settings) else None,
                "source": "bootstrap" if uid in bootstrap_ids(deps.settings) else "allowlist"}
    return resolve(deps, chat_id, user_id).as_actor()


# --- display -------------------------------------------------------------------------------
def _name(row: dict | None, user_id) -> str:
    row = row or {}
    full = " ".join(x for x in (row.get("first_name"), row.get("last_name")) if x)
    label = full or (("@" + row["username"]) if row.get("username") else f"ID {user_id}")
    return escape_html(label)


def _profile(user: dict) -> dict:
    return {k: user.get(k) for k in ts.PROFILE_KEYS}


def _send(deps, chat, text, keyboard=None):
    deps.telegram.send_message(chat, text, keyboard)


# --- commands (privacy-mode compatible: only /commands reach the bot) ------------------------
def command_of(text: str) -> str | None:
    head = (text or "").strip().split(maxsplit=1)[0] if (text or "").strip().startswith("/") else ""
    return head.split("@", 1)[0].lower() or None


def handle_command(deps, message: dict) -> dict | None:
    """/apply /me /team /requests /help. None = not a team command (normal flow continues)."""
    cmd = command_of(message.get("text") or "")
    if cmd not in {"/apply", "/me", "/team", "/requests", "/help"}:
        return None
    user = message.get("from") or {}
    chat = (message.get("chat") or {}).get("id")
    if not user.get("id") or user.get("is_bot") or str(chat) != moderation_chat(deps.settings) \
            or not _chat_ok(deps.settings, chat):
        return {"status": "unauthorized"}
    uid = str(user["id"])
    principal = resolve(deps, chat, uid)
    if principal.can_moderate or cmd in {"/apply", "/me"}:
        try:
            deps.repo.team.save_profile(chat, uid, _profile(user))   # display metadata only
        except Exception:  # noqa: BLE001
            pass
    if cmd == "/apply":
        return _apply(deps, chat, uid, user, principal)
    if cmd == "/me":
        return _me(deps, chat, uid, principal)
    if cmd == "/help":
        return _help(deps, chat, principal)
    if not principal.can_manage_team:
        _send(deps, chat, "⛔ Управление командой доступно только владельцам EVETIS.")
        return {"status": "unauthorized"}
    try:
        return _team(deps, chat, 0) if cmd == "/team" else _requests(deps, chat, 0)
    except Exception:  # noqa: BLE001
        _send(deps, chat, "⚠️ Сервис доступа временно недоступен. Попробуйте позже.")
        return {"status": "unavailable"}


def _apply(deps, chat, uid, user, principal):
    if principal.can_moderate:
        _send(deps, chat, "✅ У Вас уже есть доступ к модерации EVETIS.")
        return {"status": "already_active"}
    if principal.status == UNAVAILABLE:
        _send(deps, chat, "⚠️ Сервис доступа временно недоступен. Попробуйте позже.")
        return {"status": "unavailable"}
    try:
        outcome, request, _ = deps.repo.team.transition("apply", chat, uid, profile=_profile(user))
    except Exception:  # noqa: BLE001
        _send(deps, chat, "⚠️ Сервис доступа временно недоступен. Попробуйте позже.")
        return {"status": "unavailable"}
    if outcome == "ACTIVE":
        _send(deps, chat, "✅ У Вас уже есть доступ к модерации EVETIS.")
        return {"status": "already_active"}
    if outcome == "PENDING":
        _send(deps, chat, "⏳ Запрос доступа уже ожидает подтверждения.")
        return {"status": "already_pending"}
    _send(deps, chat, "✅ Запрос доступа отправлен владельцам EVETIS.")
    _send(deps, chat, _request_card(request), {"inline_keyboard": [
        [{"text": "✅ Добавить оператора", "callback_data": f"ta:{uid}"}],
        [{"text": "❌ Отклонить", "callback_data": f"tr:{uid}"}]]})
    return {"status": "requested", "outcome": outcome}


def _request_card(request) -> str:
    username = f"@{escape_html(request['username'])}" if request.get("username") else "—"
    return ("👤 <b>Запрос доступа EVETIS</b>\n\n"
            f"Имя: {_name(request, request['telegram_user_id'])}\nTelegram: {username}\n"
            f"Telegram ID: {escape_html(request['telegram_user_id'])}\n\n"
            "Запрашивает доступ к модерации отзывов и вопросов.")


def _me(deps, chat, uid, principal):
    if principal.can_moderate:
        source = "\nИсточник: bootstrap" if principal.source == "bootstrap" else ""
        _send(deps, chat, f"👤 <b>Ваш доступ EVETIS</b>\n\nСтатус: ACTIVE\nРоль: {principal.role}{source}")
        return {"status": "me", "access": ts.ACTIVE}
    pending = False
    if principal.status != UNAVAILABLE and enabled(deps.settings):
        try:
            pending = (deps.repo.team.get_request(chat, uid) or {}).get("status") == ts.PENDING
        except Exception:  # noqa: BLE001
            pending = False
    if pending:
        _send(deps, chat, "👤 <b>Ваш доступ EVETIS</b>\n\nСтатус: PENDING")
        return {"status": "me", "access": ts.PENDING}
    _send(deps, chat, "👤 <b>Ваш доступ EVETIS</b>\n\nСтатус: доступа нет\nИспользуйте /apply")
    return {"status": "me", "access": NONE}


def _help(deps, chat, principal):
    if principal.can_manage_team:
        text = ("ℹ️ <b>Команды EVETIS</b>\n/me — мой доступ\n/team — команда\n/requests — запросы доступа\n"
                "/apply — запрос доступа\n/help — справка\n\nКнопки модерации доступны в карточках отзывов и вопросов.")
    elif principal.can_moderate:
        text = ("ℹ️ <b>Команды EVETIS</b>\n/me — мой доступ\n/help — справка\n\n"
                "Кнопки модерации доступны в карточках отзывов и вопросов.")
    else:
        text = "ℹ️ <b>Команды EVETIS</b>\n/apply — запросить доступ к модерации\n/me — мой доступ"
    _send(deps, chat, text)
    return {"status": "help"}


def _team(deps, chat, page, message_id=None):
    s = deps.settings
    rows = [("👑", _name(deps.repo.team.get_profile(chat, u), u), "OWNER · bootstrap", None)
            for u in sorted(bootstrap_ids(s))]
    for m in deps.repo.team.list_members(chat, limit=200):
        if m.get("status") == ts.ACTIVE and m["telegram_user_id"] not in bootstrap_ids(s):
            rows.append(("🟢", _name(m, m["telegram_user_id"]), m.get("role") or ts.OPERATOR, m["telegram_user_id"]))
    page = max(0, min(page, (len(rows) - 1) // PAGE))
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    text = "👥 <b>Команда EVETIS</b>\n\n" + "\n".join(f"{i} {n} — {r}" for i, n, r, _ in chunk)
    keyboard = [[{"text": f"👤 {n}", "callback_data": f"tm:{u}"}] for _, n, _, u in chunk if u]
    nav = ([{"text": "⬅️", "callback_data": f"tl:{page - 1}"}] if page else []) + (
        [{"text": "➡️", "callback_data": f"tl:{page + 1}"}] if (page + 1) * PAGE < len(rows) else [])
    if nav:
        keyboard.append(nav)
    _show(deps, chat, message_id, text, {"inline_keyboard": keyboard})
    return {"status": "team", "page": page}


def _requests(deps, chat, page, message_id=None):
    pending = deps.repo.team.list_requests(chat, ts.PENDING, limit=200)
    if not pending:
        _show(deps, chat, message_id, "Новых запросов доступа нет.", None)
        return {"status": "requests", "count": 0}
    page = max(0, min(page, (len(pending) - 1) // PAGE))
    chunk = pending[page * PAGE:(page + 1) * PAGE]
    text = "📨 <b>Запросы доступа</b>\n\n" + "\n".join(
        f"• {_name(r, r['telegram_user_id'])} (ID {escape_html(r['telegram_user_id'])})" for r in chunk)
    keyboard = [[{"text": f"✅ {_name(r, r['telegram_user_id'])}", "callback_data": f"ta:{r['telegram_user_id']}"},
                 {"text": "❌", "callback_data": f"tr:{r['telegram_user_id']}"}] for r in chunk]
    nav = ([{"text": "⬅️", "callback_data": f"tq:{page - 1}"}] if page else []) + (
        [{"text": "➡️", "callback_data": f"tq:{page + 1}"}] if (page + 1) * PAGE < len(pending) else [])
    if nav:
        keyboard.append(nav)
    _show(deps, chat, message_id, text, {"inline_keyboard": keyboard})
    return {"status": "requests", "count": len(pending)}


def _show(deps, chat, message_id, text, keyboard):
    if message_id:
        deps.telegram.edit_message_text(chat, message_id, text, keyboard)
    else:
        _send(deps, chat, text, keyboard)


# --- callbacks: OWNER-only team administration ------------------------------------------------
def handle_callback(deps, action, payload, chat, message_id, user_id) -> dict:
    if not enabled(deps.settings):
        return {"status": "ignored"}
    principal = resolve(deps, chat, user_id)
    if not principal.can_manage_team or str(chat) != moderation_chat(deps.settings):
        from app.utils.logging import audit_event
        audit_event("auth_denied", route="/telegram-webhook", mechanism="team_admin",
                    principal_class=principal.source, result="not_owner")
        return {"status": "unauthorized"}
    if action in {"tl", "tq"}:
        page = int(payload) if payload.isdigit() and len(payload) < 4 else 0
        try:
            return _team(deps, chat, page, message_id) if action == "tl" else _requests(deps, chat, page, message_id)
        except Exception:  # noqa: BLE001
            return {"status": "unavailable"}
    target = payload
    if not target.isdigit() or len(target) > 20:
        return {"status": "bad_request"}
    if action == "tm":
        return _member_card(deps, chat, target, message_id)
    if action == "tv":
        if target in bootstrap_ids(deps.settings):
            return {"status": "bootstrap_protected"}
        member = deps.repo.team.get_member(chat, target)
        if not member or member.get("status") != ts.ACTIVE:
            return {"status": "stale"}
        _show(deps, chat, message_id, f"Подтвердить отзыв доступа у {_name(member, target)}?", {"inline_keyboard": [
            [{"text": "⚠️ Да, отозвать", "callback_data": f"tx:{target}"}],
            [{"text": "Отмена", "callback_data": f"tm:{target}"}]]})
        return {"status": "revoke_confirmation_required"}
    if action == "tx" and target in bootstrap_ids(deps.settings):
        return {"status": "bootstrap_protected"}
    op = {"ta": "approve", "tr": "reject", "tx": "revoke"}[action]
    try:
        outcome, request, member = deps.repo.team.transition(op, chat, target, actor=principal.user_id)
    except Exception:  # noqa: BLE001 — a failed transaction changed nothing
        _send(deps, chat, "⚠️ Не удалось изменить доступ. Попробуйте ещё раз.")
        return {"status": "failed"}
    name = _name(member or request, target)
    if outcome == "APPROVED":
        _show(deps, chat, message_id, f"✅ Доступ выдан: {name} — OPERATOR.", None)
        _send(deps, chat, f"✅ {name}: доступ активирован. Теперь Вам доступны ответы на отзывы и вопросы EVETIS.")
    elif outcome == "REJECTED":
        _show(deps, chat, message_id, f"❌ Запрос доступа отклонён: {name}.", None)
    elif outcome == "REVOKED":
        _show(deps, chat, message_id, f"🚫 Доступ отозван: {name}.", None)
    return {"status": outcome.lower()}


def _member_card(deps, chat, target, message_id):
    member = deps.repo.team.get_member(chat, target)
    if target in bootstrap_ids(deps.settings) or not member:
        return {"status": "stale"}
    text = (f"👤 <b>{_name(member, target)}</b>\nРоль: {member.get('role')}\nСтатус: {member.get('status')}\n"
            f"Добавлен(а): {escape_html(str(member.get('approved_at') or '—')[:16])}\n"
            f"Добавил(а): {_name(deps.repo.team.get_profile(chat, member.get('approved_by')), member.get('approved_by'))}")
    keyboard = ([[{"text": "🚫 Отозвать доступ", "callback_data": f"tv:{target}"}]]
                if member.get("status") == ts.ACTIVE else []) + [[{"text": "⬅️ Назад", "callback_data": "tl:0"}]]
    _show(deps, chat, message_id, text, {"inline_keyboard": keyboard})
    return {"status": "member"}
