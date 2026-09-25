"""Синтетические контракты для офлайн-доказательств Terraform арендатора (T3.2).

Не данные арендатора: фикстуры строятся ТЕМ ЖЕ путём, что и боевой контракт —
через valid_tenants()/load_tenant() и registry.terraform_inputs. Отличаются только
  * образом — заведомо синтетический digest (реальный выпуск — T3.2b);
  * client_002 — эфемерный дескриптор во временной копии tenants/, в Git как
    арендатор не добавляется.
Файлы infra/tenant/tests/fixtures/*.contract.json обязаны совпадать с выводом
этого модуля (tools/tests/test_tenancy_t32.py).
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path

from tools.tenancy import naming as N
from tools.tenancy import platform as PL
from tools.tenancy.registry import TENANTS_DIR, _terraform_contract, load_tenant
from tools.tenancy.validation import load_tenant_document

FIXTURE_IMAGE = f"{PL.RUNTIME_REGISTRY}/ozon-runtime@sha256:" + "f" * 64
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "infra" / "tenant" / "tests" / "fixtures"
SYNTHETIC_TENANTS = ("client_001", "client_002")


def _with_ephemeral_client_002(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    doc = copy.deepcopy(load_tenant_document(src / "client_001" / "tenant.json"))
    doc["tenant_id"] = "client_002"
    doc["display_name"] = "Синтетический арендатор 002 (эфемерная фикстура T3.2)"
    doc["data_boundary"]["gcp_project_id"] = N.derive_project_id("client_002")
    (dst / "client_002").mkdir()
    (dst / "client_002" / "tenant.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    return dst


def fixture_contract(tenant_id: str) -> dict:
    if tenant_id not in SYNTHETIC_TENANTS:
        raise ValueError(f"фикстуры только для синтетики {SYNTHETIC_TENANTS}")
    with tempfile.TemporaryDirectory() as tmp:
        root = _with_ephemeral_client_002(TENANTS_DIR, Path(tmp) / "tenants")
        # Только тесты: тот же строгий load_tenant, но во временной копии реестра.
        contract = _terraform_contract(load_tenant(tenant_id, root))
    contract["marketplaces"]["ozon"]["runtime_image"] = FIXTURE_IMAGE
    return contract


def fixture_text(tenant_id: str) -> str:
    return json.dumps(fixture_contract(tenant_id), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


EVETIS_IMAGE = ("europe-west1-docker.pkg.dev/project-fa311fc0-4d87-4781-986/cloud-run-source-deploy/"
                "ozon-runtime-ingest@sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b")


def _set(doc: dict, path: str, value) -> dict:
    d = copy.deepcopy(doc)
    node = d
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return d


def negative_cases() -> dict[str, dict]:
    """Одна мутация на случай: каждый обязан быть отвергнут проверками переменной."""
    base = fixture_contract("client_001")
    reg = PL.RUNTIME_REGISTRY
    return {
        "evetis_project": _set(base, "project_id", PL.EVETIS_PROJECT_ID),
        "arbitrary_project": _set(base, "project_id", "some-other-project-1"),
        "foreign_tenant_project": _set(base, "project_id", N.derive_project_id("client_002")),
        "foreign_state_prefix": _set(base, "state.prefix", "tenants/client_002"),
        "platform_state_prefix": _set(base, "state.prefix", "platform/client_001"),
        "evetis_state_bucket": _set(base, "state.bucket", f"evetis-wb-tfstate-{PL.EVETIS_PROJECT_NUMBER}"),
        "org_root_parent": _set(base, "parent_folder", f"organizations/{PL.ORGANIZATION_ID}"),
        "image_latest_tag": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime:latest"),
        "image_mutable_tag": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime:v1"),
        "image_missing": _set(base, "marketplaces.ozon.runtime_image", None),
        "image_empty": _set(base, "marketplaces.ozon.runtime_image", ""),
        "image_evetis_registry": _set(base, "marketplaces.ozon.runtime_image", EVETIS_IMAGE),
        "image_short_digest": _set(base, "marketplaces.ozon.runtime_image", f"{reg}/ozon-runtime@sha256:abc"),
        "scheduler_enabled": _set(base, "scheduler_state", "ENABLED"),
        "evetis_secret_name": _set(base, "marketplaces.ozon.secret_ids.seller_api_key", "EVETIS_OZON_API_KEY"),
        "evetis_ref_dataset": _set(base, "datasets.ref", "evetis_ref"),
        "unknown_contract_version": _set(base, "contract_version", 2),
    }


if __name__ == "__main__":     # регенерация: python -m tools.tenancy.synthetic
    for t in SYNTHETIC_TENANTS:
        (FIXTURE_DIR / f"{t}.contract.json").write_text(fixture_text(t), encoding="utf-8")
        print(f"записана фикстура {t}")
    neg = FIXTURE_DIR / "negative"
    neg.mkdir(exist_ok=True)
    for name, doc in negative_cases().items():
        (neg / f"{name}.contract.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"негативных фикстур: {len(negative_cases())}")
