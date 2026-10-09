"""R2.4A: the REAL FirestoreTeamStore against a fake Firestore client + transaction (no GCP).
Writes inside a transaction are buffered and applied only on commit, like Firestore; a failure
anywhere before commit leaves nothing behind. Query filters are recorded to prove the shape
needs no composite index."""
import copy

import pytest

from app.services import team_store as ts
from app.services.team_store import FirestoreTeamStore

GROUP, OWNER, MARIA = "-1009999", "302044578", "555000111"


class Snap:
    def __init__(self, data):
        self.exists, self._data = data is not None, copy.deepcopy(data)

    def to_dict(self):
        return copy.deepcopy(self._data)


class Ref:
    def __init__(self, db, col, doc_id):
        self.db, self.col, self.id = db, col, doc_id

    def get(self, transaction=None):
        return Snap(self.db.data.get((self.col, self.id)))

    def set(self, data):
        self.db.data[(self.col, self.id)] = copy.deepcopy(data)


class Query:
    def __init__(self, db, col, filters=(), limit=None):
        self.db, self.col, self.filters, self._limit = db, col, list(filters), limit

    def where(self, field, op, value):
        self.db.queries.append((self.col, field, op))
        return Query(self.db, self.col, self.filters + [(field, op, value)], self._limit)

    def limit(self, n):
        return Query(self.db, self.col, self.filters, n)

    def order_by(self, *a, **k):  # never used by the adapter (would need a composite index)
        raise AssertionError("order_by would require a composite index")

    def stream(self):
        rows = [Snap(v) for (c, _), v in sorted(self.db.data.items()) if c == self.col
                and all(op == "==" and v.get(f) == val for f, op, val in self.filters)]
        return rows[: self._limit] if self._limit else rows


class Collection(Query):
    def document(self, doc_id):
        return Ref(self.db, self.col, doc_id)


class Tx:
    def __init__(self, db):
        self.db, self.writes = db, []

    def set(self, ref, data):
        self.writes.append(("set", ref, copy.deepcopy(data)))

    def create(self, ref, data):
        if (ref.col, ref.id) in self.db.data:
            raise RuntimeError("already exists")
        if self.db.fail_on_create:
            raise RuntimeError("simulated failure inside the transaction")
        self.writes.append(("create", ref, copy.deepcopy(data)))

    def commit(self):
        if self.db.fail_on_commit:
            raise RuntimeError("simulated commit failure")
        for _, ref, data in self.writes:
            self.db.data[(ref.col, ref.id)] = data
        self.db.committed.append([(kind, ref.col) for kind, ref, _ in self.writes])


class DB:
    def __init__(self):
        self.data, self.queries, self.committed = {}, [], []
        self.fail_on_commit = self.fail_on_create = False

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Tx(self)


@pytest.fixture
def db(monkeypatch):
    from google.cloud import firestore

    def transactional(fn):
        def run(tx):
            result = fn(tx)
            tx.commit()
            return result
        return run
    monkeypatch.setattr(firestore, "transactional", transactional)
    return DB()


def store(db):
    return FirestoreTeamStore(lambda: db)


def audits(db):
    return [v for (c, _), v in db.data.items() if c == ts.AUDIT]


def test_apply_request_and_audit_in_one_transaction(db):
    outcome, request, _ = store(db).transition("apply", GROUP, MARIA, profile={"first_name": "Мария"})
    assert outcome == "CREATED" and request["status"] == "PENDING"
    assert db.committed == [[("set", ts.REQUESTS), ("create", ts.AUDIT)]]
    assert [a["action"] for a in audits(db)] == ["ACCESS_REQUESTED"]
    assert store(db).transition("apply", GROUP, MARIA)[0] == "PENDING"
    assert db.committed[-1] == [] and len(audits(db)) == 1          # a repeat writes nothing


def test_approve_request_member_audit_in_one_transaction(db):
    s = store(db)
    s.transition("apply", GROUP, MARIA, profile={"first_name": "Мария"})
    outcome, request, member = s.transition("approve", GROUP, MARIA, actor=OWNER)
    assert outcome == "APPROVED"
    assert db.committed[-1] == [("set", ts.MEMBERS), ("set", ts.REQUESTS), ("create", ts.AUDIT)]
    assert s.get_request(GROUP, MARIA)["status"] == "APPROVED"
    m = s.get_member(GROUP, MARIA)
    assert m["status"] == "ACTIVE" and m["role"] == "OPERATOR" and m["approved_by"] == OWNER
    assert [a["action"] for a in audits(db)].count("ACCESS_APPROVED") == 1


@pytest.mark.parametrize("failure", ["fail_on_create", "fail_on_commit"])
def test_failed_approve_leaves_no_partial_state(db, failure):
    s = store(db)
    s.transition("apply", GROUP, MARIA)
    before = copy.deepcopy(db.data)
    setattr(db, failure, True)
    with pytest.raises(RuntimeError):
        s.transition("approve", GROUP, MARIA, actor=OWNER)
    assert db.data == before
    assert s.get_member(GROUP, MARIA) is None and s.get_request(GROUP, MARIA)["status"] == "PENDING"


def test_duplicate_approve_no_duplicate_audit(db):
    s = store(db)
    s.transition("apply", GROUP, MARIA)
    s.transition("approve", GROUP, MARIA, actor=OWNER)
    n = len(audits(db))
    assert s.transition("approve", GROUP, MARIA, actor=OWNER)[0] == "ALREADY"
    assert len(audits(db)) == n and db.committed[-1] == []


def test_reject_request_and_audit_atomic(db):
    s = store(db)
    s.transition("apply", GROUP, MARIA)
    assert s.transition("reject", GROUP, MARIA, actor=OWNER)[0] == "REJECTED"
    assert db.committed[-1] == [("set", ts.REQUESTS), ("create", ts.AUDIT)]
    assert s.get_request(GROUP, MARIA)["status"] == "REJECTED" and s.get_member(GROUP, MARIA) is None
    assert s.transition("reject", GROUP, MARIA, actor=OWNER)[0] == "ALREADY"
    assert [a["action"] for a in audits(db)].count("ACCESS_REJECTED") == 1


def test_revoke_atomic_and_duplicate_revoke_no_duplicate_audit(db):
    s = store(db)
    s.transition("apply", GROUP, MARIA)
    s.transition("approve", GROUP, MARIA, actor=OWNER)
    assert s.transition("revoke", GROUP, MARIA, actor=OWNER)[0] == "REVOKED"
    assert db.committed[-1] == [("set", ts.MEMBERS), ("create", ts.AUDIT)]
    m = s.get_member(GROUP, MARIA)
    assert m["status"] == "REVOKED" and m["revoked_by"] == OWNER
    assert s.transition("revoke", GROUP, MARIA, actor=OWNER)[0] == "ALREADY"
    assert [a["action"] for a in audits(db)].count("ACCESS_REVOKED") == 1


def test_queries_are_equality_only_and_compatible_with_team_access(db):
    """End to end through the pipeline with the Firestore adapter: /apply → approve → /team →
    operator moderates → revoke → denied. Only equality filters, no order_by: Firestore serves
    them from automatic single-field indexes, no composite index is needed."""
    from app.services.pipeline import handle_update, run_poll
    from tests.quality.test_r24a_team_access import FixedV2, buttons, fb, make_deps, press, say
    d = make_deps([fb()], openai=FixedV2(), v31_only_operator_enabled=True, v31_owner_override_enabled=True,
                  telegram_chat_id=GROUP, telegram_allowed_user_ids={OWNER}, telegram_dynamic_access_enabled=True)
    d.publication_validator = None
    d.repo.team = store(db)
    assert say(d, "/apply", MARIA, first_name="Мария")["status"] == "requested"
    assert say(d, "/requests", OWNER, first_name="Евгений")["count"] == 1
    assert press(d, f"ta:{MARIA}", OWNER)["status"] == "approved"
    assert say(d, "/team", OWNER, first_name="Евгений")["status"] == "team" and f"tm:{MARIA}" in buttons(d)
    run_poll(d)
    doc_id = next(iter(d.repo.docs))
    assert press(d, f"skip:{doc_id}", MARIA)["status"] == "skipped"
    assert press(d, f"tx:{MARIA}", OWNER)["status"] == "revoked"
    assert say(d, "/me", MARIA)["access"] == "NONE"
    assert db.queries and all(op == "==" for _, _, op in db.queries)
    assert {(c, f) for c, f, _ in db.queries} <= {(ts.MEMBERS, "chat_id"), (ts.REQUESTS, "chat_id"),
                                                 (ts.REQUESTS, "status")}
