"""R2.4A Telegram team access: members, access requests, append-only audit, display profiles.

Logical identity is (chat_id, telegram_user_id). State transitions (apply, approve, reject,
revoke) are single transactions that also append the audit entry, so a failure never leaves a
half-approved state. Display metadata (username / names) is never used for authorization.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
OWNER, OPERATOR = "OWNER", "OPERATOR"
ACTIVE, REVOKED = "ACTIVE", "REVOKED"
PENDING, APPROVED, REJECTED = "PENDING", "APPROVED", "REJECTED"
MEMBERS, REQUESTS, AUDIT, PROFILES = ("telegram_team_members", "telegram_team_requests",
                                      "telegram_team_audit", "telegram_team_profiles")
PROFILE_KEYS = ("username", "first_name", "last_name")


def key(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile(profile: dict | None) -> dict:
    return {k: (str((profile or {}).get(k))[:64] if (profile or {}).get(k) else None) for k in PROFILE_KEYS}


def _audit(chat_id, actor, target, action, before, after, role) -> dict:
    return {"event_id": uuid.uuid4().hex, "chat_id": str(chat_id), "actor_user_id": str(actor),
            "target_user_id": str(target), "action": action, "before_status": before, "after_status": after,
            "role": role, "timestamp": _now(), "schema_version": SCHEMA_VERSION}


# --- pure transitions: (member, request) -> (outcome, member', request', audit) ----------------
def _apply(chat_id, user_id, profile, member, request):
    if member and member.get("status") == ACTIVE:
        return "ACTIVE", None, None, None
    if request and request.get("status") == PENDING:
        return "PENDING", None, None, None
    now = _now()
    attempts = int((request or {}).get("attempt_count") or 0) + 1
    new = {"chat_id": str(chat_id), "telegram_user_id": str(user_id), **_profile(profile), "status": PENDING,
           "requested_at": now, "resolved_at": None, "resolved_by": None, "attempt_count": attempts,
           "schema_version": SCHEMA_VERSION}
    audit = _audit(chat_id, user_id, user_id, "ACCESS_REQUESTED", (request or {}).get("status"), PENDING, OPERATOR)
    return ("REOPENED" if request else "CREATED"), None, new, audit


def _approve(chat_id, user_id, actor, member, request):
    if not request:
        return "NOT_FOUND", None, None, None
    if request.get("status") == APPROVED and member and member.get("status") == ACTIVE:
        return "ALREADY", None, None, None
    if request.get("status") != PENDING:
        return "NOT_PENDING", None, None, None
    now = _now()
    new_member = {**(member or {}), "chat_id": str(chat_id), "telegram_user_id": str(user_id),
                  **{k: request.get(k) for k in PROFILE_KEYS}, "role": OPERATOR, "status": ACTIVE,
                  "approved_by": str(actor), "approved_at": now, "revoked_by": None, "revoked_at": None,
                  "created_at": (member or {}).get("created_at") or now, "updated_at": now, "source": "telegram_apply",
                  "schema_version": SCHEMA_VERSION}
    new_request = {**request, "status": APPROVED, "resolved_at": now, "resolved_by": str(actor)}
    audit = _audit(chat_id, actor, user_id, "ACCESS_APPROVED", (member or {}).get("status"), ACTIVE, OPERATOR)
    return "APPROVED", new_member, new_request, audit


def _reject(chat_id, user_id, actor, member, request):
    if not request:
        return "NOT_FOUND", None, None, None
    if request.get("status") == REJECTED:
        return "ALREADY", None, None, None
    if request.get("status") != PENDING:
        return "NOT_PENDING", None, None, None
    new_request = {**request, "status": REJECTED, "resolved_at": _now(), "resolved_by": str(actor)}
    audit = _audit(chat_id, actor, user_id, "ACCESS_REJECTED", PENDING, REJECTED, OPERATOR)
    return "REJECTED", None, new_request, audit


def _revoke(chat_id, user_id, actor, member, request):
    if not member:
        return "NOT_FOUND", None, None, None
    if member.get("status") == REVOKED:
        return "ALREADY", None, None, None
    now = _now()
    new_member = {**member, "status": REVOKED, "revoked_by": str(actor), "revoked_at": now, "updated_at": now}
    audit = _audit(chat_id, actor, user_id, "ACCESS_REVOKED", ACTIVE, REVOKED, member.get("role"))
    return "REVOKED", new_member, None, audit


_TRANSITIONS = {"apply": _apply, "approve": _approve, "reject": _reject, "revoke": _revoke}


class MemoryTeamStore:
    def __init__(self):
        self.members, self.requests, self.audit, self.profiles = {}, {}, [], {}
        self.fail_on_commit = False   # test hook: a failed transaction leaves no partial state

    def get_member(self, chat_id, user_id):
        return copy.deepcopy(self.members.get(key(chat_id, user_id)))

    def get_request(self, chat_id, user_id):
        return copy.deepcopy(self.requests.get(key(chat_id, user_id)))

    def list_members(self, chat_id, limit=50):
        rows = [m for m in self.members.values() if m["chat_id"] == str(chat_id)]
        return copy.deepcopy(sorted(rows, key=lambda m: m.get("created_at") or "")[:limit])

    def list_requests(self, chat_id, status=PENDING, limit=50):
        rows = [r for r in self.requests.values() if r["chat_id"] == str(chat_id) and r["status"] == status]
        return copy.deepcopy(sorted(rows, key=lambda r: r.get("requested_at") or "")[:limit])

    def get_profile(self, chat_id, user_id):
        return copy.deepcopy(self.profiles.get(key(chat_id, user_id)))

    def save_profile(self, chat_id, user_id, profile):
        self.profiles[key(chat_id, user_id)] = {"chat_id": str(chat_id), "telegram_user_id": str(user_id),
                                                **_profile(profile), "updated_at": _now()}

    def transition(self, op, chat_id, user_id, *, actor=None, profile=None):
        k = key(chat_id, user_id)
        member, request = copy.deepcopy(self.members.get(k)), copy.deepcopy(self.requests.get(k))
        args = (chat_id, user_id, profile) if op == "apply" else (chat_id, user_id, actor)
        outcome, new_member, new_request, audit = _TRANSITIONS[op](*args, member, request)
        if self.fail_on_commit and audit:
            raise RuntimeError("simulated transaction failure")
        if new_member is not None:
            self.members[k] = new_member
        if new_request is not None:
            self.requests[k] = new_request
        if audit:
            self.audit.append(audit)
        return outcome, copy.deepcopy(new_request or request), copy.deepcopy(new_member or member)


class FirestoreTeamStore:
    def __init__(self, client_factory):
        self._client = client_factory

    def _ref(self, col, chat_id, user_id):
        return self._client().collection(col).document(key(chat_id, user_id))

    def get_member(self, chat_id, user_id):
        snap = self._ref(MEMBERS, chat_id, user_id).get()
        return snap.to_dict() if snap.exists else None

    def get_request(self, chat_id, user_id):
        snap = self._ref(REQUESTS, chat_id, user_id).get()
        return snap.to_dict() if snap.exists else None

    def list_members(self, chat_id, limit=50):
        query = self._client().collection(MEMBERS).where("chat_id", "==", str(chat_id)).limit(limit)
        return sorted((s.to_dict() for s in query.stream()), key=lambda m: m.get("created_at") or "")

    def list_requests(self, chat_id, status=PENDING, limit=50):
        query = (self._client().collection(REQUESTS).where("chat_id", "==", str(chat_id))
                 .where("status", "==", status).limit(limit))
        return sorted((s.to_dict() for s in query.stream()), key=lambda r: r.get("requested_at") or "")

    def get_profile(self, chat_id, user_id):
        snap = self._ref(PROFILES, chat_id, user_id).get()
        return snap.to_dict() if snap.exists else None

    def save_profile(self, chat_id, user_id, profile):
        self._ref(PROFILES, chat_id, user_id).set({"chat_id": str(chat_id), "telegram_user_id": str(user_id),
                                                   **_profile(profile), "updated_at": _now()})

    def transition(self, op, chat_id, user_id, *, actor=None, profile=None):
        from google.cloud import firestore
        client = self._client()
        member_ref, request_ref = self._ref(MEMBERS, chat_id, user_id), self._ref(REQUESTS, chat_id, user_id)

        @firestore.transactional
        def txn(transaction):
            m, r = member_ref.get(transaction=transaction), request_ref.get(transaction=transaction)
            member, request = (m.to_dict() if m.exists else None), (r.to_dict() if r.exists else None)
            args = (chat_id, user_id, profile) if op == "apply" else (chat_id, user_id, actor)
            outcome, new_member, new_request, audit = _TRANSITIONS[op](*args, member, request)
            if new_member is not None:
                transaction.set(member_ref, new_member)
            if new_request is not None:
                transaction.set(request_ref, new_request)
            if audit:
                transaction.create(client.collection(AUDIT).document(audit["event_id"]), audit)
            return outcome, (new_request or request), (new_member or member)

        return txn(client.transaction())
