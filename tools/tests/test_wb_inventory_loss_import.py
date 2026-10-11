"""wb_inventory_loss_import: правила импорта закрытых доказательств утраты (без сети, синтетические данные)."""
import copy
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_inventory_loss_import as w  # noqa: E402

M = {
    "kind": "wb_inventory_loss_evidence/v1",
    "incidents": [
        {"incident_id": "I-A", "report_warehouse": "Склад А", "wb_report_ref": "r1", "incident_date": "2026-07-18", "incident_evidence": "e"},
        {"incident_id": "I-B", "report_warehouse": "Склад Б", "wb_report_ref": "r2", "incident_date": "2026-08-16", "incident_evidence": "e"},
    ],
    "items": [{"incident_id": "I-A", "nm_id": 1, "lost_qty": 2}, {"incident_id": "I-B", "nm_id": 2, "lost_qty": 3}],
    "compensations": [{"wb_document": "100", "booking_date": "2026-07-22", "amount_rub": "50.00", "attribution_basis": "BOOKING_MONTH",
                       "attributed_incident_month": None, "evidence": "e"}],
    "claim": {"claim_id": "C1", "filed_date": "2026-09-01", "claim_documented_goods_cost_rub": "500.00",
              "compensation_before_claim_rub": "50.00", "claimed_goods_rub": "450.00", "claimed_other_rub": "10.00",
              "status": "FILED", "evidence": "e"},
}
LIVE = {"ref": {1: ["SKU1"], 2: ["SKU2"]}, "cogs": {("I-A", 1): ["100.5"], ("I-B", 2): ["10"]},
        "stock": {("Склад А", 1): 2, ("Склад Б", 2): 3},
        "finance": [{"wb_document": "100", "booking_date": "2026-07-22", "amount_rub": "50.00"}]}
EXP = {"rows": 2, "units": 5, "writeoff": Decimal("231.00")}


def plan(m=M, live=LIVE, exp=EXP):
    class Src:
        def fetch(self, rows):
            return live
    return w.build_plan(copy.deepcopy(m), Src(), exp)


def test_happy_path_summary_and_stable_fingerprint():
    p = plan()
    assert p["core"]["summary"]["writeoff_rub"] == "231.00" and p["core"]["summary"]["units"] == 5
    assert p["core"]["summary"]["writeoff_by_month"] == {"2026-07": "201.00", "2026-08": "30.00"}
    assert plan()["fingerprint"] == p["fingerprint"]
    assert "Склад" not in str(p["core"]) and "I-A" not in str(p["core"])      # в выводе плана нет поштучных данных


@pytest.mark.parametrize("mutate, msg", [
    (lambda m, l, e: e.update(rows=3), "позиций"),
    (lambda m, l, e: e.update(units=6), "единиц"),
    (lambda m, l, e: e.update(writeoff=Decimal("231.01")), "каноническое списание"),
    (lambda m, l, e: m["items"].append({"incident_id": "I-A", "nm_id": 1, "lost_qty": 1}), "дубли"),
    (lambda m, l, e: l["ref"].update({1: ["S1", "S2"]}), "ровно одного SKU"),
    (lambda m, l, e: l["cogs"].update({("I-A", 1): ["1", "2"]}), "интервала себестоимости"),
    (lambda m, l, e: l["stock"].update({("Склад А", 1): 1}), "остатку WB"),
    (lambda m, l, e: l["stock"].update({("Склад А", 9): 4}), "лишних позиций"),
    (lambda m, l, e: l["finance"].append({"wb_document": "101", "booking_date": "2026-08-01", "amount_rub": "5.00"}), "выплатам финотчёта"),
    (lambda m, l, e: m["compensations"][0].update(amount_rub="49.99"), "претензия"),
    (lambda m, l, e: m["compensations"][0].update(attribution_basis="DOCUMENT_LINKED"), "только при DOCUMENT_LINKED"),
    (lambda m, l, e: m["compensations"][0].update(attribution_basis="DOCUMENT_LINKED", attributed_incident_month="2026-09-01"), "нет события"),
    (lambda m, l, e: m["claim"].update(claimed_goods_rub="460.00"), "заявлено за товар"),
    (lambda m, l, e: m["items"].append({"incident_id": "I-X", "nm_id": 3, "lost_qty": 1}), "неизвестное событие"),
])
def test_refusals(mutate, msg):
    m, l, e = copy.deepcopy(M), copy.deepcopy(LIVE), dict(EXP)
    mutate(m, l, e)
    with pytest.raises(w.PlanRefused, match=msg):
        plan(m, l, e)


def test_document_linked_compensation_needs_incident_month():
    m = copy.deepcopy(M)
    m["compensations"][0].update(attribution_basis="DOCUMENT_LINKED", attributed_incident_month="2026-07-01")
    assert plan(m)["rows"]["compensations"][0]["attributed_incident_month"] == "2026-07-01"


def test_transaction_is_guarded_and_parameterized():
    sql = w.transaction_sql("run1", "a" * 64, 131, 2)
    assert sql.startswith("BEGIN TRANSACTION;") and sql.rstrip().endswith("COMMIT TRANSACTION;")
    assert "AS 'WB_LOSS_IMPORT: таблицы доказательств не пусты'" in sql
    assert "= 131 AS 'WB_LOSS_IMPORT: позиции'" in sql and "= 2 AS 'WB_LOSS_IMPORT: компенсации'" in sql
    assert "@items" in sql and "@compensations" in sql and "@claim" in sql      # данные — параметрами, не текстом SQL
    rb = w.rollback_sql("run1")
    assert rb.count("DELETE FROM") == 3 and all("import_run_id = 'run1'" in x for x in rb.splitlines() if x.startswith("DELETE"))


def test_evidence_and_manifest_must_live_outside_the_repo(tmp_path):
    inside = w.ROOT / "evidence.json"
    with pytest.raises(w.PlanRefused, match="внутри репозитория"):
        w.load_manifest(inside)
    with pytest.raises(SystemExit, match="внутри репозитория"):
        w.outside_repo(str(w.ROOT / "x"), "--manifest-dir")
    assert w.outside_repo(str(tmp_path), "--manifest-dir") == tmp_path.resolve()


def test_verify_tables_detects_any_difference():
    p = plan()
    rows = p["rows"]

    class Src:
        def __init__(self, t):
            self.t = t

        def table_rows(self):
            return self.t
    good = {"items": [{**x, "_run": "r"} for x in rows["items"]], "compensations": [{**c, "_run": "r"} for c in rows["compensations"]],
            "claims": [{**rows["claim"], "_run": "r"}]}
    assert w.verify_tables(Src(good), rows, "r")["ok"]
    bad = copy.deepcopy(good)
    bad["items"][0]["lost_qty"] += 1
    assert not w.verify_tables(Src(bad), rows, "r")["ok"]
    assert not w.verify_tables(Src(good), rows, "other")["ok"]


def test_wrong_project_refused():
    with pytest.raises(SystemExit, match="не совпадает"):
        w.main(["--project", "other"])
