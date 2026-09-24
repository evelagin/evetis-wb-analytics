# quality/autonomy — контракты Autonomous Engineering v1

| Файл | Что это |
|---|---|
| `policy.json` | единственный источник бюджетов, запрещённых путей, признаков ослабления ворот, правил ACK плана и инструментов агента; `production_mutation_authority` обязан быть `NONE` |
| `incident.schema.json` | конверт инцидента; создаёт только наблюдатель; невалидный не диспатчится |
| `objective.schema.json` | цель инженера — по инциденту или от владельца; `execute: false` запрещает запуск |
| `engineer_report.schema.json` | структурированный вывод инженера — заявление, а не доказательство |
| `review_verdict.schema.json` | вердикт независимого ревьюера: PASS / CHANGES_REQUIRED / BLOCKED / HUMAN_DECISION_REQUIRED |
| `run_state.schema.json` | долговременная запись прогона; единственное состояние системы |
| `examples/incident.synthetic.json`, `examples/objective.synthetic.json` | порождены самим наблюдателем по синтетическим наблюдениям |
| `examples/objective.ubr011.canary.json` | фикстура первой канарейки; решения не содержит; не запущена |

Валидация: `python -m tools.autonomy.cli validate --kind objective FILE`. Валидатор
(`tools/autonomy/schema.py`) — строгое подмножество JSON Schema: неизвестное ключевое слово
схемы — ошибка, а не пропуск.
