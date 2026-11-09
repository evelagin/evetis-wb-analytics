"""FBO returns loader (backfill.returns_window) against a deterministic replay of the
2026-11-09 read-only probe of /v1/returns/list (fixtures/returns_v1_transport_contract.json).
No network. Dormant until the consolidated release wires the domain and RAW_OZON_RETURNS.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backfill_core as B
import backfill as F

HERE = Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixtures" / "returns_v1_transport_contract.json").read_text())
SCHEMA = json.loads((HERE.parents[0] / "schema" / "ozon_raw" / "RAW_OZON_RETURNS.json").read_text())
PINNED_CONTRACT = B.digest(B.RETURNS_TRANSPORT_CONTRACT)


def us(value):
    t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    d = t - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds


def iso(micros):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)).isoformat().replace("+00:00", "Z")


def item(rid, moment, status="ReturnedToOzon", **product):
    r = copy.deepcopy(FIXTURE["return_shape"])
    r["id"] = rid
    r["visual"]["change_moment"] = moment
    r["visual"]["status"]["sys_name"] = status
    r["product"].update(product)
    return r


class Source:
    """Model of /v1/returns/list: closed [time_from, time_to] on visual.change_moment at
    microsecond precision, ordered by change_moment ASC, last_id continues after that id."""
    def __init__(self, items):
        self.items, self.calls = items, []

    def __call__(self, path, body):
        assert path == "/v1/returns/list" and body["filter"]["return_schema"] == "FBO"
        self.calls.append(copy.deepcopy(body))
        if not 0 < body["limit"] <= 500:
            return {"code": 3}
        w = body["filter"]["visual_status_change_moment"]
        a, b = us(w["time_from"]), us(w["time_to"])
        hit = sorted((r for r in self.items if a <= us(r["visual"]["change_moment"]) <= b),
                     key=lambda r: us(r["visual"]["change_moment"]))
        start = 0
        if body["last_id"]:
            start = next(k for k, r in enumerate(hit) if r["id"] == body["last_id"]) + 1
        page = hit[start:start + body["limit"]]
        return {"returns": copy.deepcopy(page), "has_next": start + body["limit"] < len(hit)}


def day_population():
    return [item(x["id"], x["change_moment"], x["status"]) for x in FIXTURE["day_items"]]


def call_with(source):
    return lambda path, body: source(path, body)


def test_model_reproduces_every_recorded_observation():
    src, day = Source(day_population()), FIXTURE["day_window"]
    ids = [x["id"] for x in FIXTURE["day_items"]]
    body = lambda limit, last=0, w=day: {"filter": {"visual_status_change_moment": w, "return_schema": "FBO"}, "limit": limit, "last_id": last}
    r1 = src("/v1/returns/list", body(500))
    assert len(r1["returns"]) == 181 and r1["has_next"] is False
    assert [r["visual"]["change_moment"] for r in r1["returns"]] == sorted(x["change_moment"] for x in FIXTURE["day_items"])
    p1 = src("/v1/returns/list", body(50))
    p2 = src("/v1/returns/list", body(50, p1["returns"][-1]["id"]))
    assert [r["id"] for r in p1["returns"]] == ids[:50] and [r["id"] for r in p2["returns"]] == ids[50:100]
    assert p1["has_next"] and p2["has_next"]
    r4, r5 = FIXTURE["observations"][3], FIXTURE["observations"][4]
    got4 = src("/v1/returns/list", body(500, w={"time_from": r4["time_from"], "time_to": r4["time_to"]}))["returns"]
    got5 = src("/v1/returns/list", body(500, w={"time_from": r5["time_from"], "time_to": r5["time_to"]}))["returns"]
    assert [r["id"] for r in got4] == [ids[r4["present_day_index"]]] and got5 == []


def test_contract_constant():
    c = B.RETURNS_TRANSPORT_CONTRACT
    assert B.RETURNS_TRANSPORT_CONTRACT_HASH == PINNED_CONTRACT
    assert (c["filter_field"], c["limit"], c["merge_key"]) == ("visual_status_change_moment", 500, ["return_id"])


A = int(datetime(2026, 10, 31, 21, tzinfo=timezone.utc).timestamp() * 1000)     # 01.11 00:00 MSK
MID = A + 86_400_000
END = MID + 86_400_000


def test_day_window_rows_match_the_table_schema():
    rows, detail = F.returns_window(call_with(Source(day_population())), A, MID, "rt", "2026-11-09T10:00:00+03:00")
    # Items between 21:00Z on 01.11 and 24:00Z belong to the 02.11 MSK window.
    owned = [x for x in FIXTURE["day_items"] if us(x["change_moment"]) < MID * 1000]
    assert len(rows) == len(owned) == detail["returns"] and detail["source_terminal"]
    assert sum(detail["by_moscow_day"].values()) == len(rows) and set(detail["by_moscow_day"]) == {"2026-11-01"}
    columns = [f["name"] for f in SCHEMA["schema"]]
    assert all(set(r) == set(columns) for r in rows)
    required = [f["name"] for f in SCHEMA["schema"] if f["mode"] == "REQUIRED"]
    assert all(r[k] is not None for r in rows for k in required)
    assert len({r["return_id"] for r in rows}) == len(rows) and detail["returns_contract"] == PINNED_CONTRACT


def test_adjacent_windows_paged_without_gap_or_duplicate():
    base = A * 1000
    population = [item(10_000 + k, iso(base + 50_000 + k * 100_000_000)) for k in range(1500)]   # ~41.7 h, off-boundary
    population.append(item(1, iso(MID * 1000)))                                          # exactly at the boundary
    population.append(item(2, iso(MID * 1000 - 1)))                                      # 1 us before it
    src = Source(population)
    left, dl = F.returns_window(call_with(src), A, MID, "rt", "ts")
    right, dr = F.returns_window(call_with(src), MID, END, "rt", "ts")
    ids = [r["return_id"] for r in left + right]
    assert sorted(ids) == sorted(r["id"] for r in population) and len(ids) == len(set(ids))
    assert 1 in {r["return_id"] for r in right} and 2 in {r["return_id"] for r in left}
    assert dl["boundary_deferred"] == 1 and dl["pages"] >= 2
    assert all(c["limit"] == 500 for c in src.calls)


def test_status_change_while_paging_keeps_the_latest_state():
    first = item(7, "2026-11-01T01:00:00Z", "MovingToOzon")
    later = item(7, "2026-11-01T05:00:00Z", "ReturnedToOzon")
    pages = iter([{"returns": [first], "has_next": True}, {"returns": [later], "has_next": False}])
    rows, detail = F.returns_window(lambda p, b: next(pages), A, MID, "rt", "ts")
    assert [(r["return_id"], r["visual_status_sys_name"]) for r in rows] == [(7, "ReturnedToOzon")]
    assert detail["superseded_in_window"] == 1


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r["visual"].update(change_moment=iso(MID * 1000 + 1)), "RETURNS_WINDOW_OUTSIDE_REQUESTED_INTERVAL"),
    (lambda r: r["visual"].update(change_moment="2026-11-01T01:00:00"), "RETURNS_WINDOW_MISSING_TIMEZONE"),
    (lambda r: r.update(id="7"), "record shape"),
    (lambda r: r.pop("product"), "record shape")])
def test_out_of_contract_fails_closed(mutate, match):
    r = item(7, "2026-11-01T01:00:00Z")
    mutate(r)
    with pytest.raises(B.EvidenceError, match=match):
        F.returns_window(lambda p, b: {"returns": [r], "has_next": False}, A, MID, "rt", "ts")


@pytest.mark.parametrize("page", [{"returns": [], "has_next": True}, {"returns": None, "has_next": False},
                                  {"returns": [], "has_next": "false"}])
def test_malformed_or_nonterminal_pages_fail_closed(page):
    with pytest.raises(B.EvidenceError):
        F.returns_window(lambda p, b: page, A, MID, "rt", "ts")


def test_repeated_cursor_fails_closed():
    r = item(7, "2026-11-01T01:00:00Z")
    with pytest.raises(B.EvidenceError, match="repeated cursor"):
        F.returns_window(lambda p, b: {"returns": [r], "has_next": True}, A, MID, "rt", "ts")


def test_money_is_never_relabelled_as_rub(entities):
    r = item(7, "2026-11-01T01:00:00Z", price={"currency_code": "CNY", "price": 100})
    r["product"]["commission"] = {"currency_code": "", "price": 0}
    row = entities._return_rows([r], "rt", "ts")[0]
    assert row["product_price_rub"] is None and row["commission_rub"] == 0
    assert row["sku"] == "1000001" and row["return_date"] == r["logistic"]["return_date"][:10]
