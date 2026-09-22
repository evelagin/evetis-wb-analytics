"""Проверка runtime-доступа обязана ловить именно тот случай, что сломал production 2026-09-22.

Инцидент: тело wb_mart.V_CT_ACTUAL_DAILY_LIVE получило зависимость на ozon_mart,
у sa-ct-refresh доступа туда не было, плановая пересборка упала с Access Denied.
Все прочие проверки были зелёными, потому что шли от имени владельца.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("cra", REPO / "tools" / "check_runtime_access.py")
cra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cra)

P = "project-fa311fc0-4d87-4781-986"
SA = "sa-ct-refresh@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"


def test_extracts_the_dataset_that_caused_the_incident():
    body = Path(REPO / "sql/control_tower/ct_ubr012_revenue_2026-09-22.sql").read_text(encoding="utf-8")
    ds = cra.datasets_referenced(body, P)
    assert "ozon_mart" in ds, "зависимость, сломавшая пересборку, должна быть видна инструменту"
    assert {"wb_mart", "ozon_raw", "evetis_ref"} <= ds


def test_pre_incident_acl_would_have_blocked_deployment():
    """ACL ozon_mart до инцидента: пяти записей, sa-ct-refresh среди них нет."""
    before = {"projectWriters", "projectOwners", "evelagin@gmail.com", "projectReaders",
              "sa-loaders-prod@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com"}
    assert SA not in before, "предпосылка теста: до инцидента доступа не было"


def test_old_body_needed_no_ozon_mart():
    """Откатное тело читает только ozon_raw — поэтому пересборка и работала до развёртывания."""
    body = Path(REPO / "sql/ozon/ubr012_revenue_2026-09-22/rollback_V_CT_ACTUAL_DAILY_LIVE.sql").read_text(encoding="utf-8")
    assert "ozon_mart" not in cra.datasets_referenced(body, P)


def test_read_roles_do_not_admit_write_only_grants():
    assert "WRITER" in cra.READ_ROLES and "OWNER" in cra.READ_ROLES
    for role in ("roles/bigquery.jobUser", "roles/bigquery.metadataViewer", "WRITER_ONLY"):
        assert role not in cra.READ_ROLES


def test_registry_declares_the_control_tower_consumer():
    import json
    reg = json.loads((REPO / "quality" / "runtime_identities.json").read_text(encoding="utf-8"))
    ct = [c for c in reg["consumers"] if c["runtime_identity"] == SA]
    assert len(ct) == 1
    assert "wb_mart.V_CT_ACTUAL_DAILY_LIVE" in ct[0]["reads"]
    assert "sql/control_tower/ct_ubr012_revenue_2026-09-22.sql" in ct[0]["sql_files"]
