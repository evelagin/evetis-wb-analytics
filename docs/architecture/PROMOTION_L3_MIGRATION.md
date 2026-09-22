# Манифест миграции UBR-010: продвижение Ozon на слой L3

**Статус: подготовлено и проверено, НЕ применено. Требуется ACK владельца.**
Дата: 2026-09-22. Ветка `arch/platform-baseline`. Решение владельца — вариант **C**
([`../ozon/OZON_SKU_PROMOTION_CLASSIFICATION_2026-09-22.md`](../ozon/OZON_SKU_PROMOTION_CLASSIFICATION_2026-09-22.md) §7).

Классификация: `promotion_billed_rub` — самостоятельный фактически начисленный расход на
продвижение Ozon, слой **L3**. Не уменьшает revenue. Не входит в `other_marketplace_costs_rub`.
Не смешивается с `ad_spend_attributed_rub`. Учитывается в управленческом семействе
promotion/advertising при расчёте полного расхода на продвижение и его ДРР.

---

## 1. Объекты и порядок применения

Порядок обязателен: каждый следующий объект читает предыдущий.

| # | Объект | Тип | Файл |
|---:|---|---|---|
| 1 | `ozon_mart.FCT_OZON_SKU_PNL_DAILY` | VIEW | `sql/current/ozon_mart/FCT_OZON_SKU_PNL_DAILY.sql` |
| 2 | `ozon_mart.FCT_OZON_SKU_PNL_MONTHLY` | VIEW | `sql/current/ozon_mart/FCT_OZON_SKU_PNL_MONTHLY.sql` |
| 3 | `ozon_mart.V_OZON_SKU_PNL_DAILY_OPERATIONAL` | VIEW | `sql/current/ozon_mart/V_OZON_SKU_PNL_DAILY_OPERATIONAL.sql` |
| 4 | `evetis_mart.FACT_SKU_DAILY` | VIEW | `sql/current/evetis_mart/FACT_SKU_DAILY.sql` |
| 5 | `wb_mart.V_CT_ACTUAL_DAILY_LIVE` | VIEW | `sql/control_tower/ct_promotion_l3_2026-09-22.sql` |
| 6 | `CALL evetis_ref.sp_ct_refresh_daily()` | процедура | пересборка `CT_ACTUAL_DAILY` |

Шаг 6 необязателен: то же расписание (`ct-refresh-prod`, 07:40 МСК и далее) пересоберёт
таблицу само. Явный вызов нужен, только чтобы увидеть результат сразу.

**Схема данных не меняется нигде**: все шесть шагов — замена вью и пересборка таблицы
существующей процедурой. `ALTER TABLE` нет. Процедуры не изменяются.

## 2. Что именно меняется в формулах

| Объект | Было | Стало |
|---|---|---|
| `FCT_OZON_SKU_PNL_DAILY` | `contribution_before_ads_rub` вычитал продвижение | не вычитает; `contribution_after_attributed_ads_rub` вычитает его вместе с рекламой — **значение не меняется** |
| `FCT_OZON_SKU_PNL_MONTHLY` | то же + `margin_before_ads_pct`, `variable_break_even_drr_pct`, `profitability_status` считались с продвижением | считаются без него; добавлены `promotion_family_rub` и `promotion_family_drr_pct` |
| `V_OZON_SKU_PNL_DAILY_OPERATIONAL` | `operational_contribution_before_ads_rub` вычитал продвижение | не вычитает; «после рекламы» не изменился |
| `evetis_mart.FACT_SKU_DAILY` | `contribution_after_ads_rub` НЕ вычитал продвижение, а `contribution_after_cogs_rub` вычитал — тождество ломалось | добавлена колонка `promotion_billed_rub`; `contribution_after_ads_rub` вычитает её; `contract_version` = `FACT_SKU_DAILY_V2` |
| `V_CT_ACTUAL_DAILY_LIVE` | корзины продвижения не было вовсе | корзина добавлена, вычитается в `seller_cash` и `contribution`; в `marketplace_costs` и `ad_spend` не входит; состав объявлен в `economics_basis` |

`ad_spend_attributed_rub` и `actual_drr_pct` **не переопределены**: остаются чистой атрибуцией.
Полный расход семейства и его ДРР живут в новых колонках с явно названным смешанным
основанием (атрибуция CPC + факт начисления продвижения).

Не изменены: revenue, commission, logistics, storage, acquiring, other marketplace costs.

## 3. Ожидаемое влияние (проверено до развёртывания)

Портфель Ozon, вся история:

| Величина | До | После |
|---|---:|---:|
| Выручка продавца | 1 884 733,16 | **1 884 733,16** |
| Расходы площадки | 883 976,09 | **883 976,09** |
| Вклад до рекламы | 530 505,93 | **540 934,60** |
| Семейство продвижения | 477 143,07 | **487 571,74** |
| Итог после рекламы | 53 362,86 | **53 362,86** |
| Маржа | 2,8313 % | **2,8313 %** |
| ДРР (только атрибуция, `actual_drr_pct`) | 25,3162 % | **25,3162 %** |
| ДРР семейства (`promotion_family_drr_pct`) | — | **25,8695 %** |

Меняются только «вклад до рекламы» (+10 428,67) и состав семейства продвижения. Итог,
выручка, расходы площадки и маржа неизменны.

`EVT-SET-CHERRY-AMBER` (наибольшее влияние, продвижение = 17,81 % выручки): итог SKU
−1 707,11 ₽ не меняется; ДРР атрибуции 17,25 %, ДРР семейства **35,05 %**.

Control Tower, август 2026: расхождение вклада с витриной падает с **10 336,14 ₽** до
**986,06 ₽**, то есть ровно до разницы выручки (UBR-012), которая этой миграцией не трогается.

## 4. Предпроверка

`sql/ozon/promotion_l3_2026-09-22/precheck.sql` — 5 блоков, прогон 2026-09-22 **5/5 PASS**:
новых колонок в production нет; состояние «до» соответствует откату (вклад до рекламы
530 505,93 ₽, 71 нарушение тождества в нейтральном факте); фикстура не изменилась
(84 начисления, 10 428,67 ₽); базовое расхождение CT зафиксировано; четыре потребителя ниже
по течению на месте.

## 5. Постпроверка

| Что | Команда | Ожидается |
|---|---|---|
| Сверка переноса | `run_data_checks --suite promotion_l3` | 9/9 PASS |
| Приёмка SCALE 1 | `run_data_checks --suite ozon_unit` | 11/11 PASS |
| Контракт Git | `validate_current_sql.py` | OK |
| Паритет с production | `verify_current_sql_live.py` | 18/18 MATCH (4 объекта перейдут из `PENDING_NOT_DEPLOYED` в `PENDING_DEPLOYED_MATCH`) |
| Control Tower | `run_data_checks --suite control_tower_phase1` | T10 остаётся красным по выручке — UBR-012 |

После подтверждённого `PENDING_DEPLOYED_MATCH` отдельным PR снимается снимок и
`sync_state` четырёх объектов переводится в `captured_live`.

## 6. Откат

| Шаг | Файл |
|---|---|
| 1 | `sql/ozon/promotion_l3_2026-09-22/rollback_V_CT_ACTUAL_DAILY_LIVE.sql` |
| 2 | `sql/ozon/promotion_l3_2026-09-22/rollback_FACT_SKU_DAILY.sql` |
| 3 | `sql/ozon/promotion_l3_2026-09-22/rollback_V_OZON_SKU_PNL_DAILY_OPERATIONAL.sql` |
| 4 | `sql/ozon/promotion_l3_2026-09-22/rollback_FCT_OZON_SKU_PNL_MONTHLY.sql` |
| 5 | `sql/ozon/promotion_l3_2026-09-22/rollback_FCT_OZON_SKU_PNL_DAILY.sql` |

Порядок обратный развёртыванию. Тела взяты **дословно** из Git на коммите
`52c9d6eefd4b78681effb49c9a80ecff76101060` (для CT — снято с production до применения).
Все пять прошли dry-run. Данных откат не удаляет: это замена вью. `CT_ACTUAL_DAILY`
вернётся к прежним числам после ближайшей пересборки.

Файлы отката внесены в `sql/current/historical_definitions.json` (C17) и подлежат ретайру
после того, как снимок переведёт объекты в `captured_live`.

## 7. Классификация риска: **СРЕДНИЙ, полностью обратимый**

Почему не низкий: затрагиваются четыре канонических объекта TIER 0 и вью Control Tower,
который читают экраны владельца; меняются публикуемые величины «вклад до рекламы» и
`margin_before_ads_pct`.

Почему обратимый и предсказуемый:

- ни одной записи данных, ни одного `ALTER TABLE`, ни одной процедуры;
- все шесть операторов прошли dry-run против текущего production;
- вся арифметика проверена **до** развёртывания подстановкой канонических тел
  (`tools/scale1_predeploy_render.py`): набор `promotion_l3` 9/9 PASS, приёмка SCALE 1
  11/11 PASS;
- итог периода, выручка, расходы площадки и маржа не меняются — меняется разрез;
- откат — пять замен вью телами из Git.

Остаточный риск: потребители, читающие `contribution_before_ads_rub` или
`margin_before_ads_pct` **вне** BigQuery (карточки Metabase), увидят другое число. По карте
потребления `ozon_mart` дашбордами не читается с 2026-09-06, но это наблюдение, а не гарантия.

## 8. Чего миграция НЕ делает

- Не трогает revenue, commission, logistics, storage, acquiring, other marketplace costs.
- Не переопределяет `ad_spend_attributed_rub` и `actual_drr_pct`.
- Не решает UBR-012 (расхождение определений выручки Control Tower и витрины).
- Не создаёт расписаний и не меняет существующие.
