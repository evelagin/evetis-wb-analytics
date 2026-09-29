---
name: evetis-data-validation
description: Валидирует SQL, аналитические модели, KPI и выводы EVETIS: authoritative sources, grain, joins, completeness, reconciliation, temporal semantics, business definitions, regression и downstream consistency. Использовать при проверке изменений data contracts, критичных витрин, метрик и аналитических выводов.
---

# EVETIS Data Validation

## Роль

Проверяй не только выполнение кода, но и доказанность результата.

Validation не должна создавать параллельную систему QA поверх существующих machine gates проекта.

Сначала установи:
- что именно изменилось;
- какой contract затронут;
- какой risk нужно доказать;
- какие проверки уже зарегистрированы для этого объекта.

## 1. Source and definition

Проверь:

- выбран ли authoritative source;
- найден ли действующий domain/financial/data contract;
- соответствует ли период бизнес-вопросу;
- совпадает ли definition метрики с действующим контрактом и implementation;
- не подменены ли разные бизнес-события друг другом;
- не смешаны ли факты с разной recognition semantics;
- не представлен ли derived/modelled показатель как observed или authoritative fact.

Не считать документ authoritative только по имени или более новой дате.

Если Git, contract documentation и production расходятся, это unresolved state до установления действующего контракта.

## 2. Structural QA

В зависимости от риска проверь:

- source grain;
- output grain;
- uniqueness ключа;
- NULL/empty в ключах;
- join cardinality;
- row multiplication;
- unexpected row loss;
- referential integrity;
- date/partition completeness;
- schema или key drift.

Не исправляй проблему дополнительным `DISTINCT`, `ANY_VALUE` или агрегацией без доказательства её причины.

## 3. Calculation QA

Проверяй применимые к изменению свойства:

- reconciliation критичных денежных показателей;
- parts-to-total, когда это предусмотрено контрактом;
- denominator rates/percentages;
- zero/null denominator semantics;
- единицы измерения;
- знак доходов и расходов;
- effective dates;
- bundle/cost semantics;
- billing fact vs analytical attribution;
- observed fact vs modelled/proposed value.

Не создавай обязательную ручную сверку, если соответствующий контроль уже зарегистрирован в project suite.

## 4. Temporal QA

При наличии временной семантики проверь:

- event/business/load time;
- timezone и cutoff;
- complete vs partial period;
- gaps;
- late arrivals;
- upstream freshness;
- сопоставимость сравниваемых периодов.

Не сравнивай неполный текущий период с полным предыдущим как равнозначные периоды без явной оговорки.

## 5. Regression QA

Для изменения действующей модели установи expected impact.

При необходимости сравни old vs new по:

- row count;
- distinct grain;
- ключевым метрикам;
- historical/control period;
- периоду или кейсу, ради которого делалось изменение.

Не требуй нулевого расхождения, если изменение контракта предполагает другое ожидаемое значение. Каждое существенное расхождение должно быть объяснено контрактом или признано unresolved.

## 6. Business sanity

Business sanity используется как detector, а не как доказательство.

Исследуй, например:

- резкие необъяснённые изменения;
- значения вне допустимого business domain;
- неожиданные 0/NULL/extreme rates;
- одинаковые значения по dimensions, которые должны различаться;
- финансовый результат, противоречащий структуре расходов;
- attribution или mapping, появившийся без доказанного источника;
- состояние promotions/prices, несовместимое с наблюдаемым lifecycle.

Не используй универсальный процентный threshold без действующего контракта или исторического baseline.

## Machine validation

Для изменения файлов сначала используй существующий dependency/quality framework проекта.

Основные точки:

- `tools/impact_analysis.py` — affected objects, contracts, risk и обязательные suites;
- `tools/run_data_checks.py` — зарегистрированные production data checks;
- `tools/validate_current_sql.py` — контракт `sql/current`, когда применим;
- `tools/verify_current_sql_live.py` — read-only Git/production parity, когда применим;
- `tools/verify_task.py` — итоговый сбор доказательств Definition of Done.

Не запускай все gates механически. Применимость должна определяться impact analysis, registry и характером изменения.

Если штатный инструмент не может запуститься из-за отсутствующего project environment/dependency, это не PASS. Не устанавливай произвольные глобальные зависимости ради обхода такого состояния.

## Verdict

Не вводи собственный статус, конкурирующий с machine verdict проекта.

Если существует machine verdict, сообщай его без переименования и отдельно объясняй смысл результата.

В частности:

- `PASS` означает, что требуемые доказательства соответствующего gate получены;
- `FAIL` означает доказанное нарушение;
- `BLOCKED` означает отсутствие необходимого доказательства или невозможность завершить проверку;
- `ERROR` означает ошибку выполнения проверки.

`EMPTY`, `UNPROVEN` и аналогичные незавершённые состояния не трактовать как PASS.

Для исследовательского анализа без machine verdict явно разделяй:
- доказанные факты;
- найденные проблемы;
- unresolved assumptions;
- ограничения вывода.

## Output

Укажи:

1. что проверялось;
2. authoritative contract/source;
3. применённые проверки;
4. результаты и существенные reconciliation findings;
5. проблемы и unresolved state;
6. downstream impact;
7. machine verdict, если он существует;
8. следующий необходимый шаг.

Не утверждай готовность merge/deploy только на основании визуальной проверки, успешного SQL-run или business sanity.
