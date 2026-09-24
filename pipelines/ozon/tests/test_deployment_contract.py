"""Контракт развёртывания Ozon ingestion. Stage A / A3.

Тест НЕ трогает Ozon API, BigQuery и GCP: он читает два файла репозитория и
проверяет, что они согласованы между собой.

Зачем. Наборы сущностей живут в двух местах — реестр `REGISTRY` в
`pipelines/ozon/runtime/entities.py` и переменная ENTITIES в
`infra/terraform/ozon_ingestion.tf`. Рассогласование не падает: джоб просто
молча не загрузит сущность, а данные окажутся неполными задним числом. Именно
такой класс дефекта тест и ловит.

Реестр разбирается через `ast`, а не импортом: `entities` тянет `common`, а тот
— облачные библиотеки, которых в CI-проверке быть не должно.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ENTITIES_PY = REPO / "pipelines" / "ozon" / "runtime" / "entities.py"
OZON_TF = REPO / "infra" / "terraform" / "ozon_ingestion.tf"

# Каденция расписания -> метка каденции, объявленная в REGISTRY.
JOB_CADENCE = {
    "ozon-runtime-fast": "twice_daily",
    "ozon-runtime-daily": "daily",
    "ozon-runtime-weekly": "weekly",
    # PR-PROMO-1: наблюдатель акций. Четыре слота в сутки — обоснование каденса
    # в docs/promotions/PROMOTION_DECISION_ENGINE_SPEC_2026-09-22.md §9.
    "ozon-runtime-promo": "four_times_daily",
}


def load_registry() -> dict[str, tuple[int, str]]:
    """{entity: (lookback_days, cadence)} из REGISTRY без импорта модуля."""
    tree = ast.parse(ENTITIES_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "REGISTRY" for t in node.targets):
            continue
        out: dict[str, tuple[int, str]] = {}
        for key, value in zip(node.value.keys, node.value.values):
            name = ast.literal_eval(key)
            _fn, lookback, cadence = value.elts
            out[name] = (ast.literal_eval(lookback), ast.literal_eval(cadence))
        return out
    raise AssertionError("REGISTRY не найден в entities.py")


def load_tf_jobs() -> dict[str, str]:
    """{job_name: entities_csv} из local.ozon_jobs."""
    tf = OZON_TF.read_text(encoding="utf-8")
    block = re.search(r"ozon_jobs\s*=\s*\{(.*?)\n  \}", tf, re.S)
    assert block, "local.ozon_jobs не найден в ozon_ingestion.tf"
    pairs = re.findall(
        r'"(ozon-runtime-[a-z]+)"\s*=\s*\{.*?entities\s*=\s*"([^"]*)"',
        block.group(1),
        re.S,
    )
    assert pairs, "не разобран ни один job в local.ozon_jobs"
    return dict(pairs)


def test_every_registry_entity_is_scheduled_exactly_once():
    registry = load_registry()
    scheduled: dict[str, list[str]] = {}
    for job, csv in load_tf_jobs().items():
        for entity in filter(None, csv.split(",")):
            scheduled.setdefault(entity, []).append(job)

    missing = sorted(set(registry) - set(scheduled))
    assert not missing, (
        f"сущности есть в REGISTRY, но ни одно расписание их не грузит: {missing}. "
        "Данные будут неполными и это не упадёт в рантайме."
    )

    duplicated = {e: j for e, j in scheduled.items() if len(j) > 1}
    assert not duplicated, f"сущность назначена нескольким джобам: {duplicated}"


def test_no_unknown_entity_in_terraform():
    registry = load_registry()
    for job, csv in load_tf_jobs().items():
        for entity in filter(None, csv.split(",")):
            assert entity in registry, (
                f"{job}: ENTITIES содержит '{entity}', которого нет в REGISTRY. "
                "Джоб упадёт или молча пропустит сущность."
            )


def test_declared_cadence_matches_the_job_it_runs_in():
    """Перенос сущности между каденциями меняет окно ретроспективы."""
    registry = load_registry()
    for job, csv in load_tf_jobs().items():
        expected = JOB_CADENCE[job]
        for entity in filter(None, csv.split(",")):
            _lookback, cadence = registry[entity]
            assert cadence == expected, (
                f"{entity} объявлена в REGISTRY как '{cadence}', "
                f"а запускается в {job} ('{expected}'). "
                "Сверить с docs/ozon/OZON_INCREMENTAL_CONTRACT_V1.md."
            )


def _ozon_common_env() -> str:
    tf = OZON_TF.read_text(encoding="utf-8")
    block = re.search(r"ozon_common_env\s*=\s*\{(.*?)\n  \}", tf, re.S)
    assert block, "local.ozon_common_env не найден в ozon_ingestion.tf"
    return block.group(1)


def test_every_ozon_job_sets_gcp_project_id_explicitly():
    """Tenancy T2: common.py больше не подставляет проект EVETIS по умолчанию.

    Значит, каждый job обязан задавать GCP_PROJECT_ID сам. Доказательство —
    три звена: проект есть в общем окружении, общее окружение подмешивается в
    env каждого job'а, и env не исключён из управления Terraform.
    Живая проверка 2026-09-24 (gcloud run jobs describe): у всех ozon-* задан.
    """
    assert re.search(r"GCP_PROJECT_ID\s*=\s*var\.project_id", _ozon_common_env())
    tf = OZON_TF.read_text(encoding="utf-8")
    assert re.search(r"for_each\s*=\s*merge\(local\.ozon_common_env,", tf), \
        "env job'ов обязан включать ozon_common_env"
    lifecycle = re.search(r'resource "google_cloud_run_v2_job" "ozon_runtime".*?'
                          r"ignore_changes\s*=\s*\[(.*?)\]", tf, re.S)
    assert lifecycle, "lifecycle.ignore_changes job'ов не найден"
    assert "env" not in lifecycle.group(1), \
        "env под ignore_changes: Terraform не доказывал бы, что проект задан в живом job'е"


def test_evetis_jobs_rely_on_legacy_secret_and_ref_defaults():
    """EVETIS не задаёт имён секретов и справочника: работают значения по умолчанию
    из common.py, равные прежним литералам. Появление этих переменных в
    Terraform EVETIS — это изменение production, которое требует отдельного решения."""
    tf = OZON_TF.read_text(encoding="utf-8")
    for var in ("OZON_SECRET_SELLER_CLIENT_ID", "OZON_SECRET_SELLER_API_KEY",
                "OZON_SECRET_PERF_CLIENT_ID", "OZON_SECRET_PERF_CLIENT_SECRET",
                "BQ_REF_DATASET", "STRICT_PAGE_CAPS"):
        assert var not in tf, f"{var} появилась в Terraform EVETIS"


def test_runtime_image_is_pinned_by_digest():
    """Тег mutable — не идентификатор production.

    Наблюдение Stage A: тег `latest` в Artifact Registry указывал на образ
    2026-09-03, а в production работал образ 2026-09-06. Развёртывание по тегу
    откатило бы Ozon ingestion на три дня назад без единого следа в Git.
    """
    tf = OZON_TF.read_text(encoding="utf-8")
    match = re.search(r'ozon_runtime_image\s*=\s*"([^"]+)"', tf)
    assert match, "ozon_runtime_image не найден"
    image = match.group(1)
    assert "@sha256:" in image, (
        f"образ должен быть закреплён digest'ом, а не тегом: {image}"
    )
    assert not re.search(r":(latest|dev|stable)(@|$)", image), (
        f"в идентификаторе образа mutable-тег: {image}"
    )
