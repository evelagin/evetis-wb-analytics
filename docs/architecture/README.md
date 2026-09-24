# docs/architecture — карта платформы

Каталог отвечает на четыре вопроса: **как устроено сейчас**, **что сломано**, **куда идём**,
**как понять, что задача закончена**. Его читают человек и агент; ничего, что нельзя проверить
командой, сюда не попадает.

## Состав

| Файл | Отвечает на вопрос | Кто пишет |
|---|---|---|
| [`CURRENT_ARCHITECTURE.md`](CURRENT_ARCHITECTURE.md) | Как система устроена сейчас | человек |
| [`SYSTEM_INVENTORY.md`](SYSTEM_INVENTORY.md) | Что именно существует: объекты, расписания, Jobs | **генерируется** |
| [`DATA_LINEAGE.md`](DATA_LINEAGE.md) | Что от чего зависит и что сломается | **генерируется** |
| [`system_inventory.json`](system_inventory.json) | То же, машиночитаемо | **генерируется** |
| [`TECH_DEBT.md`](TECH_DEBT.md) | Что сломано, с доказательством и приоритетом | человек |
| [`FAILURE_MODES.md`](FAILURE_MODES.md) | Как система отказывает и что теряется навсегда | человек |
| [`TARGET_ARCHITECTURE.md`](TARGET_ARCHITECTURE.md) | Куда идём и чего сознательно не делаем | человек |
| [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) | В каком порядке, с проверкой и откатом | человек |
| [`DEFINITION_OF_DONE.md`](DEFINITION_OF_DONE.md) | Когда задача действительно закончена | человек |
| [`AI_ENGINEERING.md`](AI_ENGINEERING.md) | Как здесь работает агент и что ему запрещено | человек |
| [`TENANCY_DESIGN.md`](TENANCY_DESIGN.md) | Где зашит один продавец и как это снять | человек |
| [`TOOLING_DECISIONS.md`](TOOLING_DECISIONS.md) | Почему выбрано и почему отвергнуто (ADR) | человек |
| [`CANONICAL_COVERAGE.md`](CANONICAL_COVERAGE.md) | Тиры вью и измеренная граница канонического слоя | человек |
| [`HEALTH_COVERAGE_MIGRATION.md`](HEALTH_COVERAGE_MIGRATION.md) | Готовая к ACK миграция наблюдаемости Ozon / orders / sales | человек |
| [`AUTONOMY_READINESS.md`](AUTONOMY_READINESS.md) | Готов ли проект принять автономного агента, по направлениям | человек |
| [`REPOSITORY_DATA_POLICY.md`](REPOSITORY_DATA_POLICY.md) | Что кладём в Git, а что нет | человек |
| [`AE_V1_ARCHITECTURE.md`](AE_V1_ARCHITECTURE.md) | Autonomous Engineering v1: компоненты, машина состояний, модель доверия CI, V2 backlog | человек |
| [`AE_V1_SECURITY.md`](AE_V1_SECURITY.md) | Почему AE не может менять production, модель угроз, матрица прав, находка по WIF | человек |
| [`AE_V1_OPERATIONS.md`](AE_V1_OPERATIONS.md) | Как AE работает в обычный день, оповещения, стоимость, отмена | человек |
| [`AE_V1_RUNBOOK.md`](AE_V1_RUNBOOK.md) | Включение по шагам, канарейка UBR-011, остановка и откат | человек |
| [`AE_V1_ADR_EXECUTION_PLATFORM.md`](AE_V1_ADR_EXECUTION_PLATFORM.md) | ADR-07: GitHub Actions + Claude Code, решение по OpenHands, датированные источники | человек |
| [`AE_V1_ACCEPTANCE.md`](AE_V1_ACCEPTANCE.md) | Результаты приёмки A–F, что доказано и что нет | человек |

## Пересобрать снимок

Только чтение: BigQuery — исключительно `SELECT`, `gcloud` — только `list`.

```bash
python tools/architecture_baseline.py \
  --project project-fa311fc0-4d87-4781-986 \
  --token-command "gcloud auth print-access-token" --with-gcloud
python tools/render_architecture_docs.py
```

Первая команда обновляет `system_inventory.json`, вторая — два производных `.md`.
Правки прямо в сгенерированных файлах будут затёрты: меняй генератор или снимок.

## Проверить завершённость задачи

```bash
python tools/verify_task.py --project project-fa311fc0-4d87-4781-986 \
  --token-command "gcloud auth print-access-token" --task <имя>
```

Одна команда собирает шесть направлений доказательств и возвращает вердикт
`PASS` / `FAIL` / `BLOCKED` с кодом выхода. Состав обязательных контрактов выводится из
графа зависимостей (`tools/impact_analysis.py`), а не назначается вручную.

## Прогнать ворота данных

```bash
python tools/run_data_checks.py --suite ozon_unit \
  --project project-fa311fc0-4d87-4781-986 \
  --token-command "gcloud auth print-access-token" \
  --output ~/checks/ozon_unit.json
```

Коды выхода: `0` PASS · `1` FAIL · `2` EMPTY (проверка ничего не доказала) · `3` ошибка.
Отчёт содержит значения данных, поэтому пишется **вне** репозитория.
Состав наборов и долг неподключённых артефактов — [`../../quality/suites.json`](../../quality/suites.json);
поадресный реестр проверок и граф доказательств — [`../../quality/contract_registry.json`](../../quality/contract_registry.json);
нерешённые бизнес-правила — [`../../quality/unresolved_business_rules.json`](../../quality/unresolved_business_rules.json).

## Узнать, что требует изменение

```bash
python tools/impact_analysis.py --git-diff origin/main   # затронутые объекты, потребители, контракты, риск-тир
python tools/impact_analysis.py --tiers                  # классификация всех вью
```

## Границы

Этот каталог описывает платформу, а не бизнес-методологию. Контракты экономики —
`docs/FIN_CONTRACT_V2_2026-09-16.md` и документы этапов. Актуальное состояние работ —
`docs/CURRENT_PROJECT_STATE.md`. Правила разработки — `CLAUDE.md` в корне.
