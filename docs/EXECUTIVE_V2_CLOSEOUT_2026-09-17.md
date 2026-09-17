# EXECUTIVE V2 — CLOSEOUT (Phase C3)

Дата: 2026-09-17. Статус: **Executive V2 FROZEN.**

Цепочка: `docs/FIN_CONTRACT_V2_2026-09-16.md` → `docs/EXECUTIVE_V2_BACKEND_2026-09-16.md` →
`docs/EXECUTIVE_V2_PHASE_B_IMPLEMENTATION_2026-09-16.md` → `docs/EXECUTIVE_V2_PHASE_C_PERFORMANCE_2026-09-16.md` →
`docs/EXECUTIVE_V2_PHASE_C2_MATERIALIZED_LAYER_2026-09-17.md` → этот документ.

Phase C3 не меняет metric contracts, SQL бизнес-логики, раскладку Executive и материализованную архитектуру.
Изменены только файлы валидации и документация.

## 1. Заморозка

Dashboard 2 «EVETIS · WB Executive» работает на карточках 187–217 поверх `wb_mart.V_DASH_EXECUTIVE_V2_DAILY`
(пересборка `executive-v2-layer-build`, каждый час в :10, 07:10–23:10 МСК). Дальнейшие изменения — только по
отдельной команде владельца с ACK. SKU Performance V2 не начата.

Сознательно **не делается** (решение владельца 2026-09-17):
- оптимизация 37 запросов dashboard 2 (объединение семи плиток карточки 164 и т. п.);
- кеш Metabase;
- `skip-if-sources-unchanged` — вынесено в backlog (§3).

## 2. OPEN KNOWN ISSUES

Не исправлены. Итоги Executive остаются такими, как считают канонические view. Любое исправление меняет
контрольные суммы и требует решения владельца.

| ID | Статус | Суть | Влияние | Где видно / чем контролируется | Что нужно для закрытия |
| --- | --- | --- | --- | --- | --- |
| **KI-2026-09-16-1** (KI-1) | 🔴 OPEN | LEGACY-строка «Продажа» `778547028#3129609040166` от 09.07.2026 (nm 1083392113, SKU EVT-SET-ACNE-TONIC-SERUM разрешён), но `sku_match_status = 'not_found'` | спред 484,76 ₽ не вычитается в `marketplace_fee_rub`; vw −183,72 и эквайринг 29,76 уходят на уровень счёта; **результат 09.07.2026 завышен на 638,72 ₽** | `fin_contract_v2_validation.sql` DQ-6/DQ-6b изолирует ровно этот ключ — новая такая строка уронит валидацию | решение владельца о пересопоставлении LEGACY-строки; `docs/FIN_CONTRACT_V2_2026-09-16.md` §4 |
| **KI-2026-09-16-2** (KI-2) | 🔴 OPEN | Строка «Возврат» 21.07.2026 (srid `eAL.r0beccb16df984929944be5399989c01b.0.0`): WB отдаёт `forPay` 547,44 положительным | `settlement_goods_rub` суммирует её как поступление, `marketplace_fee_rub` — как сбор (+292,56); если это сторно продажи, **выплата 21.07 завышена на 2 × 547,44 ₽** | `docs/EXECUTIVE_V2_BACKEND_2026-09-16.md` §9; официальная сверка выплаты этот день не покрывала | сверка с еженедельным отчётом WB за 20–26.07.2026 и решение о знаке |
| **KI-2026-09-16-3** (KI-3) | 🔴 OPEN | Логистика SKU вне активного universe: 88 строк LEGACY, 14 960 ₽, 08.04–07.05.2026 | не входит ни в `logistics_rub`, ни в уровень счёта; **в результат периода попадают одни сутки на 1 360 ₽** (остальные — до начала покрытия 13.04) | `V_DASH_KPI_DAILY.sku_costs_outside_universe_rub` | решение владельца: расширить universe или отнести на уровень счёта |

Все три относятся к датам не позже 21.07.2026 и не затрагивают контрольные окна Executive V2 (27.07–17.09.2026).

## 3. BACKLOG

### BL-EXEC-V2-01 · optional `skip-if-sources-unchanged` для `EXECUTIVE_V2_DAILY` — НЕ реализовано

- **Проблема.** `sp_build_executive_v2_daily` пересобирает слой 17 раз в сутки независимо от того, изменились
  ли источники: ~77 с, 1,8 ГБ, 13,1 тыс. slot-с на сборку ≈ 31 ГБ и 223 тыс. slot-с в сутки (замер 17.09).
  Сама сборка — 354 МБ; остальное — построчная повторная сверка A5.
- **Идея.** Перед `CREATE TEMP TABLE` вычислять отпечаток источников и сравнивать с отпечатком последней
  успешной сборки за **текущие сутки МСК**; при совпадении писать в журнал `SKIPPED_UNCHANGED` и выходить.
  Прецедент — `evetis_ref.sp_ct_refresh_daily` (сравнение отпечатка с последним успешным прогоном за день).
- **Обязательные ограничения.**
  - Первая сборка каждых суток МСК выполняется всегда: календарь и статусы зависят от даты.
  - Отпечаток должен покрывать все источники канонических view: как минимум `MART_RUNS`/`MART_SKU_DAILY.built_at`,
    загрузки `RAW_WB_FINANCE`, `RAW_WB_ADV_COSTS` (снапшоты биллинга), `FACT_SALES`, `FACT_ORDERS`,
    `evetis_ref.REF_SKU_COGS_HISTORY`, `REF_BUNDLE_COMPONENTS`, `wb_mart.REF_COST_MAP`. Пропущенный источник =
    устаревший слой при зелёной сборке — хуже, чем лишняя сборка.
  - Карточка свежести должна различать «витрина собрана» и «проверена без изменений».
  - Проверка: две сборки подряд без изменений → вторая `SKIPPED_UNCHANGED`; изменение любого источника из списка
    → полная сборка; A1–A6 не ослабляются.
- **Приоритет.** Низкий: стоимость известна и приемлема; реализовывать по отдельной команде.

## 4. Исправленные устаревшие и ложные проверки

Проверки не удалялись и не ослаблялись. Формула v1 заменена формулой V2 с тем же точным равенством; снимки
«количество объектов в датасете на август» (ломались на любом новом объекте и не проверяли сами объекты)
заменены инвариантами слоёв и закрытыми списками потребителей.

| Проверка | Было | Стало |
| --- | --- | --- |
| C-7 | `corrected − before = ad_billing_reconstructed` (v1) | `= атрибуция − биллинг по дате услуги + документы «WB Продвижение» + форс-мажор` (V2), `IS DISTINCT FROM` |
| C-12 | `corrected_account = account − ad_billing_reconstructed` | `− ad_billing_reconstructed − force_majeure` (Stage 3.1F) |
| C-14 | `evetis_ref` = 4 объекта | ни одно view `evetis_ref` не читает `wb_mart`, ни одна процедура — финансовые слои |
| C-15 | `wb_raw` = 56 объектов | ни одно view и ни одна процедура `wb_raw` не читает `wb_mart` |
| C-16 | `REF_COST_MAP` = 19 строк | 21 строка, уникальный ключ `(op_key, amount_field)`, `FARM_FINGERPRINT` смысловых колонок = утверждённый FIN CONTRACT V2 |
| C-17 | ни одно `V_DASH_*` не читает CORRECTED | потребители CORRECTED — ровно `V_DASH_EXECUTIVE_ECONOMICS_DAILY` (view) и `sp_build_executive_v2_daily` (процедура) |
| D-12 | `evetis_ref` = 4, `wb_mart` = 41 | 10 объектов Stage 3.1A–D существуют с нужным типом; ECONOMICS читает только `sp_build_executive_v2_daily` |
| G-11 | `LIKE '%_pct%' / '%margin%' / '%ratio%'` — ловил «ope**ratio**ns», «remune**ratio**n»; `_` в LIKE — любой символ | токены имени между `_`: `pct, percent, margin, ratio, share, rate` (шире прежнего списка) |
| G-13 | 42 / 56 / 4 объекта | SETTLEMENT — VIEW; читает только `sp_build_executive_v2_daily`; `wb_raw` и `evetis_ref` не зависят от `wb_mart` |

В исходном отчёте C2 были названы C-7, D-12, G-11: скрипт останавливается на первом упавшем ASSERT, поэтому
C-12, C-14…C-17 и G-13 из тех же файлов остались невидимыми. Покомпонентный прогон их выявил; они того же
класса и исправлены по тому же правилу.

**Мутационная проверка** (новые ASSERT обязаны ловить нарушение, 2026-09-17):

| Мутация | Результат |
| --- | --- |
| C-7 с формулой v1 | 156 нарушений |
| C-7 без форс-мажора | 2 нарушения |
| C-7 со сдвигом на 0,01 ₽ | 156 нарушений |
| C-12 без форс-мажора | 2 нарушения |
| C-16: одна строка `REF_COST_MAP` с другим `economic_direction` | отпечаток не совпал |
| G-11 на именах `margin_rub, buyout_ratio, fee_pct, share_of_revenue, commission_rate` | пойманы все 5; `…remuneration_rub`, `…operations_rub` — не пойманы (верно) |

## 5. Результаты валидации (2026-09-17)

Каждый файл запущен целым скриптом (fail-fast, как при выкате) и дополнительно — каждый ASSERT отдельно.

| Файл | ASSERT | Результат |
| --- | --- | --- |
| `sql/dash/executive_v2_daily_validation.sql` | 16 | ✅ 16/16 |
| `sql/dash/executive_v2_backend_validation.sql` | 16 | ✅ 16/16 |
| `sql/dash/fin_contract_v2_validation.sql` | 19 | ✅ 19/19 |
| `sql/dash/pr_dash_finance_corrected_validation.sql` | 17 | ✅ 17/17 |
| `sql/dash/pr_dash_executive_economics_validation.sql` | 15 | ✅ 15/15 |
| `sql/dash/pr_dash_settlement_validation.sql` | 14 | ✅ 14/14 |
| **Итого** | **97** | **✅ 97/97** |

`dashboard_contract_v1_validation.sql` и `dashboard_contract_v2_validation.sql` содержат только отчётные SELECT-гейты
(0 ASSERT) исторических стадий и в fail-fast набор не входят.

## 6. Metabase: репозиторий ↔ live

Сверка 2026-09-17 после перезапуска Metabase:
- состав: 109 карточек, 3 дашборда (2, 3, 4), 4 коллекции — live = `metabase/manifest.json`, лишних и
  недостающих нет;
- dashboard 2: 45 dashcards, карточки 187–217, позиции, `visualization_settings`, `parameter_mappings`,
  параметр «Период», ширина `full` — live = `metabase/dashboards/dashboard-2-evetis-wb-executive.json`;
  `manifest.dashboard_to_cards["2"]` = live;
- полный экспорт `tools/metabase_export_snapshot.sh` дал отличия **только** в волатильных полях статистики
  использования: `last_used_at` (109 карточек, 3 карточки внутри dashboard 2) и `query_average_duration`
  (2 карточки внутри dashboard 3), плюс производные sha256/`export_timestamp`. Смысловых расхождений ноль;
  экспорт откачен, чтобы не коммитить шум. Эти поля не входят в `excluded_volatile_fields` по конвенции
  снимка (см. заголовок скрипта).

Незакоммиченных изменений Phase B нет: Phase B/C закоммичены в `87edb4f`, C2 — в `71d51cc`, слиты в `main` (PR #130).
