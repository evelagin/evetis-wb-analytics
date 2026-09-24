# PR-PROMO-2 — каноническое состояние акций

**Дата:** 2026-09-23 (имя файла сохраняет дату серии документов акций, 2026-09-22).
**Статус:** реализовано (часть I, PR #164), развёрнуто 2026-09-23 из `main 000b253`, проверено на
живых объектах и на новом слоте наблюдения, снимок production внесён в манифесты (часть II,
§21–§24). **DONE.**

**На какой вопрос отвечает слой:** какое состояние было наблюдаемо для площадки / акции / SKU
в момент T.
**На какой не отвечает:** входить ли EVETIS в акцию, оставаться или выходить. Экономики,
вклада, требуемого роста, запасов, порогов политики и рекомендаций здесь нет — это PR-PROMO-3+.

Три слоя: **RAW** — что вернула площадка (PR-PROMO-1, не изменялся); **CANONICAL** — что эти
факты означают, с явной доказательностью (этот PR); **DECISION** — что делать (дальше).

---

## 1. Предварительное исследование

Проверено до первой строки SQL, 2026-09-23, на живых объектах (только чтение).

| Что | Результат |
|---|---|
| `origin/main` | `2cbc734` (после #160 Platform Baseline, #161–#163). Работа в отдельной ветке от него |
| Схемы 10 таблиц PR-PROMO-1 | живые `INFORMATION_SCHEMA.COLUMNS` = DDL `sql/promotions/pr_promo1_raw.sql`; фикстуры типизируются из этого DDL |
| Слоты наблюдений | по **6** годных слотов на площадку: 22.09 14:00 и 19:00, 23.09 04:00, 09:00, 14:00, 19:00 UTC |
| Реальные изменения между слотами | WB: 11 → **12** акций в 23.09 14:00 (новая 2958). Ozon: автодобавление 19 → 18 и 64 → 47 строк в 23.09 04:00 (дата 22.09 21:00 прошла); `marketing_actions` 278 → 280 → 298 |
| Время снимка | `observed_at` манифеста **≠** времени данных у слотов `REUSED`: WB T1 14:01:11 (строки) против 15:02:58 (манифест), Ozon T1 14:04:40 против 16:58:20. Повтор слота переписывает манифест, строки остаются от первого прогона. Канон берёт время из строк |
| `REF_SKU_CHANNEL_MAP` | OZON: 20 текущих строк, `marketplace_product_id`, `offer_id`, `marketplace_sku` уникальны; 2 неактуальные. WB: 25 текущих, `marketplace_sku` = nm_id |
| Канонический контракт | `sql/current/**` (R2A/R2B/R2C): `ozon_mart` и `evetis_mart` покрыты на 100 %, `wb_mart` — **0 %** (CANONICAL_COVERAGE §3) |
| Политика изоляции | `EXTERNAL_DATASET_POLICY`: `wb_mart` ← `wb_raw`, `wb_ops`, `evetis_ref`; `ozon_mart` ← `ozon_raw`, `evetis_ref`; `evetis_mart` ← `wb_mart`, `ozon_mart`, `evetis_ref` |
| Конфликт имён | ни одного объекта `*PROMO*` в `wb_mart`, `ozon_mart`, `evetis_mart` |
| Состояние здоровья | наблюдатели акций в `OPS_PIPELINE_REGISTRY` не зарегистрированы (L-6), порога свежести для них в платформе нет |

**Решение по размещению**, выведенное из контракта, а не выбранное:

- WB → `wb_mart`, **вне** `sql/current` (`sql/promotions/pr_promo2/wb_mart/`). Частичный манифест
  `wb_mart` сделал бы R2C постоянно `DRIFT`: 70 прежних объектов датасета стали бы
  `UNEXPECTED_LIVE`. Формат файлов — тот же «один объект, один `CREATE OR REPLACE VIEW`», чтобы
  канонизация `wb_mart` (вариант A CANONICAL_COVERAGE) перенесла их без правки.
- Ozon → `ozon_mart`, **в** `sql/current` (датасет полностью канонический: вью вне манифеста
  стало бы `UNEXPECTED_LIVE`).
- Межплощадочное → `evetis_mart`, в `sql/current`: единственное место, где площадки встречаются.

## 2. Зерно RAW (PR-PROMO-1, без изменений)

| Таблица | Зерно | Годность |
|---|---|---|
| `wb_raw.WB_PROMO_OBSERVATIONS` | `observation_id` | манифест прогона слота |
| `wb_raw.RAW_WB_PROMO_CALENDAR` | `observation_id × promotion_id` | факты уровня акции |
| `wb_raw.RAW_WB_PROMO_RANGING` | `observation_id × promotion_id × tier_ordinal` | лестница бустинга |
| `wb_raw.RAW_WB_PROMO_NOMENCLATURE` | `observation_id × promotion_id × in_action_requested × nm_id` | **0 строк**: все акции `auto` |
| `ozon_raw.OZON_PROMO_OBSERVATIONS` | `observation_id` | манифест |
| `ozon_raw.RAW_OZON_PROMO_ACTIONS` | `observation_id × action_id` | факты уровня акции |
| `ozon_raw.RAW_OZON_PROMO_PRODUCTS` | `observation_id × action_id × membership × product_id` | участники и кандидаты |
| `ozon_raw.RAW_OZON_PROMO_AUTO_ADD` | `observation_id × action_id × auto_add_at × list_kind × product_id` | автодобавление |
| `ozon_raw.RAW_OZON_PROMO_PRODUCT_MARKETING` | `observation_id × offer_id` | каталог продавца в снимке |
| `ozon_raw.RAW_OZON_PROMO_PRODUCT_ACTION` | `observation_id × offer_id × action_ordinal` | `marketing_actions.actions[]` |

Дублей по зерну в RAW — **0** во всех 10 таблицах (проверка V07). `observed_at` одинаков во
всех строках снимка во всех таблицах (V09).

## 3. Каноническая модель сущностей

| Сущность | Ключ | Где |
|---|---|---|
| Снимок (observation) | `marketplace × observation_id` | `*_PROMO_OBSERVATION_HISTORY` |
| Акция | `marketplace × source_promotion_id` (+ `observation_id` в истории) | `*_PROMO_HISTORY`, `V_PROMO_STATE_*` |
| Свидетельство уровня товара | `marketplace × observation_id × evidence_key` | `*_PROMO_SKU_EVIDENCE_HISTORY` |
| Разрешённое состояние товара в акции | `marketplace × source_promotion_id × marketplace_product_id` (+ `observation_id`) | `*_PROMO_SKU_STATE_*` |
| Наблюдаемость | `marketplace × capability` | `V_PROMO_OBSERVABILITY_CURRENT` |

**Глобального id акции нет и не создаётся.** `source_promotion_id` — строка id источника,
ключ всегда пара с `marketplace`. Совпадение названий или дат акций WB и Ozon не означает
одну кампанию.

`promotion_type` — значение источника без нормализации (`auto`, `regular`, `ELASTIC_BOOSTING`,
`STOCK_DISCOUNT`). Поля «механизм акции» нет: доказанной эквивалентности механик между
площадками нет, и общий столбец утверждал бы её.

## 4. Машина состояний

**Жизненный цикл акции** — только из дат источника относительно `observed_at` снимка:

| Состояние | Условие | Доказательность |
|---|---|---|
| `UPCOMING` | `observed_at < starts_at` | `DERIVED_DETERMINISTIC` |
| `ACTIVE` | `starts_at ≤ observed_at ≤ ends_at` | `DERIVED_DETERMINISTIC` |
| `ENDED` | `observed_at > ends_at` | `DERIVED_DETERMINISTIC` |
| `UNKNOWN` | даты не отданы | `UNKNOWN` |

Граница включительная: обе площадки отдают конец как последнюю секунду (`20:59:59Z` =
23:59:59 МСК). `CURRENT_TIMESTAMP` в истории не участвует нигде (тест
`test_history_views_do_not_depend_on_query_time`).

Дополнительные состояния, которые источники могли бы поддержать, и почему не добавлены:
**FROZEN** (Ozon `freeze_date`: «продавец не может повышать цены и менять список товаров») —
у всех 12 акций `NULL` за все 6 слотов, момент начала заморозки не наблюдался; поле сохранено
как факт `freeze_at`. **CANCELLED** — ни одна площадка не отдаёт признак отмены.

**Участие продавца в акции** (уровень акции): Ozon `is_participating` —
`PARTICIPATING`/`NOT_PARTICIPATING`, `DIRECT_SOURCE`; WB — из `inPromoActionTotal`: `> 0` →
`PARTICIPATING`, явный `0` → `NOT_PARTICIPATING`, `DERIVED_DETERMINISTIC`. `NULL` → `UNKNOWN`.

**Состояние товара в акции** — не булево значение, а две оси и итог:

| Ось | Значения |
|---|---|
| `participation_state` (сейчас) | `PARTICIPATING` · `CANDIDATE` · `NOT_ELIGIBLE` · `UNKNOWN` |
| `auto_add_state` (будущая дата) | `SCHEDULED` · `ELIGIBLE` · `NOT_LISTED` · `NOT_APPLICABLE` · `UNKNOWN` |
| `resolved_state` (итог) | `PARTICIPATING` · `SCHEDULED_AUTO_ADD` · `AUTO_ADD_ELIGIBLE` · `CANDIDATE` · `NOT_ELIGIBLE` · `UNKNOWN` |

**Почему оси две, а не одна.** Это не интуиция, а наблюдение: в T1 товар 3874879926
одновременно был **участником** акции 4253043 (`/products`, `MANUAL`) и стоял в
**запланированном** автодобавлении на 22.09 21:00 (`/auto-add/products/list`, `AUTO`). После
этой даты он остался участником, а запись автодобавления исчезла. Автодобавление —
утверждение о будущей дате, участие — о настоящем. Схлопнув их в одно поле, мы потеряли бы
либо «участвует сейчас», либо «будет добавлен».

⚠️ Это уточняет фазу 0 (`PROMOTION_DISCOVERY` §4.3): «1 SKU `AUTO` на 22.09 (4253043), +29 ₽»
— этот товар уже был участником; автодобавление касалось продления, а не входа.

`NOT_PARTICIPATING` на уровне товара намеренно не используется: «нет в списке участников
при полном перечислении» у Ozon означает и «не участвует», и «не может быть добавлен»
(нет и в кандидатах), поэтому точное имя — `NOT_ELIGIBLE`.

## 5. Ozon: отображение источника и приоритет

| Endpoint | Поле / значение | `evidence_source` → `source_state` | Доказательность |
|---|---|---|---|
| `POST /v1/actions/products` | элемент `products[]` | `ACTION_PRODUCTS` → `PARTICIPATING` | `DIRECT_SOURCE` (`CONFIRMED_API`) |
| `POST /v1/actions/candidates` | элемент `products[]` | `ACTION_CANDIDATES` → `CANDIDATE` | `DIRECT_SOURCE` (`CONFIRMED_API`) |
| `POST /v1/actions/auto-add/products/list` | элемент на `auto_add_date` | `AUTO_ADD_LIST` → `SCHEDULED_AUTO_ADD` | `DIRECT_SOURCE` (`CONFIRMED_API` + `CONFIRMED_DOCS`) |
| `POST /v1/actions/auto-add/products/candidates` | элемент на `auto_add_date` | `AUTO_ADD_CANDIDATES` → `AUTO_ADD_ELIGIBLE` | `DIRECT_SOURCE` (`CONFIRMED_API`) |
| `POST /v5/product/info/prices` | `marketing_actions.actions[]` | `PRODUCT_MARKETING_ACTIONS` → `MARKETING_ACTION_LISTED` | факт листинга `DIRECT_SOURCE`, связь — §8 |
| любое иное значение `membership` / `list_kind` | — | `UNRECOGNISED` | `UNKNOWN` (V14 падает) |

**Приоритет** (`V_OZON_PROMO_SKU_STATE_HISTORY`, `resolution_rule`):

| Правило | Условие | `resolved_state` | Класс |
|---|---|---|---|
| R1 | в `/products` | `PARTICIPATING` | `DIRECT_SOURCE` |
| R2 | в `/auto-add/products/list` | `SCHEDULED_AUTO_ADD` | `DIRECT_SOURCE` |
| R3 | в `/auto-add/products/candidates` | `AUTO_ADD_ELIGIBLE` | `DIRECT_SOURCE` |
| R4 | в `/candidates` | `CANDIDATE` | `DIRECT_SOURCE` |
| R5 | ни в одном списке, перечисление полное, автодобавление известно | `NOT_ELIGIBLE` | `DERIVED_DETERMINISTIC` |
| R6 | иначе | `UNKNOWN` | `UNKNOWN` |

R1 первым: участие — факт настоящего, всё остальное — возможности. R2 > R3 > R4: чем меньше
действий продавца нужно для фактического участия — «площадка добавит» > «площадка может
добавить сама» > «продавец может добавить вручную».

Свидетельства не теряются: оба флага-источника остаются на строке (`in_participants_list`,
`in_candidates_list`, `in_auto_add_scheduled_list`, `in_auto_add_eligible_list`), а
`participation_state` и `auto_add_state` сохраняют каждую ось. Товар одновременно в
`/products` и `/candidates` — `evidence_conflict = TRUE` (так источник делать не должен:
за 6 слотов 0 случаев), итог `PARTICIPATING`.

**Когда возможен `NOT_ELIGIBLE`.** Вселенная пар = пары со свидетельством + (акции снимка ×
товары каталога того же снимка). Отсутствие в списках превращается в `NOT_ELIGIBLE` только
если число перечисленных строк `/products` и `/candidates` совпало с объявленными
`participating_products_count` и `potential_products_count`
(`sku_membership_observability = OBSERVED`). Иначе `ENUMERATION_INCOMPLETE` → `UNKNOWN`. За 6
слотов расхождений 0: 72 из 72 «акция × снимок» перечислены полностью.

**Флаги списков трёхзначны:** `TRUE` — в списке; `FALSE` — не в списке при доказанно полном
перечислении; `NULL` — не знаем (перечисление неполное) или список не запрашивался (у акции
нет дат автодобавления).

## 6. WB: модель наблюдаемости

| `nomenclature_status` (RAW) | `sku_membership_observability` | Причина |
|---|---|---|
| `SKIPPED_AUTO_PROMOTION` | `NOT_OBSERVABLE` | `AUTO_PROMOTION_SKU_LIST_NOT_PROVIDED_BY_SOURCE` |
| `UNSUPPORTED_422` | `NOT_OBSERVABLE` | `SOURCE_ANSWERED_HTTP_422` |
| `FETCHED` | `OBSERVED` | `SOURCE_ENUMERATED_NOMENCLATURES` |
| `EMPTY` | `OBSERVED_EMPTY` | `SOURCE_RETURNED_EMPTY_LIST` |
| иное | `UNKNOWN` | `UNRECOGNISED_NOMENCLATURE_STATUS` |

**Отсутствие ≠ FALSE.** Для автоакций WB нет ни одной строки уровня SKU нигде в
каноническом слое — ни `NOT_PARTICIPATING`, ни `UNKNOWN`-заглушек, ни декартова произведения
«каталог × акция» (тесты `test_wb_has_no_catalog_cartesian`, V16, V17). Ответ на вопрос
«участвует ли nm_id X в автоакции Y» — на уровне акции:
`sku_membership_observability = NOT_OBSERVABLE`.

Агрегаты `inPromoActionTotal`, `inPromoActionLeftovers`, `notInPromoActionTotal`,
`exceptionProductsCount`, `participationPercentage` остаются фактами **уровня акции** и в
строки SKU не опускаются. Лестница бустинга — `V_WB_PROMO_RANGING_HISTORY`, тоже уровень акции.

Для обычных акций (`regular`) путь готов: `inAction = TRUE` → `PARTICIPATING`, `FALSE` →
`CANDIDATE` (документация WB; живыми данными не подтверждено — таких акций у EVETIS не было).
Отрицательных состояний WB из отсутствия строки не выводит: полноту перечисления WB доказать
сегодня нечем.

## 7. Доказательность

| Класс | Смысл | Где встречается |
|---|---|---|
| `DIRECT_SOURCE` | значение прямо сказано источником | участие/кандидат/автодобавление Ozon, `is_participating`, списки WB |
| `DERIVED_DETERMINISTIC` | однозначно выведено из фактов источника | жизненный цикл, `NOT_ELIGIBLE` при полном перечислении, участие продавца WB из агрегата, `NOT_APPLICABLE` |
| `STRONGLY_INFERRED` | выведено с допущением | только `current_lifecycle_status` акции, исчезнувшей из последнего снимка (допущение: даты не менялись) |
| `UNKNOWN` | доказать нельзя | нет дат, неполное перечисление, нераспознанное значение |

Ни одно выведенное значение не помечено как прямой факт. Нечётких выводов нет.

## 8. Связь `marketing_actions[]` с акцией (L-5)

`marketing_actions.actions[]` в `/v5/product/info/prices` несёт `title`, `date_from`,
`date_to`, `value` — **id акции нет**. Другого стабильного поля нет: других ключей элемент не
содержит (`raw_item_json` 6 слотов).

| Класс | Правило | Факт, 6 слотов |
|---|---|---|
| `DIRECT_ID` | id из источника | только `/products`, `/candidates`, `/auto-add/*` |
| `DETERMINISTIC_EXACT_MATCH` | в том же снимке ровно одна акция `/v1/actions` с `title`, **побайтно** равным, И `date_from = date_start` И `date_to = date_end` | **94 строки / 9 названий** в каждом слоте |
| `AMBIGUOUS` | несколько акций с тем же `title`, либо `title` совпал, а окно — нет | **0** |
| `UNRESOLVED` | такого `title` в `/v1/actions` нет | 184 → 186 → **204** строк, 13 → 14 названий |

Нет `TRIM`, `LOWER`, `LIKE`, расстояния редактирования (тест
`test_marketing_title_link_is_exact_only`). Хвостовой пробел — часть названия: «Максимальный
бустинг » (4253043) совпадает только потому, что пробел есть в обоих источниках.
Дубли `title` не разрешаются никогда, даже если окно дат совпало ровно у одной из акций.

`UNRESOLVED` — программы Ozon, которых нет в `/v1/actions`: «РК. Рассрочка 0-0-4/6/12»,
«Семплинг в Реф. Программе Ozon x VK Adblogger» (×6), «Товары за 1 рубль на Ozon fresh» (×4),
«Скидка 20 % … по промокоду FR20SALE» (с 23.09 14:00). Сущностью «акция» они **не становятся**:
id нет, придумывать его нельзя. Они видны как свидетельства с `source_action_title`.

**Сверка, а не источник состояния.** `marketing_actions_corroborated` на строке состояния:
`TRUE` — точная связь есть; `FALSE` — товар в каталоге снимка, неоднозначных строк у него нет,
связи нет; `NULL` — не знаем. В состояние не входит. Факт: все 94 участия последнего снимка
подтверждены (`TRUE`), все 146 прочих пар — `FALSE`; расхождений 0.

## 9. Семантика не доказана (L-3/L-4) — `SEMANTICS_UNRESOLVED`

В канон **не выносятся**, остаются только в RAW: `stock`, `min_stock`,
`price_min_elastic_rub`, `price_max_elastic_rub`, `alert_max_action_price_rub`,
`alert_max_action_price_failed` (Ozon `/products`, `/candidates`) и `value` из
`marketing_actions` (разнородная единица: рубли, проценты, срок рассрочки). В разрешении
состояния не участвуют. Тест `test_unresolved_semantics_not_exposed`.

## 10. Идентичность SKU

Источник — `evetis_ref.REF_SKU_CHANNEL_MAP`, `is_current`. Канон резолвит заново (а не берёт
отметку наблюдателя, замороженную на момент загрузки): исправление справочника сразу
исправляет всю историю.

| Площадка | Порядок | `mapping_status` |
|---|---|---|
| Ozon | `product_id` → `marketplace_product_id`, иначе `offer_id` | `MAPPED_BY_PRODUCT_ID` · `MAPPED_BY_OFFER_ID` · `AMBIGUOUS_MAPPING` · `UNMAPPED` |
| WB | `nm_id` → `marketplace_sku` | `MAPPED_BY_NM_ID` · `AMBIGUOUS_MAPPING` · `UNMAPPED` |

Выход: `internal_sku`, `marketplace_product_id` (WB nm_id, Ozon product_id), `offer_id` (Ozon;
WB источник акций его не отдаёт), `ozon_sku` (в Ozon-представлениях). Несопоставленный товар
**не отбрасывается**: строка есть, `internal_sku = NULL`, `mapping_status = UNMAPPED` (FX08,
V18). Неоднозначное сопоставление (несколько `internal_sku`) даёт `NULL`, а не произвольный
выбор. Факт: Ozon 20 из 20 товаров сопоставлены по `product_id` во всех 6 слотах. WB: строк
уровня SKU нет — сопоставлять нечего, и это не пробел покрытия.

## 11. История и текущее состояние

- **История** — по одной строке на годный снимок. Ничего не схлопывается: два слота с
  неизменным состоянием дают две строки (FX12), изменение состояния видно между строками
  (FX13, и в production — 3874879926).
- **Текущее** выводится из истории, второй изменяемой истины нет: строка = последнее
  наблюдение сущности. Последний годный снимок площадки берётся из
  `V_PROMO_OBSERVATION_HISTORY`, а не из строк сущностей — иначе пустой снимок или исчезнувшая
  акция выглядели бы текущими (FX14).
- **Годный снимок**: `prod`, id по контракту слота, последняя строка манифеста `COMPLETE` или
  `REUSED`. `ERROR` и `STARTED` исключены: их строки могут быть неполными (FX14).
- Исчезнувшая сущность остаётся в текущем с `present_in_latest_observation = FALSE`.

## 12. Контракты зерна

| Объект | Зерно / логический ключ | Временной ключ | Источники | Дедупликация | NULL | Наблюдаемость |
|---|---|---|---|---|---|---|
| `wb_mart.V_WB_PROMO_OBSERVATION_HISTORY` | `observation_id` | `observed_at` (данные) | манифест WB, календарь | последняя строка манифеста по `completed_at` | `catalog_products_observed` всегда NULL (у WB нет каталога) | — |
| `wb_mart.V_WB_PROMO_HISTORY` | `observation_id × promotion_id` | `observed_at` | календарь WB | по `ingested_at, source_payload_hash` | счётчики NULL = не отдано | `sku_membership_observability` |
| `wb_mart.V_WB_PROMO_RANGING_HISTORY` | `observation_id × promotion_id × tier_ordinal` | `observed_at` | лестница WB | то же | — | уровень акции |
| `wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY` | `observation_id × evidence_key` | `observed_at` | номенклатуры WB, справочник | то же | `internal_sku` NULL = не сопоставлен | только не-автоакции |
| `wb_mart.V_WB_PROMO_SKU_STATE_HISTORY` | `observation_id × promotion_id × nm_id` | `observed_at` | свидетельства WB | агрегирование | флаги автодобавления NULL | только пары со свидетельством |
| `ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY` | `observation_id` | `observed_at` | манифест Ozon, акции, каталог | последняя строка манифеста | — | — |
| `ozon_mart.V_OZON_PROMO_HISTORY` | `observation_id × action_id` | `observed_at` | акции, участники/кандидаты | по `ingested_at, source_payload_hash` | счётчики NULL = не отдано | `OBSERVED` / `ENUMERATION_INCOMPLETE` |
| `ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY` | `observation_id × evidence_key` | `observed_at` | 4 списка + `marketing_actions`, справочник | по зерну RAW каждого источника | `action_id` NULL у `AMBIGUOUS`/`UNRESOLVED` | все источники |
| `ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY` | `observation_id × action_id × product_id` | `observed_at` | история акций, свидетельства, каталог | агрегирование | флаги: TRUE / FALSE / NULL | R1–R6 |
| `evetis_mart.V_PROMO_OBSERVATION_HISTORY` | `marketplace × observation_id` | `observed_at` | оба `*_OBSERVATION_HISTORY` | UNION ALL | — | — |
| `evetis_mart.V_PROMO_STATE_HISTORY` | `marketplace × source_promotion_id × observation_id` | `observed_at` | оба `*_PROMO_HISTORY` | UNION ALL | как в источниках | да |
| `evetis_mart.V_PROMO_STATE_CURRENT` | `marketplace × source_promotion_id` | `last_observed_at` | история + снимки | последняя строка по `observed_at, observation_id` | — | + свежесть |
| `evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY` | `marketplace × observation_id × evidence_key` | `observed_at` | оба `*_SKU_EVIDENCE_HISTORY` | UNION ALL | `offer_id` NULL у WB | да |
| `evetis_mart.V_PROMO_SKU_STATE_HISTORY` | `marketplace × source_promotion_id × marketplace_product_id × observation_id` | `observed_at` | оба `*_SKU_STATE_HISTORY` | UNION ALL | флаги трёхзначны | да |
| `evetis_mart.V_PROMO_SKU_STATE_CURRENT` | `marketplace × source_promotion_id × marketplace_product_id` | `last_observed_at` | история + снимки | последняя строка пары | — | + свежесть |
| `evetis_mart.V_PROMO_OBSERVABILITY_CURRENT` | `marketplace × capability` | последний снимок | вся нейтральная история | — | — | это и есть наблюдаемость |

Дедупликация в каноне детерминирована, но дубль RAW — дефект наблюдателя, поэтому он
проверяется отдельно (V07), а не прячется. Зерно каждого объекта — FXG (фикстуры) и V01–V06
(production).

## 13. Семантика NULL

```
NULL ≠ 0            счётчики источника: NULL остаётся NULL (V20, FX11)
NULL ≠ FALSE        флаги списков: NULL = не знаем / не применимо (FXU, FXW)
UNKNOWN ≠ NOT_ELIGIBLE        неполное перечисление → UNKNOWN (FXU, V16)
NOT_OBSERVABLE ≠ NOT_PARTICIPATING   автоакции WB: состав не наблюдаем (FX09, V16, V17)
```

Завершённая акция WB 2850: `/details` вернул пустой массив → `aggregate_state =
NOT_RETURNED_BY_SOURCE`, оба счётчика `NULL`, `seller_participation_state = UNKNOWN`.
Синтетического нулевого участия нет. Явный ноль (2936, 2958: `inPromoActionTotal = 0`) —
`NOT_PARTICIPATING`: источник это прямо сказал.

## 14. Время

| Поле | Смысл | Часовой пояс |
|---|---|---|
| `starts_at`, `ends_at` | время события источника | `TIMESTAMP`, абсолютный момент (хранится UTC) |
| `starts_on_msk`, `ends_on_msk` | календарные даты площадки | `DATE(ts, 'Europe/Moscow')` — пояс назван явно |
| `observed_at` | время знания: момент ответа площадки | `TIMESTAMP` |
| `observation_slot` | логический слот наблюдателя | строка UTC `YYYY-MM-DDTHH:MM` |
| `manifest_observed_at` | время последнего прогона слота | справочно; для `REUSED` ≠ `observed_at` |
| `observation_age_minutes` | свежесть | единственное место с `CURRENT_TIMESTAMP` |

Сравнения жизненного цикла — только моменты с моментами, без дат и без неявного пояса.
Ozon пишет `ingested_at` со смещением +03:00, WB — в UTC; в `TIMESTAMP` оба абсолютны, в канон
`ingested_at` не выходит.

## 15. Исторический бэкфилл WB (L-7) — отложен

Оценено: API календаря WB отдаёт ~242 акции за ~2 года (фаза 0), `/details` для завершённых
акций пуст — бэкфилл дал бы только id, название, тип и даты.

**Почему не сделан.** Различить «время события» и «время знания» каноном можно
(`history_class`), но в RAW нет поля класса наблюдения: исторический снимок пришлось бы
отличать по шаблону `observation_id`, то есть по соглашению об именах. Это не безопасно:
такой снимок, окажись он последним, стал бы «текущим состоянием» 242 акций. Нужна отдельная
таблица или колонка класса в манифесте — изменение RAW, которое по условию этого PR
обосновывается отдельно. Данные не скоропортящиеся: API держит историю.

**Что сделано вместо.** Страж: в каноническую историю входят только снимки с id по контракту
слота, `history_class = OBSERVED`. Снимок с любым другим id исключается из истории и никогда
не становится текущим (FX15, V23). Будущий бэкфилл получит свой `history_class =
API_HISTORICAL` только вместе с явным полем класса в RAW.

## 16. Проверки качества данных

`sql/promotions/pr_promo2_canonical_validation.sql`, набор `promo_canonical_state`
(`quality/suites.json`, ворота). 24 блока, только чтение, пороги только нулевые.

| Блок | Что доказывает |
|---|---|
| V01–V06 | зерно всех 16 объектов; нет дублей текущего состояния; 16 строк наблюдаемости |
| V07 | в RAW нет дублей по зерну (10 таблиц) |
| V08 | последний снимок канона = независимо найденный последний годный снимок RAW |
| V09 | `observed_at` канона = время данных, одно на снимок во всех таблицах |
| V10 | жизненный цикл = независимый пересчёт из дат RAW |
| V11 | жизненный цикл не идёт назад без изменения дат |
| V12–V13 | ни одна строка RAW годного снимка не потеряна и не размножена; нейтральный слой = сумма площадок |
| V14 | каждое свидетельство отображено в известное состояние |
| V15 | `resolved_state` = пересчёт приоритета из флагов; кандидат ≠ запланирован ≠ участник |
| V16 | `NOT_ELIGIBLE` только при полном перечислении; автоакции WB `NOT_OBSERVABLE` и без строк SKU |
| V17 | строк WB уровня SKU ровно столько, сколько отдал `/nomenclatures` |
| V18 | каждый товар Ozon последнего снимка есть в каноне; покрытие — отчётно |
| V19 | каждая точная связь `marketing_actions` подтверждена независимо; у неоднозначных id нет |
| V20 | NULL источника не стал нулём и не стал `NOT_PARTICIPATING` |
| V21–V22 | текущее = история на последнем наблюдении сущности |
| V23 | в истории только наблюдённые слоты |
| V24 | ни одной колонки ПДн, экономики или решения (`INFORMATION_SCHEMA`) |

**До развёртывания** (тела из Git поверх живых RAW, `tools/promo_canonical_render.py run
predeploy`): **23 PASS, 1 FAIL** — V24, по построению (`INFORMATION_SCHEMA` не знает ещё не
созданных представлений).

## 17. Тесты

**Регрессионные сценарии на фикстурах** — `tools/promo_canonical_render.py run fixtures`.
Тела 16 представлений из Git дословно, RAW и справочник заменены типизированными фикстурами
(схема — из DDL PR-PROMO-1), ни одной таблицы не читается, исполняется движком BigQuery.
Второй реализации логики на Python нет.

| Блок | Сценарий спецификации §24 | Утверждений |
|---|---|---:|
| FX01 | 1 — только кандидат | 4 |
| FX02 | 2 — только запланирован | 4 |
| FX03 | 3 — участник | 4 |
| FX04 | 4 — кандидат + запланирован | 3 |
| FX05 | 5 — кандидат + участник | 3 |
| FX06 | 6 — кандидат + запланирован + участник; три состояния различны | 5 |
| FX07 | 7 — неоднозначный title | 11 |
| FX08 | 8 — нет сопоставления | 5 |
| FX09 | 9 — автоакция WB: агрегат есть, состава нет | 6 |
| FX10 | 10 — завершённая акция WB, пустые детали | 3 |
| FX11 | 11 — NULL агрегатов | 3 |
| FX12 | 12 — два слота, состояние не менялось | 4 |
| FX13 | 13 — два слота, состояние изменилось | 4 |
| FX14 | 14 — выбор последнего годного снимка, свежесть | 11 |
| FX15 | 15 — бэкфилл против наблюдённой истории | 4 |
| FXL, FXU, FXW, FXO, FXG | граница цикла, UNKNOWN/NOT_ELIGIBLE, обычные акции WB, наблюдаемость, зерно 16 объектов | 48 |
| **итого** | | **122** |

Результат: **20 блоков, 122 из 122 PASS**.

**Офлайн** (`tools/tests/test_promo_canonical.py`, CI `sql-current`): 23 теста — изоляция
площадок, порядок зависимостей, отсутствие `CURRENT_TIMESTAMP` в истории, жизненный цикл
только от `observed_at`, нет колонок экономики/решений/ПДн, нет чтения экономических
источников, `SEMANTICS_UNRESOLVED` не выведены, связь по title только точная, порядок
приоритета, нет декартова произведения WB, флаги автодобавления WB `NULL`, покрытие
сценариев 1–15, фикстуры соответствуют DDL, блоки фикстур — только SELECT без таблиц
production, проверки production рендерятся до развёртывания и остаются read-only, откат
удаляет ровно 16 объектов, манифест объявляет схему.

| Набор | До | После |
|---|---:|---:|
| `pytest tools/tests` | 362 passed (на `2cbc734`) | **385 passed** (+23), 0 failed |
| `pipelines/ozon` pytest, `cloud` vitest | не затронуты | не затронуты |

## 18. Объекты production (план)

16 `CREATE VIEW`, **0** изменений существующих объектов. `tools/promo_canonical_deploy.py`:
закрытый список, ровно один `CREATE OR REPLACE VIEW` своего имени в файле, никаких иных
операторов; с `--apply` — только из чистого дерева на `origin/main`. План (dry-run тел поверх
живых RAW): 16 × `CREATE`, 0 × `REPLACE`.

| Датасет | Объекты | Файлы |
|---|---|---|
| `wb_mart` | `wb_mart.V_WB_PROMO_OBSERVATION_HISTORY`, `wb_mart.V_WB_PROMO_HISTORY`, `wb_mart.V_WB_PROMO_RANGING_HISTORY`, `wb_mart.V_WB_PROMO_SKU_EVIDENCE_HISTORY`, `wb_mart.V_WB_PROMO_SKU_STATE_HISTORY` | `sql/promotions/pr_promo2/wb_mart/` |
| `ozon_mart` | `ozon_mart.V_OZON_PROMO_OBSERVATION_HISTORY`, `ozon_mart.V_OZON_PROMO_HISTORY`, `ozon_mart.V_OZON_PROMO_SKU_EVIDENCE_HISTORY`, `ozon_mart.V_OZON_PROMO_SKU_STATE_HISTORY` | `sql/current/ozon_mart/` (`pending_deploy`) |
| `evetis_mart` | `evetis_mart.V_PROMO_OBSERVATION_HISTORY`, `evetis_mart.V_PROMO_STATE_HISTORY`, `evetis_mart.V_PROMO_STATE_CURRENT`, `evetis_mart.V_PROMO_SKU_EVIDENCE_HISTORY`, `evetis_mart.V_PROMO_SKU_STATE_HISTORY`, `evetis_mart.V_PROMO_SKU_STATE_CURRENT`, `evetis_mart.V_PROMO_OBSERVABILITY_CURRENT` | `sql/current/evetis_mart/` (`pending_deploy`) |

Откат — `sql/promotions/pr_promo2_rollback.sql`: 16 `DROP VIEW IF EXISTS` в обратном порядке.
RAW, наблюдатели, Terraform, расписания, права — не затрагиваются. Маркетплейсы не вызываются.

Стоимость: полный скан самого тяжёлого `V_PROMO_OBSERVABILITY_CURRENT` на 6 слотах — доли
мегабайта; все объекты — VIEW, без расписания и состояния.

## 19. Доказательства до развёртывания (живые RAW, тела из Git)

| Факт | Значение |
|---|---|
| Годные снимки | WB 6, Ozon 6; `observed_at_basis = RAW_ROWS` у всех 12 |
| Акции в последнем снимке | WB 12: 7 `ACTIVE`, 4 `UPCOMING`, 1 `ENDED` (2850). Ozon 12 `ACTIVE` |
| WB, наблюдаемость состава | 12 из 12 `NOT_OBSERVABLE`; строк SKU WB 0; лестница 29 ступеней |
| WB 2850 | `ENDED`, `NOT_RETURNED_BY_SOURCE`, счётчики NULL, участие `UNKNOWN` |
| Ozon, пары последнего снимка | 240 = 12 × 20: `PARTICIPATING` 94, `AUTO_ADD_ELIGIBLE` 35, `CANDIDATE` 26, `NOT_ELIGIBLE` 85, `UNKNOWN` 0 |
| Ozon, автодобавление у участников | 17 `PARTICIPATING` + `SCHEDULED` (продление в «Эластичном бустинге»), 1 + `ELIGIBLE` |
| Изменение между слотами | 3874879926 × 4253043: `SCHEDULED` → `NOT_LISTED` после 22.09 21:00, участие неизменно |
| Сопоставление Ozon | 20 / 20 по `product_id` |
| `marketing_actions` | 94 точных связи (9 названий), 0 неоднозначных, 204 `UNRESOLVED` (14 программ Ozon); подтверждение участия 94/94 |
| Нейтральная история | 140 строк акций (WB 68, Ozon 72), 1 440 строк состояния товаров, 3 068 свидетельств (все Ozon) |

## 20. Оставшиеся ограничения

| # | Ограничение | Класс |
|---|---|---|
| L-2 | Состав автоакций WB по SKU не наблюдаем — контракт площадки | `NOT_OBSERVABLE`, представлено явно |
| L-3/L-4 | Семантика `stock` и `price_*_elastic` не доказана | `SEMANTICS_UNRESOLVED`, только RAW |
| L-5 | `marketing_actions[]` без id: 204 строки — программы Ozon вне `/v1/actions` | `UNRESOLVED`, видимы как свидетельства |
| L-6 | Наблюдатели не в `OPS_PIPELINE_REGISTRY`: у свежести нет утверждённого порога | свежесть видна, порога нет |
| L-7 | Исторический бэкфилл WB | отложен (§15) |
| L-10 | `wb_mart`-часть вне `sql/current`: R2C её не сверяет | граница контракта CANONICAL_COVERAGE §3 |
| L-11 | `regular`-акции WB и `SCHEDULED_AUTO_ADD` без участия в production не встречались | покрыты только фикстурами |
| L-12 | `FROZEN` не выводится: `freeze_date` не наблюдалась заполненной | поле сохранено |

---

# Часть II. Развёртывание и проверка в production

## 21. Развёртывание

| | |
|---|---|
| Источник | слитый `origin/main` **`000b253cc86dfb720b51f61cd46a9322936b5e82`** (PR #164), чистое дерево, detached HEAD |
| Инструмент | `tools/promo_canonical_deploy.py --apply` |
| План перед применением | 16 × `CREATE`, 0 × `REPLACE`: ни одного из 16 имён в production не было |
| Результат | 16 заданий, все `statementType = CREATE_VIEW`, 2026-09-23 ~21:10 UTC |
| Затронуто кроме этого | ничего: RAW, наблюдатели, Terraform, расписания, IAM, маркетплейсы — нет. Объекты `wb_mart`, изменённые за 3 часа до проверки, — плановые пересборки `EXECUTIVE_V2_DAILY`, `SKU_PERFORMANCE_V2_DAILY`, `UNITKA_COGS_EFFECTIVE` (20:11–20:50), до развёртывания |

Задания: `job_S8wSzyIZQejYLVdzg9xZUpQhLNWo` … `job_zVHMlv23bPSf3IarZTNw6rFrsLi7` (по одному на
объект, в порядке `OBJECTS`).

## 22. Сверка с Git

| Проверка | До | После |
|---|---|---|
| R2C (`verify_current_sql_live.py`), 29 объектов `ozon_mart` + `evetis_mart` | `DRIFT`: 18 `MATCH`, 11 `MISSING_LIVE` (ожидаемо), `UNEXPECTED_LIVE` 0 | `PENDING`: 18 `MATCH`, 11 `PENDING_DEPLOYED_MATCH`, провенанс подтверждён (`000b253`) |
| После внесения снимка (этот PR) | — | **`MATCH` 29 / 29** |
| 5 представлений `wb_mart` (вне R2C) — хеш тела `canonical_hash_v1` и описание из `tables.get` против Git | — | **5 / 5 совпали** |

Снимок: `capture_main_sha = 000b253…`, `captured_at = 2026-09-23T21:15:03Z`, 11 объектов
`pending_deploy` → `captured_live`, `bigquery_verified`.

## 23. Проверки на живых объектах

| Прогон | Слоты | Результат |
|---|---|---|
| сразу после развёртывания | 6 + 6 | `promo_canonical_state`: **24 / 24 PASS**, 0,04 ГБ |
| после нового слота 2026-09-24 04:00 UTC | 7 + 7 | **24 / 24 PASS** |
| регрессионные сценарии из `main` | — | 20 блоков, **122 / 122 PASS**, обработано **0 байт** (ни одной таблицы) |

## 24. Новый слот: состояние меняется, история сохраняется

Слот 2026-09-24T04:00 наблюдателями записан сам (обе площадки `COMPLETE`). Канон подхватил
его без какого-либо действия — это VIEW над историей.

| Акция | T1…T6 | T7 (24.09 04:00) | Почему |
|---|---|---|---|
| WB 2866 «дополнительный бустинг» | `ACTIVE` ×6 | **`ENDED`** | конец 23.09 20:59:59Z |
| WB 2932 «Бархатные скидки: финал» | `UPCOMING` ×6 | **`ACTIVE`** | начало 23.09 21:00:00Z |
| WB 2850 | `ENDED` ×6 | `ENDED` | детали по-прежнему пусты, счётчики NULL |

`V_PROMO_STATE_CURRENT`: 2866 и 2932 — новый статус, `last_observation_id =
WBPROMO_prod_202609240400`, `observations_count = 7`, возраст наблюдения 58 минут. История
акций: WB 80 строк, Ozon 84, по 7 снимков, ни одна прежняя строка не изменилась.

Ozon в T7: 240 пар текущих, `PARTICIPATING` 94, `NOT_ELIGIBLE` 85, `AUTO_ADD_ELIGIBLE` 35,
`CANDIDATE` 26, `UNKNOWN` 0; сопоставление 20 / 20; `marketing_actions` 94 точных,
0 неоднозначных, 204 `UNRESOLVED`. WB: 12 из 12 акций `NOT_OBSERVABLE`, строк SKU 0.
Единственная пара с изменённым состоянием товара за 7 слотов — 3874879926 × 4253043
(`SCHEDULED` → `NOT_LISTED`), участие неизменно.
