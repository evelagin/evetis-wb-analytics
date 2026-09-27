"""Tenancy T4-b — рендер пакета SQL арендатора: правила P1–P8, переносимость, детерминизм.

Офлайн. Пакеты-фикстуры собираются во временном каталоге; контракт — синтетический вывод
реестра (tools/tenancy/synthetic.py), тот же, что у Terraform.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools import validate_current_sql as VC  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import sql_package as SP  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402

GOOD_VIEW = """CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_TEST_POSTING`
OPTIONS(description = 'Тестовая строка отправления')
AS
SELECT p.posting_number, p.sku, p.quantity
FROM `__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO` p;
"""
GOOD_SHARE = """CREATE OR REPLACE VIEW `__tenant__.analytics_share.test_orders`
OPTIONS(description = 'Тестовый клиентский слой')
AS
SELECT n.posting_number, SUM(n.quantity) AS units
FROM `__tenant__.ozon_mart.NORM_TEST_POSTING` n
GROUP BY n.posting_number;
"""


def _pkg(tmp_path: Path, objects: dict[tuple[str, str], str], classes: dict | None = None) -> Path:
    root = tmp_path / "pkg"
    manifest = {"package": "test", "version": 1, "objects": []}
    for (ds, name), sql in objects.items():
        (root / ds).mkdir(parents=True, exist_ok=True)
        (root / ds / f"{name}.sql").write_text(sql, encoding="utf-8")
        manifest["objects"].append({"dataset": ds, "name": name,
                                    "semantic_class": (classes or {}).get(name, "MARKETPLACE_FACT")})
    root.mkdir(exist_ok=True)
    (root / SP.MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return root


def _findings(root, tid="client_001"):
    return SP.load_package(root, SY.fixture_contract(tid))[1]


GOOD = {("ozon_mart", "NORM_TEST_POSTING"): GOOD_VIEW, ("analytics_share", "test_orders"): GOOD_SHARE}


# ═══════════════════════════════════════ положительный путь и переносимость
def test_valid_package_passes_and_orders_dependencies(tmp_path):
    order, findings = SP.load_package(_pkg(tmp_path, GOOD), SY.fixture_contract("client_001"))
    assert findings == []
    assert [o.name for o in order] == ["NORM_TEST_POSTING", "test_orders"]


@pytest.mark.parametrize("tid", ["client_001", "client_002"])
def test_same_package_renders_for_any_tenant_without_code_changes(tmp_path, monkeypatch, tid):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    root = _pkg(tmp_path, GOOD)
    out = tmp_path / f"out_{tid}"
    m = SP.render(tid, out, root)
    c = SY.fixture_contract(tid)
    assert m["tenant_id"] == tid and m["project_id"] == c["project_id"]
    text = (out / "ozon_mart" / "NORM_TEST_POSTING.sql").read_text(encoding="utf-8")
    assert f"`{c['project_id']}.ozon_raw.RAW_OZON_POSTINGS_FBO`" in text
    assert SP.TEMPLATE_PROJECT not in text
    for mk in PL.EVETIS_FORBIDDEN_MARKERS:
        assert mk not in text


def test_render_is_deterministic_and_differs_between_tenants_only_in_project(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    root = _pkg(tmp_path, GOOD)
    a1 = SP.render("client_001", tmp_path / "a1", root)
    a2 = SP.render("client_001", tmp_path / "a2", root)
    b = SP.render("client_002", tmp_path / "b", root)
    assert a1 == a2
    assert a1["template_sha256"] == b["template_sha256"]
    ta = (tmp_path / "a1" / "ozon_mart" / "NORM_TEST_POSTING.sql").read_text(encoding="utf-8")
    tb = (tmp_path / "b" / "ozon_mart" / "NORM_TEST_POSTING.sql").read_text(encoding="utf-8")
    assert ta.replace("mpa-t-client-001", "X") == tb.replace("mpa-t-client-002", "X")


def test_render_refuses_an_invalid_package(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): GOOD_VIEW.replace("p.quantity", "p.qty")})
    with pytest.raises(SP.PackageError):
        SP.render("client_001", tmp_path / "out", root)


# ═══════════════════════════════════════ отрицательные контроли P1–P8
NEG = {
    "P1 DML instead of view": GOOD_VIEW.replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TABLE"),
    "P1 two statements": GOOD_VIEW + "\nSELECT 1;",
    "P1 select star": GOOD_VIEW.replace("p.posting_number, p.sku, p.quantity", "*"),
    "P1 no description": GOOD_VIEW.replace("OPTIONS(description = 'Тестовая строка отправления')\n", ""),
    "P1 scripting": GOOD_VIEW + "\nEXECUTE IMMEDIATE 'SELECT 1';",
    "P2 target name differs from file": GOOD_VIEW.replace("ozon_mart.NORM_TEST_POSTING`", "ozon_mart.OTHER`", 1),
    "P3 foreign project": GOOD_VIEW.replace("`__tenant__.ozon_raw.", "`mpa-t-client-002.ozon_raw."),
    "P3 unqualified reference": GOOD_VIEW.replace("`__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO`", "RAW_OZON_POSTINGS_FBO"),
    "P3 split template reference": GOOD_VIEW.replace("`__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO`",
                                                     "`__tenant__`.`ozon_raw`.`RAW_OZON_POSTINGS_FBO`"),
    "P5 unknown table": GOOD_VIEW.replace("RAW_OZON_POSTINGS_FBO", "RAW_OZON_POSTINGS_FBS"),
    "P5 unknown column": GOOD_VIEW.replace("p.quantity", "p.qty"),
    "P5 unknown dataset key": GOOD_VIEW.replace("__tenant__.ozon_raw.", "__tenant__.wb_raw."),
    "P8 EVETIS project": GOOD_VIEW.replace("SELECT p.posting_number",
                                           "SELECT 'project-fa311fc0-4d87-4781-986' AS x, p.posting_number"),
    "P8 evetis word": GOOD_VIEW.replace("Тестовая строка", "строка как у EVETIS"),
    "P8 evetis_ref dataset": GOOD_VIEW.replace("__tenant__.ozon_raw.", "__tenant__.evetis_ref."),
    "P8 tenant id literal": GOOD_VIEW.replace("FROM", "WHERE 'client_001' = 'x' FROM"),
    "P8 tenant project literal": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT 'mpa-t-client-001' AS x, p.posting_number"),
    "P8 EVT sku": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT 'EVT-HC-HAND-300' AS x, p.posting_number"),
    "P8 posting number literal": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT '12345678-0001-1' AS x, p.posting_number"),
    "P8 inn literal": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT '7700000000' AS x, p.posting_number"),
    "P8 business date literal": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT DATE '2026-08-28' AS x, p.posting_number"),
    "P8 cis buyout": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT 'CIS_BUYOUT' AS x, p.posting_number"),
    "P8 evetis calibration": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT 0.558 AS x, p.posting_number"),
    "P8 platform project": GOOD_VIEW.replace("SELECT p.posting_number", "SELECT 'mpa-platform' AS x, p.posting_number"),
}


@pytest.mark.parametrize("name", sorted(NEG))
def test_negative_control(tmp_path, name):
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): NEG[name]})
    assert _findings(root), name


def test_p4_customer_layer_cannot_read_raw(tmp_path):
    bad = GOOD_SHARE.replace("`__tenant__.ozon_mart.NORM_TEST_POSTING` n", "`__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO` n")
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): GOOD_VIEW, ("analytics_share", "test_orders"): bad})
    assert any("P4" in f for f in _findings(root))


def test_p4_mart_cannot_read_tenant_ops_or_share(tmp_path):
    bad = GOOD_VIEW.replace("`__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO` p",
                            "`__tenant__.tenant_ops.DATA_COVERAGE` p").replace(
        "p.posting_number, p.sku, p.quantity", "p.entity, p.status, p.rows_loaded")
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): bad})
    assert any("P4" in f for f in _findings(root))


def test_p6_cycle_is_rejected(tmp_path):
    a = """CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_A`
OPTIONS(description = 'a') AS SELECT b.x FROM `__tenant__.ozon_mart.NORM_B` b;"""
    b = """CREATE OR REPLACE VIEW `__tenant__.ozon_mart.NORM_B`
OPTIONS(description = 'b') AS SELECT a.x FROM `__tenant__.ozon_mart.NORM_A` a;"""
    root = _pkg(tmp_path, {("ozon_mart", "NORM_A"): a, ("ozon_mart", "NORM_B"): b})
    assert any("P6" in f for f in _findings(root))


@pytest.mark.parametrize("cls,ok", [("MARKETPLACE_FACT", True), ("GENERIC_DERIVED", True), ("TENANT_CONFIG", True),
                                    ("EVETIS_EMPIRICAL", False), ("", False)])
def test_p7_semantic_class(tmp_path, cls, ok):
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): GOOD_VIEW}, {"NORM_TEST_POSTING": cls})
    assert (not _findings(root)) is ok


def test_p2_undeclared_file_and_missing_file(tmp_path):
    root = _pkg(tmp_path, GOOD)
    (root / "ozon_mart" / "NORM_STRAY.sql").write_text(GOOD_VIEW.replace("NORM_TEST_POSTING`", "NORM_STRAY`", 1),
                                                       encoding="utf-8")
    assert any("не объявлен" in f for f in _findings(root))
    (root / "ozon_mart" / "NORM_STRAY.sql").unlink()
    (root / "ozon_mart" / "NORM_TEST_POSTING.sql").unlink()
    assert any("файла нет" in f for f in _findings(root))


def test_package_dataset_outside_package_layers_is_rejected(tmp_path):
    root = _pkg(tmp_path, {("ozon_raw", "RAW_HACK"): GOOD_VIEW.replace("ozon_mart.NORM_TEST_POSTING", "ozon_raw.RAW_HACK")})
    assert any("не входит в пакет" in f for f in _findings(root))


# ═══════════════════════════════════════ реальный пакет репозитория
def test_repository_package_is_valid():
    order, findings = SP.load_package(SP.PACKAGE_DIR, SY.fixture_contract("client_001"))
    assert findings == [], findings


def test_package_object_names_never_collide_with_evetis_canonical_objects():
    canonical = set()
    for manifest in (REPO / "sql" / "current").glob("*/MANIFEST.json"):
        canonical |= {o["object_name"].casefold() for o in json.loads(manifest.read_text(encoding="utf-8"))["objects"]}
    package = {p.stem.casefold() for p in SP.PACKAGE_DIR.glob("*/*.sql")}
    assert not package & canonical, package & canonical


def test_package_is_not_scanned_as_a_competing_canonical_definition():
    findings, _summary = VC.validate(REPO)
    assert not [f for f in findings if "sql/tenant/" in f.subject]


# ═══════════════════════════════════════ правила несут нагрузку
def test_banned_literal_rule_is_load_bearing(tmp_path, monkeypatch):
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): NEG["P8 evetis calibration"]})
    assert _findings(root)
    monkeypatch.setattr(SP, "BANNED", ())
    assert _findings(root) == []


def test_read_policy_rule_is_load_bearing(tmp_path, monkeypatch):
    bad = GOOD_SHARE.replace("`__tenant__.ozon_mart.NORM_TEST_POSTING` n", "`__tenant__.ozon_raw.RAW_OZON_POSTINGS_FBO` n")
    root = _pkg(tmp_path, {("ozon_mart", "NORM_TEST_POSTING"): GOOD_VIEW, ("analytics_share", "test_orders"): bad})
    assert any("P4" in f for f in _findings(root))
    monkeypatch.setattr(SP, "READ_POLICY", {k: v | {"ozon_raw"} for k, v in SP.READ_POLICY.items()})
    assert not any("P4" in f for f in _findings(root))
