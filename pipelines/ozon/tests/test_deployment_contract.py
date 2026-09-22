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
