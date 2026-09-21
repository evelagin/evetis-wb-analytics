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
| [`REPOSITORY_DATA_POLICY.md`](REPOSITORY_DATA_POLICY.md) | Что кладём в Git, а что нет | человек |

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

## Прогнать ворота данных

```bash
python tools/run_data_checks.py --suite ozon_unit \
  --project project-fa311fc0-4d87-4781-986 \
  --token-command "gcloud auth print-access-token" \
  --output ~/checks/ozon_unit.json
```

Коды выхода: `0` PASS · `1` FAIL · `2` EMPTY (проверка ничего не доказала) · `3` ошибка.
Отчёт содержит значения данных, поэтому пишется **вне** репозитория.
Состав наборов и долг непереведённых проверок — [`../../quality/suites.json`](../../quality/suites.json).

## Границы

Этот каталог описывает платформу, а не бизнес-методологию. Контракты экономики —
`docs/FIN_CONTRACT_V2_2026-09-16.md` и документы этапов. Актуальное состояние работ —
`docs/CURRENT_PROJECT_STATE.md`. Правила разработки — `CLAUDE.md` в корне.
