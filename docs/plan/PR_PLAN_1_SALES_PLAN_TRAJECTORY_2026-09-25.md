# PR-PLAN-1 — план продаж и помесячная траектория запаса

**Дата:** 2026-09-25 · **Статус:** реализовано Git-first; развёртывание — журнал в конце документа.
**Не входит:** выручка, вклад и экономика плана (это PLAN-2), решения о производстве и закупке,
распределение дефицитного компонента, распродажа крема для рук, сезонность.

## 1. Что это и чего здесь нет

Поверхность планирования по физическому SKU. Четыре смысла разведены в колонках и на экране:

| Смысл | Откуда | Что это |
| --- | --- | --- |
| **OBSERVED** | `V_SKU_SELL_THROUGH_CURRENT` (PR-PROMO-4) | запас, свежесть, наблюдаемый темп 30/90 дней |
| **REQUIRED** | `V_SKU_INVENTORY_TARGET_CURRENT` (PR-PROMO-4) | дата «срок − буфер C1» и требуемый темп к ней; это требование к запасу, а не прогноз |
| **PROPOSED** | версия `SYSTEM_PROPOSED` / `OWNER_AUTHORED` | план, предложенный владельцу; до утверждения — сценарий |
| **APPROVED** | `V_SALES_PLAN_APPROVED` | бизнес-план; появляется только после ACK владельца на точный хеш |

Control Tower (`V_CT_PLAN_ACTIVE`, потребность в поставке, сборка наборов, OVERSTOCK) и C1
(`evetis_ops`) **не переключены** и читают свой план как раньше. Новый контракт построен рядом.
Юнитка и Forward economics не затронуты. YouSend не автоматизирован; устаревший остаток ФФ не
подменяется данными `/api/product/stock`.

## 2. Хранилище (`evetis_ref`, только добавление строк)

`sql/plan/plan1_storage.sql`:

| Таблица | Назначение |
| --- | --- |
| `PLAN_VERSION` | шапка версии: `plan_kind` (`MODEL_SCENARIO` / `SYSTEM_PROPOSED` / `OWNER_AUTHORED`), метод, родитель, горизонт, базис данных (`basis_sales_as_of`, `basis_inventory_as_of`, свежесть), `line_count`, `bom_basis_rows`, `content_sha256` |
| `PLAN_LINE_MONTHLY` | строки: `plan_version × month × marketplace × internal_sku` → `planned_cards` (NUMERIC) |
| `PLAN_ASSUMPTION` | допущения версии с классом доказательности (`FACT`, `INFERENCE`, `ASSUMPTION`, `OWNER_DECISION`, `LEGACY_MODEL`) |
| `PLAN_BOM_BASIS` | снимок BOM на момент создания версии. Утверждённая история не меняется при правке `REF_BUNDLE_COMPONENTS` |
| `INBOUND_LOT_EVENT` | события партий поступления; действует последнее событие `inbound_id` |

Реестр утверждений один — существующий `REF_SALES_PLAN_APPROVAL` (PR-PROMO-4), расширен двумя
nullable-колонками: `content_sha256` и `effective_from_month`. Второго реестра нет.

**Legacy SET_2026-09-09** регистрируется шапкой `MODEL_SCENARIO`. Строки не копируются:
`V_PLAN_LINE_MONTHLY_ALL` читает их из `CT_SEASON_PLAN_MONTHLY` по `source_ref`, а хеш шапки
фиксирует их. Если строку CT изменят, версия станет `INTEGRITY_BROKEN`. `APPROVED` модельному
сценарию не записывается и не действует: процедура отказывает до записи, представление
игнорирует событие с причиной `MODEL_NOT_APPROVABLE`.

## 3. Жизненный цикл и хеш

`DRAFT → PROPOSED → APPROVED`, плюс `WITHDRAWN` (снято предложение) и `REVOKED` (снято
утверждение). `SUPERSEDED` выводится, а не записывается. Статус считается
`V_PLAN_VERSION_STATUS` при каждом чтении. Недействующие события видны в `ignored_events`
с причинами: `NO_SUCH_VERSION`, `MODEL_NOT_APPROVABLE`, `HASH_MISMATCH`, `EFFECTIVE_MONTH_INVALID`,
`NOT_PROPOSED_BEFORE`, `DUPLICATE_OR_WITHDRAWN`, `OUT_OF_ORDER`.

**Канон `content_sha256`.** Он одинаков в представлении, процедурах и Python-фикстурах; тест
`test_one_hash_canon_in_view_procedures_and_fixtures` это проверяет:

```
строки  = CONCAT(month, '|', marketplace, '|', internal_sku, '|', CAST(ROUND(planned_cards, 4) AS STRING)), сортировка, '\n'
BOM     = CONCAT(bundle_sku, '|', component_sku, '|', component_qty), сортировка, '\n'
sha256  = TO_HEX(SHA256(строки || '\n#BOM\n' || BOM))
legacy  : planned_cards = ROUND(CAST(SUM(target_cards) AS NUMERIC), 4) по месяцу × UPPER(площадка) × карточке
```

**Утверждение действует**, только если выполнены все условия:

1. Версия вида `SYSTEM_PROPOSED` или `OWNER_AUTHORED`.
2. Пересчёт хеша совпадает с хешем шапки, а хеш события — с хешем шапки.
3. Раньше было действующее `PROPOSED`.
4. Версия не снята (`WITHDRAWN`) и утверждается впервые.
5. `effective_from_month` — первое число месяца, не раньше месяца утверждения и внутри горизонта.

Задним числом утвердить нельзя.

**Окна действия.** Утверждённая версия действует с `effective_from_month` до
`effective_from_month` следующего по времени записи утверждения, до отзыва или до конца горизонта
(что раньше). Отзыв снимает версию с месяца, следующего за месяцем отзыва; прошедшие месяцы не
переписываются. Окна не пересекаются: на каждый месяц × площадку × карточку приходится не больше
одной строки (DQ P05).

**Путь утверждения.** Система создаёт версию `DRAFT`, затем выполняется
`sp_plan_submit` → `PROPOSED`. Владелец видит содержимое и хеш на экране и даёт ACK. После этого
вызывается `sp_plan_approve(версия, хеш, месяц начала, кто, основание)`. Автоматического
утверждения нет: расписаний, сервисных аккаунтов и вызовов из кода нет (тест
`test_nothing_approves_automatically`).

## 4. Процедуры (`sql/plan/plan1_procedures.sql`, только ручной CALL)

| Процедура | Что делает |
| --- | --- |
| `sp_plan_register_legacy_ct(ct_version, кто, основание)` | регистрирует `MODEL_SCENARIO`: шапка, снимок BOM, допущения модели L01–L08 (`LEGACY_MODEL`) |
| `sp_plan_propose_observed_run_rate(horizon_to, кто, OUT версия)` | метод `OBSERVED_RUN_RATE_30D_V1` → версия `DRAFT` |
| `sp_plan_submit(версия, хеш, кто, заметка)` | `DRAFT → PROPOSED` |
| `sp_plan_approve(версия, хеш, месяц, кто, основание)` | `PROPOSED → APPROVED`, только по ACK владельца |
| `sp_plan_close(версия, WITHDRAWN\|REVOKED, хеш, кто, основание)` | снятие предложения или утверждения |
| `sp_inbound_record(...)` | событие партии; перечисления и связность проверяются |

Каждая процедура работает в транзакции. После записи она сверяет результат с
`V_PLAN_VERSION_STATUS`: при расхождении статуса или хеша транзакция откатывается.

**Метод `OBSERVED_RUN_RATE_30D_V1`:**

- **Темп.** Для каждой карточки × площадки: заказанные карточки без отмен за 30 полных суток,
  оканчивающихся `sales_as_of` (`V_CT_ACTUAL_DAILY`), делённые на 30.
- **Горизонт.** С месяца дня `sales_as_of + 1` по `horizon_to`.
- **План месяца.** `ROUND(темп × все календарные дни месяца, 1)`; в последнем месяце — только
  дни до `horizon_to`. Текущий месяц планируется целиком; уже прошедшую часть вычитает MTD (§5).
- **Охват.** Текущие карточки `REF_SKU_CHANNEL_MAP` (WB, Ozon); при нулевом темпе строка равна 0.
- **Чего нет:** сезонности, множителя Ozon ×1,25, множителей наборов ×2,5/×1,1, программы HC-B,
  предполагаемых поставок. Это проверяют DQ P15 и допущения M01–M06.

## 5. Физическая потребность и траектория

`V_PLAN_PHYSICAL_MONTHLY`:

- **Разложение.** `физ(компонент, месяц) = Σ planned_cards × qty` по снимку BOM версии.
  Одиночная карточка расходует саму себя.
- **Текущий месяц.** По карточке × площадке `MAX(план − заказано MTD, 0)`. Прошлые месяцы дают 0,
  будущие — план целиком.
- **Разбивки.** Одиночные / в наборах, WB / Ozon, строка наборов.
- **Набор без снимка BOM** не раскладывается: он даёт исключение `BOM_BASIS_MISSING`.

`V_INBOUND_LOT_CURRENT` — в базовую траекторию попадает только:

- **`ELIGIBLE_CONFIRMED`:** состояние `ORDER_CONFIRMED` / `IN_PRODUCTION` / `PRODUCED` /
  `READY_FOR_SHIPMENT` / `IN_TRANSIT`, без блокера, `eta_status = CONFIRMED`, дата не раньше даты
  запаса. Партия входит в месяц своей даты.
- **`RECEIVED_NOT_IN_POSITION`:** приёмка после среза ФФ, на котором стоит позиция. Партия входит
  в текущий месяц. Приёмка не позже среза (`RECEIVED_IN_POSITION`) уже учтена в запасе и повторно
  не считается.

Остальные партии видны, но в траекторию не входят. Статусы проверяются в таком порядке:
`EXCLUDED_CANCELLED`, `EXCLUDED_HYPOTHETICAL`, `BLOCKED`, `ETA_UNKNOWN`, `ETA_NOT_CONFIRMED`,
`ETA_OVERDUE`. Дата модели Control Tower (`ct_model_inbound_eta`) показывается только для
сравнения.

`V_PLAN_TRAJECTORY_MONTHLY`:

```
closing(m) = opening(m) + eligible_inbound(m) − planned_physical(m),  opening(m+1) = closing(m)
opening(текущий месяц) = позиция запаса на дату среза;  shortfall = MAX(−closing, 0) — без обрезки
```

- **`APPROVED_PLAN`** — бизнес-траектория по действующей в месяце версии. Нет утверждения —
  статус `NO_APPROVED_PLAN`. Запас не FRESH — статус `INVENTORY_STALE`, числа остатков NULL.
- **`VERSION_SCENARIO`** — сценарий каждой версии. На несвежем запасе он считается, но помечен
  `SCENARIO_ON_STALE_INVENTORY` (`trajectory_meaning = SCENARIO_NOT_A_PLAN`).
- **Сравнение (не влияет на остатки):** наблюдаемый темп 30 дней × дни периода; требуемый темп к
  sell-by; остаток к sell-by — линейно внутри месяца, поступления месяца считаются пришедшими
  в его начале.

`V_PLANNING_EXCEPTIONS` — исключения с числом и происхождением, без решений:

- `BLOCKER`: `INVENTORY_STALE`, `NO_APPROVED_PLAN`, `PLAN_INTEGRITY`, `BOM_BASIS_MISSING`,
  `COMPONENT_SHORTFALL` (для утверждённого плана);
- `WARNING`: `COMPONENT_SHORTFALL` (для сценария), `EXPIRY_PRESSURE`, `INBOUND_BLOCKED`,
  `INBOUND_ETA_UNKNOWN`, `INBOUND_ETA_OVERDUE`;
- `INFO`: `INBOUND_HYPOTHETICAL`.

`V_PLANNING_SKU_OVERVIEW` — одна строка на SKU с четырьмя смыслами. `V_PLANNING_HEADER` — даты,
свежесть, действующий план, предложенная версия с хешем, модельный сценарий, счётчики исключений.

**Переключённые представления PR-PROMO-4** (схема колонок меняется, см. CHANGELOG):

- `V_SALES_PLAN_MONTHLY_CURRENT` показывает исполнение только утверждённого плана и читает
  `V_SALES_PLAN_APPROVED`.
- `V_SKU_INVENTORY_TRAJECTORY_MONTHLY_CURRENT` оставляет только основу
  `TARGET_REQUIRED_RUN_RATE`; траектория плана живёт в `V_PLAN_TRAJECTORY_MONTHLY`.

## 6. Проверки

| Что | Где | Результат до развёртывания |
| --- | --- | --- |
| Регрессия §17 на фикстурах (PX10 жизненный цикл, PX11 замена и отзыв, PX20 BOM/MTD/поступления/траектория/нехватка/срок, PX21 несвежий запас и календарь, PX22 legacy-адаптер) | `tools/plan1_render.py run fixtures` | 24 блока, 81 утверждение — PASS |
| Регрессия PR-PROMO-4 (FX42 переписан на контракт версий) | `tools/promo_inventory_render.py run fixtures` | 42 блока — PASS |
| Живой набор `sales_plan` P01–P18 | `sql/plan/plan1_validation.sql` | на симуляции записей процедур: PASS, кроме намеренно заложенного в симуляцию утверждения SET (P02/P16 его ловят) |
| Офлайн-контракт | `tools/tests/test_plan1.py`, `test_promo_inventory.py` | PASS |

## 7. Развёртывание и откат

- **Развёртывание:** `tools/plan1_deploy.py --apply` из чистого `origin/main`. Порядок: хранилище
  и ALTER → 11 представлений → 7 процедур. Данные не пишутся.
- **Откат:**
  1. Удалить дашборд Metabase «EVETIS PLANNING»: `tools/metabase_plan1_build.py --rollback`.
  2. Вернуть два представления PR-PROMO-4 к `15b1867`.
  3. Выполнить `sql/plan/plan1_rollback.sql`.

  Таблицы с решениями и фактами владельца откат не удаляет.

## 8. Ограничения

- Запас ФФ сейчас STALE (срез 09.09). Поэтому бизнес-траектория не считается, а сценарии помечены.
  Обновление среза ФФ — ручной шаг владельца, вне PLAN-1.
- MTD вычитается до `sales_as_of`. Заказы дня среза могут попасть и в позицию, и в остаток плана:
  это не больше суток, ошибка в консервативную сторону.
- Остаток к sell-by считается линейно внутри месяца.
- Темп наборов наблюдается по карточке набора. Физический темп компонента в
  `V_SKU_SELL_THROUGH_CURRENT` уже включает наборы, поэтому это сравнение, а не вход плана.
- Наблюдаемый темп на SKU с дефицитом (`STOCKOUT_CONSTRAINED`) занижает спрос. Метод об этом
  предупреждает (M06, качество окна видно рядом), но не корректирует.

## 9. Готовность к PLAN-2

- **Ключ для моста к экономике:** `V_SALES_PLAN_APPROVED` (месяц × площадка × карточка), тот же, что
  у PR-PROMO-3.
- **Физические единицы по BOM версии:** `V_PLAN_PHYSICAL_MONTHLY`.
- **Воспроизводимость:** версии неизменны, и их хеш сверяется при каждом чтении.

Для PLAN-2 нужны выбор экономического канона (WB_FE_V1 / Forward Ozon) и правило цены по
месяцам. Оба решения — за владельцем.
