"""FBO transport contract (B.FBO_TRANSPORT_CONTRACT) against a deterministic replay of the
2026-11-09 read-only probe. No network: the source is a model whose semantics are pinned to the
recorded observations (fixtures/fbo_v3_transport_contract.json), and the engine is checked
against that model: in_process_at ownership, no duplicates across adjacent windows, the
sub-millisecond boundary, status refresh by MERGE, v3 commission parsing.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import backfill_core as B
import backfill as F
import common as C

FIXTURE = json.loads((Path(__file__).resolve().parent / "fixtures" / "fbo_v3_transport_contract.json").read_text())
PINNED_CONTRACT = "c4a3735b7ea12a9198bfad3385a7a85e441ef0e3729e1d0e73e157c2274abce1"


def us(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    d = t - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def iso(micros):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)).isoformat().replace("+00:00", "Z")


def posting(number, created, in_process, status="delivered", sku=1000001):
    p = copy.deepcopy(FIXTURE["posting_v3_shape"])
    p.update(posting_number=number, created_at=created, in_process_at=in_process, status=status)
    for x in p["products"]:
        x["sku"] = sku
    for x in p["financial_data"]["products"]:
        x["product_id"] = sku
    return p


def probe_population():
    """Targets plus synthetic neighbours at the observed per-window counts.

    Neighbours have created_at == in_process_at, so only the targets discriminate the filter field.
    X_inproc: 19 neighbours, 3 of them inside the X_edge_since_next_ms window.
    """
    out = [posting(n, t["created_at"], t["in_process_at"], t["status"]) for n, t in FIXTURE["targets"].items()]
    fill = {"X_created": 6, "Y_created": 7, "Y_inproc": 13, "Z_created": 15, "Z_inproc": 14}
    for obs in FIXTURE["observations"]:
        n = fill.get(obs["label"], 0)
        a, b = us(obs["since"]), us(obs["to"])
        for k in range(n):
            t = iso(a + (b - a) * (k + 1) // (n + 2))
            out.append(posting(f"SYN-{obs['label']}-{k}", t, t))
    base = us("2026-09-15T14:50:10Z")
    for k in range(16):
        t = iso(base + k * 10_000_000)
        out.append(posting(f"SYN-XI-{k}", t, t))
    for k, t in enumerate(("2026-09-15T14:57:00.000001Z", "2026-09-15T14:57:10Z", "2026-09-15T14:57:20.5Z")):
        out.append(posting(f"SYN-XE-{k}", t, t))
    return out


class Source:
    """Model of /v3/posting/fbo/list: closed [since, to] on `field` at microsecond precision,
    ordered by in_process_at ASC, limit in (0, 100], opaque offset cursor."""
    def __init__(self, postings, field="in_process_at"):
        self.postings, self.field, self.calls = postings, field, []

    def __call__(self, path, body):
        assert path == "/v3/posting/fbo/list"
        self.calls.append(copy.deepcopy(body))
        if not 0 < body["limit"] <= 100:
            return 400, {"code": 3, "message": "Request validation error: invalid PostingFboListRequest.Limit: value must be inside range (0, 100]"}
        a, b = us(body["filter"]["since"]), us(body["filter"]["to"])
        hit = sorted((p for p in self.postings if a <= us(p[self.field]) <= b), key=lambda p: us(p["in_process_at"]))
        start = int(body.get("cursor") or 0)
        page = hit[start:start + body["limit"]]
        more = start + body["limit"] < len(hit)
        return 200, {"postings": copy.deepcopy(page), "has_next": more, "cursor": str(start + body["limit"]) if more else ""}


def replay(source):
    out = {}
    for obs in FIXTURE["observations"]:
        code, d = source("/v3/posting/fbo/list", {"cursor": "", "filter": {"since": obs["since"], "to": obs["to"]}, "limit": obs["limit"]})
        out[obs["label"]] = (code, d)
    return out


def test_model_reproduces_every_recorded_observation():
    for label, (code, d) in replay(Source(probe_population())).items():
        obs = next(o for o in FIXTURE["observations"] if o["label"] == label)
        assert code == obs["http"], label
        if code != 200:
            assert d["code"] == obs["error_code"] and obs["error_fragment"] in d["message"]
            continue
        names = [p["posting_number"] for p in d["postings"]]
        assert len(names) == obs["count"] and d["has_next"] is obs["has_next"], label
        assert sorted(n for n in names if n in FIXTURE["targets"]) == obs["targets_present"], label
        assert [us(p["in_process_at"]) for p in d["postings"]] == sorted(us(p["in_process_at"]) for p in d["postings"])


def test_created_at_filter_cannot_explain_the_observations():
    # The rejected hypothesis: with created_at as the filter field every target lands in its
    # created_at window and leaves its in_process_at window, contradicting the probe.
    results = replay(Source(probe_population(), field="created_at"))
    assert any(sorted(p["posting_number"] for p in results[o["label"]][1]["postings"] if p["posting_number"] in FIXTURE["targets"])
               != o["targets_present"] for o in FIXTURE["observations"] if o["http"] == 200)


def test_contract_constant_is_pinned():
    c = B.FBO_TRANSPORT_CONTRACT
    assert B.FBO_TRANSPORT_CONTRACT_HASH == B.digest(c) == PINNED_CONTRACT
    assert (c["filter_field"], c["limit_max"], c["merge_key"]) == ("in_process_at", 100, ["posting_number", "sku"])


# ───────────────────────────── engine against the model
def env(**kw):
    return dict(dict(BACKFILL_MODE=B.VERSION, TENANT_BINDING_REQUIRED="1", STRICT_PAGE_CAPS="1",
                BACKFILL_TARGET_PROJECT="mpa-t-example", SINCE="2026-08-19", UNTIL="2026-08-20",
                BACKFILL_GENERATION="contract-v1", BACKFILL_ORIGIN="2026-10-04T00:00:00Z", BACKFILL_WINDOW_DAYS="1"), **kw)


def plan(**kw):
    return B.plan(env(**kw), "fbo_postings", "mpa-t-example", "ozon_raw", "ref", date(2026, 10, 4))


@pytest.fixture
def harness(entities, monkeypatch):
    db, proofs = {}, []
    monkeypatch.setattr(F.time, "sleep", lambda _: None)
    def merge(table, rows, keys, run_id, **kw):
        cols = set().union(*(r.keys() for r in rows)) if rows else set()
        C.validate_merge_batch(table, rows, keys, sorted(cols), kw.get("on_duplicate_key", "collapse_identical"))
        target = db.setdefault(table, {})
        inserted = 0
        for row in rows:
            key = C.merge_key(row, keys)
            inserted += key not in target
            target[key] = copy.deepcopy(row)
        return {"received": len(rows), "inserted": inserted, "updated": len(rows) - inserted}
    monkeypatch.setattr(C, "merge_rows", merge)
    return db, proofs, lambda result, evidence: proofs.append(copy.deepcopy(evidence))


def run(p, source, monkeypatch, persist, state=None):
    monkeypatch.setattr(C, "seller_post", source)
    return F.Engine(p, "rt-synthetic-unique", "2026-10-04T10:00:00+03:00", state or B.initial(p)).run(persist)


A = B.utc_ms("2026-08-19")            # 2026-08-18T21:00:00Z
MID = B.utc_ms("2026-08-20")          # leaf boundary
END = B.utc_ms("2026-08-21")


def boundary_population():
    t = lambda ms, d=0: iso(ms * 1000 + d)
    return [
        posting("SYN-269", "2026-08-18T05:16:22.460142Z", t(A, 3_600_000_000)),     # leaf-269 shape: created 16 h before a
        posting("SYN-SUBMS", t(MID, -500), t(MID, -500)),                           # (b - 1 ms, b): lost by to = b - 1 ms
        posting("SYN-AT-B", t(MID, -10), t(MID)),                                    # exactly at the leaf boundary
        posting("SYN-LATE", t(A, 7_200_000_000), t(MID, 3_600_000_000), status="cancelled"),
        posting("SYN-OUT", t(END), t(END)),                                          # next plan, never requested here
    ]


def test_adjacent_leaves_own_each_posting_exactly_once(harness, monkeypatch):
    db, proofs, persist = harness
    source = Source(boundary_population())
    out = run(plan(), source, monkeypatch, persist)
    assert out["evidence"]["complete"]
    rows = db["RAW_OZON_POSTINGS_FBO"]
    assert sorted(k[0] for k in rows) == ["SYN-269", "SYN-AT-B", "SYN-LATE", "SYN-SUBMS"]
    assert [c["filter"] for c in source.calls] == [{"since": B.stamp(A), "to": B.stamp(MID)}, {"since": B.stamp(MID), "to": B.stamp(END)}]
    details = [p["detail"] for p in proofs if p["detail"]["action"] == "WINDOW_COMPLETE"]
    assert [d["boundary_deferred"] for d in details] == [1, 1]   # SYN-AT-B, then SYN-OUT at the plan end
    assert sum(d["product_rows"] for d in details) == len(rows) == 4
    assert {k: v for d in details for k, v in d["by_moscow_day"].items()} == {"2026-08-19": 2, "2026-08-20": 2}
    assert details[0]["created_before_window"] == 1 and {d["fbo_contract"] for d in details} == {PINNED_CONTRACT}


def test_previous_request_end_lost_the_submillisecond_tail():
    # Regression evidence: the former `to = b - 1 ms` request never returns SYN-SUBMS, and the
    # next leaf starts at b; under the recorded sub-millisecond semantics it belonged to no leaf.
    src = Source(boundary_population())
    _, left = src("/v3/posting/fbo/list", {"cursor": "", "filter": {"since": B.stamp(A), "to": B.stamp(MID - 1)}, "limit": 100})
    _, right = src("/v3/posting/fbo/list", {"cursor": "", "filter": {"since": B.stamp(MID), "to": B.stamp(END - 1)}, "limit": 100})
    assert "SYN-SUBMS" not in {p["posting_number"] for p in left["postings"] + right["postings"]}


def test_out_of_contract_record_fails_closed_without_merge(harness, monkeypatch):
    db, proofs, persist = harness
    bad = posting("SYN-BAD", "2026-08-18T22:00:00Z", iso(MID * 1000 + 1))
    source = lambda path, body: (200, {"postings": [bad], "has_next": False, "cursor": ""})
    with pytest.raises(B.FboWindowError, match="OUTSIDE_REQUESTED_INTERVAL") as raised:
        run(plan(), source, monkeypatch, persist)
    assert not db and all(p["detail"]["action"] == "SPLIT_CONFIGURED_WINDOW" for p in proofs)
    assert raised.value.fbo_window_evidence["requested_utc_to"] == B.stamp(MID)


@pytest.mark.parametrize("field,value,category", [("in_process_at", None, "INVALID_TIMESTAMP_FORMAT"),
    ("created_at", "2026-08-19T10:00:00", "MISSING_TIMEZONE"), ("created_at", "bad", "INVALID_TIMESTAMP_FORMAT")])
def test_both_timestamps_must_be_well_formed(harness, monkeypatch, field, value, category):
    db, proofs, persist = harness
    p0 = posting("SYN-1", "2026-08-19T00:00:00Z", "2026-08-19T00:00:01Z")
    p0[field] = value
    with pytest.raises(B.FboWindowError, match=category):
        run(plan(), lambda path, body: (200, {"postings": [p0], "has_next": False, "cursor": ""}), monkeypatch, persist)
    assert not db


def test_status_and_cancellation_refresh_by_merge_without_duplicates(harness, monkeypatch):
    db, proofs, persist = harness
    population = boundary_population()
    run(plan(), Source(population), monkeypatch, persist)
    before = copy.deepcopy(db["RAW_OZON_POSTINGS_FBO"])
    for p in population:
        if p["posting_number"] == "SYN-269":
            p["status"], p["cancel_reason_id"] = "cancelled", 352
    run(plan(BACKFILL_GENERATION="contract-refresh"), Source(population), monkeypatch, persist)
    after = db["RAW_OZON_POSTINGS_FBO"]
    assert set(after) == set(before) and len(after) == 4
    row = next(v for k, v in after.items() if k[0] == "SYN-269")
    assert (row["status"], row["cancel_reason_id"]) == ("cancelled", 352)


def test_v3_commission_object_and_v2_scalar(entities):
    v3 = posting("SYN-C", "2026-08-19T00:00:00Z", "2026-08-19T00:00:01Z")
    v2 = copy.deepcopy(v3)
    v2["financial_data"]["products"][0].pop("commission")
    v2["financial_data"]["products"][0]["commission_amount"] = 12.5
    r3, r2 = (entities._fbo_rows([x], "rt", "2026-10-04T10:00:00+03:00", v3_commission=True)[0] for x in (v3, v2))
    assert float(r3["commission_amount_rub"]) == 298.5 and float(r2["commission_amount_rub"]) == 12.5
    assert float(r3["price_rub"]) == 1299 and r3["in_process_at"] == "2026-08-19T00:00:01Z"


# ───────────────────────────── EVETIS isolation
def test_default_fbo_rows_are_byte_identical_to_the_base_commit(entities):
    """EVETIS path (entities.fbo_postings → _fbo_rows default) must not change on a tenant's
    behalf. The fixture was generated by the base commit's code (1a2b61a0) on the same inputs."""
    base = json.loads((Path(__file__).resolve().parent / "fixtures" / "fbo_rows_evetis_baseline.json").read_text())
    rows = entities._fbo_rows(base["inputs"], "rt-evetis-baseline", "2026-10-04T10:00:00+03:00")
    assert json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str) == \
        json.dumps(base["rows"], ensure_ascii=False, sort_keys=True, default=str)
    assert base["rows"][0]["commission_amount_rub"] is None      # v3 object stays unparsed for EVETIS


def test_evetis_entity_path_keeps_its_request_and_rows(entities, monkeypatch):
    base = json.loads((Path(__file__).resolve().parent / "fixtures" / "fbo_rows_evetis_baseline.json").read_text())
    bodies, merged = [], []
    monkeypatch.setattr(entities.C, "STRICT_PAGE_CAPS", False, raising=False)
    monkeypatch.setattr(entities, "seller_post", lambda path, body: bodies.append(body) or (200, {"postings": base["inputs"], "has_next": False, "cursor": ""}))
    monkeypatch.setattr(entities, "merge_rows", lambda table, rows, keys, run_id: merged.extend(rows) or {"received": len(rows)})
    entities.fbo_postings("rt-evetis-baseline", "2026-10-04T10:00:00+03:00", "2026-09-01", "2026-09-02")
    assert bodies[0]["filter"] == {"since": "2026-09-01T00:00:00.000Z", "to": "2026-09-02T23:59:59.000Z"}
    assert json.dumps(merged, sort_keys=True, default=str) == json.dumps(base["rows"], sort_keys=True, default=str)


def test_strict_tenant_path_reads_the_v3_commission(entities):
    base = json.loads((Path(__file__).resolve().parent / "fixtures" / "fbo_rows_evetis_baseline.json").read_text())
    rows = entities._fbo_rows(base["inputs"], "rt", "ts", v3_commission=True)
    assert [r["commission_amount_rub"] for r in rows] == [298.5, 12.5, None]
