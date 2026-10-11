"""Правка начала состава двух наборов (cogs_bundle_start_fix): логика плана и текст транзакции (офлайн, без сети)."""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cogs_bundle_start_fix as fix  # noqa: E402
from lib.bq_readonly import assert_read_only, strip_sql_comments  # noqa: E402

P = fix.PROJECT
TON, SER = "EVT-SET-TON-CREAM-ACNE", "EVT-SET-ACNE-TONIC-SERUM"


def _row(bcid, bundle, comp, frm, notes=None, src="BUNDLES sheet"):
    return {"bundle_component_id": bcid, "bundle_internal_sku": bundle, "component_internal_sku": comp,
            "component_qty": 1, "effective_from": frm, "effective_to": None,
            "effective_from_is_reconstructed": False, "effective_from_window_from": None,
            "effective_from_window_to": None, "effective_from_transition_id": None,
            "effective_to_is_reconstructed": False, "effective_to_window_from": None, "effective_to_window_to": None,
            "effective_to_transition_id": None, "assembly_model": "FF_ASSEMBLED", "source_ref": src, "notes": notes}


ROWS = [
    _row("BC-28", TON, "EVT-FT-ACNE-150", "2026-05-03"),
    _row("BC-29", TON, "EVT-FC-ACNE-50", "2026-05-03"),
    _row("BC-30", SER, "EVT-FT-ACNE-150", "2026-07-09", notes="DQ", src="SKU_MASTER.bundle_components_source"),
    _row("BC-31", SER, "EVT-FS-ACNE-30", "2026-07-09", src="SKU_MASTER.bundle_components_source"),
]
HISTORY = [
    {"cogs_history_id": "COGS-014", "internal_sku": "EVT-FC-ACNE-50", "effective_from": "2025-09-16",
     "effective_to": None, "product_cogs_rub": "131.66"},
    {"cogs_history_id": "COGS-011", "internal_sku": "EVT-FS-ACNE-30", "effective_from": "2025-03-05",
     "effective_to": "2026-07-31", "product_cogs_rub": "168"},
    {"cogs_history_id": "COGS-012", "internal_sku": "EVT-FS-ACNE-30", "effective_from": "2026-08-01",
     "effective_to": None, "product_cogs_rub": "135.78"},
    {"cogs_history_id": "COGS-017", "internal_sku": "EVT-FT-ACNE-150", "effective_from": "2026-01-10",
     "effective_to": None, "product_cogs_rub": "182.785"},
]
OTHER_BUNDLE = {"internal_sku": "EVT-SET-OTHER", "effective_from": "2026-01-01", "effective_to": None,
                "product_cogs_rub": "100.5", "component_count": "2"}
DERIVED_BEFORE = [
    {"internal_sku": TON, "effective_from": "2026-05-03", "effective_to": None, "product_cogs_rub": "314.445",
     "component_count": "2"},
    {"internal_sku": SER, "effective_from": "2026-07-09", "effective_to": "2026-07-31", "product_cogs_rub": "350.785",
     "component_count": "2"},
    {"internal_sku": SER, "effective_from": "2026-08-01", "effective_to": None, "product_cogs_rub": "318.565",
     "component_count": "2"},
    OTHER_BUNDLE,
]
BODY = (f"WITH defs AS (\n  SELECT bundle_internal_sku, effective_from AS def_from\n"
        f"  FROM `{P}.evetis_ref.REF_BUNDLE_COMPONENTS`\n)\nSELECT * FROM defs")
SALES = [
    {"internal_sku": TON, "nm_id": "952068582", "order_date_msk": "2026-05-01", "sold_qty": "1"},
    {"internal_sku": SER, "nm_id": "1083392113", "order_date_msk": "2026-07-01", "sold_qty": "1"},
]


def _after(rows):
    out = []
    for r in copy.deepcopy(rows):
        if r["internal_sku"] == TON and r["effective_from"] == "2026-05-03":
            r["effective_from"] = "2026-05-01"
        if r["internal_sku"] == SER and r["effective_from"] == "2026-07-09":
            r["effective_from"] = "2026-07-01"
        out.append(r)
    return out


class Fake:
    def __init__(self, **over):
        self.rows = over.get("rows", copy.deepcopy(ROWS))
        self.history = over.get("history", copy.deepcopy(HISTORY))
        self.before = over.get("before", copy.deepcopy(DERIVED_BEFORE))
        self.after = over.get("after", _after(DERIVED_BEFORE))
        self.sales = over.get("sales", copy.deepcopy(SALES))
        self.substituted = None

    def bundle_rows(self):
        return copy.deepcopy(self.rows)

    def cogs_history(self):
        return copy.deepcopy(self.history)

    def derived_view_body(self):
        return BODY

    def derived_rows(self, body):
        if body is None:
            return copy.deepcopy(self.before)
        self.substituted = body
        return copy.deepcopy(self.after)

    def effective_bundle_rows(self):
        return [{k: r[k] for k in ("internal_sku", "effective_from", "effective_to", "product_cogs_rub")}
                for r in self.before if r["internal_sku"] in (TON, SER)]

    def affected_sales(self):
        return copy.deepcopy(self.sales)


# ── план: успешный путь ──

def test_plan_passes_on_live_shape():
    src = Fake()
    res = fix.build_plan(src)
    core = res["plan"]
    assert [r["bundle_component_id"] for r in core["before_image"]] == ["BC-28", "BC-29", "BC-30", "BC-31"]
    assert core["before_image"] == ROWS                               # полная строка для отката
    d = core["derived"]["bundles"]
    assert [(r["effective_from"], r["product_cogs_rub"]) for r in d[TON]["after"]] == [("2026-05-01", "314.445")]
    assert [(r["effective_from"], r["product_cogs_rub"]) for r in d[SER]["after"]] == [
        ("2026-07-01", "350.785"), ("2026-08-01", "318.565")]
    assert core["affected_sales"][TON]["units"] == "1" and core["affected_sales"][SER]["units"] == "1"
    assert "DATE '2026-05-01'" in src.substituted and "ELSE effective_from END AS effective_from" in src.substituted


def test_fingerprint_is_stable_and_sensitive():
    a, b = fix.build_plan(Fake()), fix.build_plan(Fake())
    assert a["fingerprint"] == b["fingerprint"] == fix.fingerprint(a["plan"])
    assert re.fullmatch(r"[0-9a-f]{64}", a["fingerprint"])
    # порядок ключей и строк на входе не влияет
    rows = [dict(reversed(list(r.items()))) for r in reversed(ROWS)]
    assert fix.build_plan(Fake(rows=rows))["fingerprint"] == a["fingerprint"]
    # любое изменение данных меняет отпечаток
    rows = copy.deepcopy(ROWS)
    rows[0]["notes"] = "x"
    assert fix.build_plan(Fake(rows=rows))["fingerprint"] != a["fingerprint"]


# ── план: отказы ──

@pytest.mark.parametrize("mutate", [
    lambda r: r[0].update(effective_from="2026-05-01"),         # уже исправлено / другое начало
    lambda r: r[1].update(component_internal_sku="EVT-X"),     # другой компонент
    lambda r: r[2].update(bundle_internal_sku=TON),            # другой набор
    lambda r: r[3].update(component_qty=2),                    # другое количество
    lambda r: r[3].update(effective_to="2026-12-31"),          # интервал закрыт
    lambda r: r.pop(0),                                        # строки нет
    lambda r: r.append(dict(r[0])),                            # дубль id
])
def test_identity_mismatch_refused(mutate):
    rows = copy.deepcopy(ROWS)
    mutate(rows)
    with pytest.raises(fix.PlanRefused):
        fix.build_plan(Fake(rows=rows))


@pytest.mark.parametrize("other", [
    _row("BC-90", TON, "EVT-OLD", "2026-01-01") | {"effective_to": "2026-05-02"},   # кончается в [новое, старое)
    _row("BC-91", SER, "EVT-NEW", "2026-07-05"),                                    # третий компонент внутри
    _row("BC-92", SER, "EVT-OLD", "2026-01-01") | {"effective_to": "2026-07-01"},   # ровно на новом начале
])
def test_overlap_refused(other):
    with pytest.raises(fix.PlanRefused, match="пересекает новое начало"):
        fix.build_plan(Fake(rows=copy.deepcopy(ROWS) + [other]))


def test_non_overlapping_other_row_allowed():
    other = _row("BC-93", TON, "EVT-OLD", "2025-01-01") | {"effective_to": "2026-04-30"}
    assert fix.build_plan(Fake(rows=copy.deepcopy(ROWS) + [other]))["plan"]["other_bundle_rows"] == [other]


@pytest.mark.parametrize("units", [[], [("2026-05-01", "1"), ("2026-05-02", "1")], [("2026-05-02", "2")],
                                   [("2026-05-01", "0")]])
def test_affected_sales_not_one_refused(units):
    sales = [SALES[1]] + [{"internal_sku": TON, "nm_id": "952068582", "order_date_msk": d, "sold_qty": q}
                          for d, q in units]
    with pytest.raises(fix.PlanRefused, match="STOP"):
        fix.build_plan(Fake(sales=sales))


def test_affected_sales_foreign_nm_refused():
    sales = copy.deepcopy(SALES)
    sales[0]["nm_id"] = "1"
    with pytest.raises(fix.PlanRefused, match="чужим nm"):
        fix.build_plan(Fake(sales=sales))


def test_component_cost_change_inside_window_refused():
    history = [h for h in HISTORY if h["cogs_history_id"] != "COGS-011"] + [
        {"cogs_history_id": "COGS-A", "internal_sku": "EVT-FS-ACNE-30", "effective_from": "2025-03-05",
         "effective_to": "2026-07-04", "product_cogs_rub": "150"},
        {"cogs_history_id": "COGS-B", "internal_sku": "EVT-FS-ACNE-30", "effective_from": "2026-07-05",
         "effective_to": "2026-07-31", "product_cogs_rub": "168"},
    ]
    with pytest.raises(fix.PlanRefused, match="BC-31"):
        fix.build_plan(Fake(history=history))


def test_component_cost_missing_at_new_start_refused():
    history = copy.deepcopy(HISTORY)
    history[3]["effective_from"] = "2026-05-02"                # FT-ACNE-150 начинается внутри окна
    with pytest.raises(fix.PlanRefused, match="не покрывает"):
        fix.build_plan(Fake(history=history))


def test_derived_amount_change_refused():
    after = _after(DERIVED_BEFORE)
    after[0]["product_cogs_rub"] = "314.446"
    with pytest.raises(fix.PlanRefused, match="after-image"):
        fix.build_plan(Fake(after=after))


def test_derived_other_bundle_change_refused():
    after = _after(DERIVED_BEFORE)
    after[3]["product_cogs_rub"] = "1"
    with pytest.raises(fix.PlanRefused, match="других наборов"):
        fix.build_plan(Fake(after=after))


def test_new_start_already_covered_refused():
    before = copy.deepcopy(DERIVED_BEFORE)
    before[0]["effective_from"] = "2026-05-01"
    with pytest.raises(fix.PlanRefused):
        fix.build_plan(Fake(before=before, after=copy.deepcopy(before)))


def test_substitution_requires_single_reference():
    with pytest.raises(fix.PlanRefused):
        fix.substitute_body(BODY + f"\nUNION ALL SELECT * FROM `{P}.evetis_ref.REF_BUNDLE_COMPONENTS`")
    with pytest.raises(fix.PlanRefused):
        fix.substitute_body("SELECT 1")
    sub = fix.substitute_body(BODY)
    assert_read_only(f"SELECT * FROM (\n{sub}\n)")                # after-image — чистое чтение


# ── транзакция ──

def _stmts(sql):
    return [s.strip() for s in strip_sql_comments(sql).split(";") if s.strip()]


@pytest.mark.parametrize("direction", ["apply", "rollback"])
def test_transaction_exactly_four_guarded_updates(direction):
    sql = fix.transaction_sql(direction)
    bare = strip_sql_comments(sql)
    assert len(re.findall(r"\bUPDATE\b", bare)) == 4
    for word in ("INSERT", "DELETE", "MERGE", "TRUNCATE", "DROP", "CREATE", "ALTER", "GRANT", "EXECUTE", "CALL"):
        assert not re.search(rf"\b{word}\b", bare, re.I), word
    assert set(re.findall(r"`([^`]+)`", bare)) == {f"{P}.evetis_ref.REF_BUNDLE_COMPONENTS"}
    updates = [s for s in _stmts(sql) if s.startswith("UPDATE")]
    assert len(updates) == 4
    for c, u in zip(fix.CORRECTIONS, updates):
        src, dst = (c.old_from, c.new_from) if direction == "apply" else (c.new_from, c.old_from)
        assert re.search(r"\bSET effective_from = DATE '" + dst + r"'\s+WHERE\b", u)
        assert u.count("SET ") == 1                                # меняется только effective_from
        for guard in (f"bundle_component_id = '{c.bundle_component_id}'",
                      f"bundle_internal_sku = '{c.bundle_internal_sku}'",
                      f"component_internal_sku = '{c.component_internal_sku}'",
                      f"effective_from = DATE '{src}'", "effective_to IS NULL"):
            assert guard in u
    # проверка каждой строки сразу после своего UPDATE, итоговая — до COMMIT
    assert len(re.findall(r"SET n = @@row_count;\s+IF n IS NULL OR n != 1 THEN RAISE", bare)) == 4
    i_begin, i_check = bare.index("BEGIN TRANSACTION"), bare.index("IF changed != 4 THEN RAISE")
    i_commit, i_rb = bare.index("COMMIT TRANSACTION"), bare.index("ROLLBACK TRANSACTION")
    assert i_begin < bare.index("UPDATE") and bare.rindex("UPDATE") < i_check < i_commit < i_rb
    assert bare.count("COMMIT TRANSACTION") == 1 and bare.count("BEGIN TRANSACTION") == 1
    assert "EXCEPTION WHEN ERROR THEN" in bare[i_commit:i_rb]


def test_rollback_is_exact_inverse():
    a, r = fix.transaction_sql("apply"), fix.transaction_sql("rollback")
    for c in fix.CORRECTIONS:
        assert f"SET effective_from = DATE '{c.new_from}'" in a and f"effective_from = DATE '{c.old_from}'" in a
        assert f"SET effective_from = DATE '{c.old_from}'" in r and f"effective_from = DATE '{c.new_from}'" in r


def test_wrong_project_refused():
    with pytest.raises(fix.PlanRefused):
        fix.transaction_sql("apply", project="other-project")
    with pytest.raises(SystemExit, match="не совпадает"):
        fix.main(["--project", "other-project"])


def test_apply_requires_fingerprint_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(fix, "resolve_token", lambda *a: pytest.fail("сеть не должна вызываться"))
    with pytest.raises(SystemExit, match="plan-fingerprint"):
        fix.main(["--project", P, "--apply"])
    with pytest.raises(SystemExit, match="plan-fingerprint"):
        fix.main(["--project", P, "--apply", "--plan-fingerprint", "abc"])
    with pytest.raises(SystemExit, match="manifest-dir"):
        fix.main(["--project", P, "--apply", "--plan-fingerprint", "0" * 64])
    with pytest.raises(SystemExit, match="внутри репозитория"):
        fix.main(["--project", P, "--apply", "--plan-fingerprint", "0" * 64, "--manifest-dir", str(fix.ROOT / "x")])


def test_apply_refuses_dirty_tree(tmp_path, monkeypatch):
    calls = []

    def git(*args):
        calls.append(args)
        return " M tools/x.py" if args[0] == "status" else "abc"
    monkeypatch.setattr(fix, "git", git)
    monkeypatch.setattr(fix, "resolve_token", lambda *a: pytest.fail("сеть не должна вызываться"))
    with pytest.raises(SystemExit, match="не чистое"):
        fix.main(["--project", P, "--apply", "--plan-fingerprint", "0" * 64, "--manifest-dir", str(tmp_path / "m")])


def test_apply_refuses_head_not_origin_main(tmp_path, monkeypatch):
    heads = {"HEAD": "aaa", "origin/main": "bbb"}
    monkeypatch.setattr(fix, "git", lambda *a: "" if a[0] in ("fetch", "status") else heads[a[1]])
    monkeypatch.setattr(fix, "resolve_token", lambda *a: pytest.fail("сеть не должна вызываться"))
    with pytest.raises(SystemExit, match="origin/main"):
        fix.main(["--project", P, "--apply", "--plan-fingerprint", "0" * 64, "--manifest-dir", str(tmp_path / "m")])


def test_after_rows_and_diff():
    after = fix.after_rows(ROWS)
    assert [r["effective_from"] for r in after] == ["2026-05-01", "2026-05-01", "2026-07-01", "2026-07-01"]
    assert ROWS[0]["effective_from"] == "2026-05-03"               # before-image не мутирует
    assert fix.diff_rows(after, after) == []
    bad = copy.deepcopy(after)
    bad[2]["notes"] = "changed"
    assert fix.diff_rows(bad, after) == ["BC-30: расходятся ['notes']"]


def test_read_queries_are_read_only():
    class Rec:
        def __init__(self):
            self.sql = []

        def query(self, sql):
            assert_read_only(sql)
            self.sql.append(sql)
            if "INFORMATION_SCHEMA.VIEWS" in sql:
                return [{"view_definition": BODY}]
            return []
    rec = Rec()
    src = fix.LiveSource(rec)
    src.bundle_rows(), src.target_rows(), src.cogs_history(), src.derived_view_body()
    src.derived_rows(None), src.derived_rows(fix.substitute_body(BODY)), src.effective_bundle_rows()
    src.affected_sales()
    assert len(rec.sql) == 8
    sales = rec.sql[-1]
    assert "order_date_msk >= DATE '2026-05-01' AND c.order_date_msk < DATE '2026-05-03'" in sales
    assert "order_date_msk >= DATE '2026-07-01' AND c.order_date_msk < DATE '2026-07-09'" in sales
