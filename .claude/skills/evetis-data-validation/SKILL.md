---
name: evetis-data-validation
description: Валидирует SQL, MART, KPI и аналитические выводы EVETIS перед merge/deploy/Looker: sources, grain, joins, completeness, reconciliation, partial periods, business definitions и downstream consistency. Использовать перед публикацией любой критичной витрины или вывода.
---

# EVETIS Data Validation

## Роль

Это обязательный QA-слой для аналитической системы. Не подтверждай корректность только потому, что код выполняется без ошибки.

## 1. Source and definition check

Проверь:
- выбран ли authoritative source для метрики;
- соответствует ли период бизнес-вопросу;
- совпадает ли definition метрики с `DATA_MODEL.md`, `PROJECT_RULES.md` и актуальной MART-логикой;
- не подменены ли sale/order/realization/payout/profit друг другом;
- не смешан ли provisional/final факт;
- не представлен ли оценочный показатель как точный.

## 2. Structural QA

Проверь:
- source grain;
- output grain;
- uniqueness ключа;
- NULL/empty key values;
- join cardinality;
- row explosion;
- unexpected row loss;
- referential integrity;
- partition/date completeness.

## 3. Calculation QA

Минимум:
- пересчитай ключевые суммы независимым запросом;
- parts-to-total там, где они должны складываться;
- denominator у rates/percentages;
- `SAFE_DIVIDE`/zero denominator;
- единицы измерения и знак расходов;
- cost effective dates;
- bundle cost logic;
- ads fact vs analytical attribution.

## 4. Temporal QA

- сравнивай периоды одинаковой длины;
- partial period помечай явно;
- проверяй timezone/cutoff;
- ищи gaps и late arrivals;
- для daily MART проверяй target_date и freshness upstream sources.

## 5. Regression QA

Для изменения production-модели:
- выбери исторический контрольный период;
- сравни old vs new row count;
- сравни distinct grain;
- сравни ключевые денежные суммы;
- объясни каждое расхождение;
- отдельно проверь период, ради которого делалось изменение.

## 6. Business sanity

Красные флаги:
- резкий скачок >50% без объяснения;
- невозможная отрицательная величина;
- 0%/100% там, где это неожиданно;
- одинаковые значения по сегментам, когда dimension должна влиять;
- прибыль близка к выплате WB без учёта расходов;
- расходы рекламы внезапно появились на SKU без нового доказанного mapping.

## Verdict

Верни один статус:
- `READY` — проверки пройдены;
- `READY_WITH_CAVEATS` — результат применим при перечисленных ограничениях;
- `BLOCKED` — есть ошибка/недоказанный контракт, merge/deploy/вывод блокируется.

Всегда перечисли:
1. что проверено;
2. контрольные цифры;
3. найденные проблемы по severity;
4. caveats;
5. точный следующий шаг.
