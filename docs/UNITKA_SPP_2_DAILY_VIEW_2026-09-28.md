# СПП в Юнитку — этап 2: `wb_mart.V_WB_SPP_DAILY`

Дата: 28.09.2026. Предыдущий этап: `docs/UNITKA_SPP_1_ORDERS_PROBE_2026-09-28.md`.
Этот этап — только канонический слой BigQuery. Юнитка, колонка `AB`, ручные значения,
writer Юнитки и формулы **не меняются**.

## 1. Контракт

**Грейн:** `date_msk × nm_id`. Строка есть только там, где у SKU в этот день есть строки
Orders API (`wb_raw.V_WB_ORDERS`). Для дней без заказов строк нет — ничего не выдумывается.

**Две разные величины — не смешивать:**

| величина | поле(я) | смысл |
|---|---|---|
| сырая СПП WB | `raw_spp_avg_pct`, `raw_spp_median_pct`, `raw_spp_min_pct`, `raw_spp_max_pct` | поле `spp`, которое WB прислал в каждом заказе (целое, %) |
| эффективная СПП дня | `effective_spp_pct` | `100 × (1 − Σ finished_price·q / Σ price_with_disc·q)` — скидка, которую получил покупатель |

`effective_spp_pct` соответствует колонке `AB` Юнитки: `AA = Σ price_with_disc·q / Σ q`
(`V_UNITKA_DAILY_FACT`), `AC = AA × (1 − AB)`, и при `AB = effective_spp_pct`
получается `Σq × AC = Σ finished_price·q` — сумма, уплаченная покупателями.

| колонка | тип | смысл |
|---|---|---|
| `date_msk` | DATE | дата заказа WB (МСК) = `_order_date` |
| `nm_id` | INT64 | артикул WB |
| `internal_sku` | STRING | атрибут из `evetis_ref.REF_SKU_CHANNEL_MAP` (действующая на дату привязка, иначе текущая, иначе последняя); не ключ, строки без привязки не теряются |
| `orders_qty` | INT64 | Σ `quantity` всех заказов дня, **включая отменённые** |
| `cancelled_orders_qty` | INT64 | из них отменённых |
| `orders_with_spp` | INT64 | заказов с непустым `spp` |
| `raw_spp_avg_pct` / `raw_spp_median_pct` / `raw_spp_min_pct` / `raw_spp_max_pct` | FLOAT64 | статистики сырого `spp` (медиана — точная) |
| `zero_spp_orders` | INT64 | заказов с `spp = 0` (валидный факт WB, в NULL не превращается) |
| `sum_price_with_disc` / `sum_finished_price` | NUMERIC | Σ цены продавца и Σ суммы покупателя |
| `effective_spp_pct` | FLOAT64 | эффективная СПП; **не обрезается** — может быть < 0 |
| `neg_markup_orders` | INT64 | заказов, где покупатель заплатил больше цены продавца (рассрочка с наценкой) |
| `price_missing_rows` | INT64 | строк без цены или суммы покупателя (DQ) |
| `spp_source` | STRING | всегда `ORDER_SPP_ACTUAL` |
| `spp_status` | STRING | `OK` / `ZERO_SPP` / `NEGATIVE_MARKUP` / `PRICE_DATA_MISSING` |

**Статусы** (в порядке проверки): `PRICE_DATA_MISSING` — хотя бы у одной строки дня нет цены
или суммы покупателя (или Σ цены ≤ 0): `effective_spp_pct` = NULL, ноль не подставляется;
`NEGATIVE_MARKUP` — `effective_spp_pct < 0`; `ZERO_SPP` — `ROUND(effective_spp_pct, 1) = 0`;
иначе `OK`.

**Источник:** единственный — `ORDER_SPP_ACTUAL`. Нет fallback по выкупам, последнего
известного значения, ручного ввода, витрины и оценок.

## 2. Популяция = популяция `AA` Юнитки

`wb_mart.V_UNITKA_DAILY_FACT`, CTE `o`: `price = SAFE_DIVIDE(SUM(price_with_disc * quantity),
SUM(quantity))` по **всем** строкам `wb_mart.FACT_ORDERS` за `order_date` дня — без фильтра по
отмене. `FACT_ORDERS` — построчный pass-through `wb_raw.V_WB_ORDERS` (`ASSERT BUILD rows =
source rows`, `order_date = _order_date`). Вью берёт те же строки `V_WB_ORDERS` с тем же весом
`quantity`.

Доказательство на данных (28.09): во **всех 183** парах SKU × день сентября, где Юнитка берёт
`AA` из Orders API, `Σ price_with_disc / orders_qty` вью совпадает с `AA` до копейки
(расхождений 0).

## 3. DQ (тело вью как read-only запрос, 28.09, окно с 01.09)

| проверка | результат |
|---|---|
| строк SKU × день с 01.09 | 188 (OK 179, ZERO_SPP 8, NEGATIVE_MARKUP 1, PRICE_DATA_MISSING 0) |
| дубликаты грейна | 0 |
| NULL `effective_spp_pct` | 0 |
| вне [−100; 100] | 0 |
| без `internal_sku` | 0 |
| заказы: источник / вью | 392 / 392 (отмены 22 / 22) |
| Σ `price_with_disc`: источник / вью | 292 727,33 / 292 727,33 |
| Σ `finished_price`: источник / вью | 205 010,40 / 205 010,40 |
| популяция `AA` | 183 из 183 совпали |

Вся история: 1 797 строк, из них 955 — `PRICE_DATA_MISSING` (заказы до 25.06.2026 загружены
до этапа 1, ценовых полей у них нет). После развёртывания те же проверки —
`sql/unitka/spp2/v_wb_spp_daily_dq.sql`.

## 4. Shadow будущего writer (книга не менялась)

`future_AB_write_value = CASE WHEN effective_spp_pct < 0 THEN 0 ELSE ROUND(effective_spp_pct, 1) END`
против ручного `AB` сентября (снимок 28.09, 720 ячеек, 260 заполнено, SHA-256 `bdfebbd1…`):

| | SKU × день |
|---|---|
| пары вью, 01–27.09 | 183 |
| сопоставлено с ручным `AB` | 100: \|Δ\| ≤ 2 п.п. — 38; > 2 — 62; > 5 — 52; > 10 — 22; максимум 30 п.п. |
| новые заполнения (ручного нет) | 83 |
| ручное без СПП заказов | 160 |
| `NEGATIVE_MARKUP` | 1: 07.09 `535581674`, effective −7,65 %, запись 0, ручное 20 |

Эффект: прибыль — 0 (от `AB` зависят только `AC`, ДРР `Z`/`K`, `AC767`/`Z767`/`K767`);
ДРР магазина за 01–27.09 — 15,35 % → 19,46 %, из них +3,94 п.п. — заполнение пустых дней
18–27.09 (пустой `AB` считается как 0 %), +0,11 п.п. — замена ручных значений.

## 5. 160 ручных `AB` без СПП заказов

| класс | число |
|---|---|
| `Q = 0` (заказов нет, `AB` ни на что не влияет) | 158 |
| `Q > 0`, строк Orders API нет | 2 (08.09 и 10.09, `305101361`) |
| будущая дата | 0 |
| другое | 0 |

**Шесть дней, где Юнитка показывает заказы, а Orders API строк не даёт:** 08.09 и 10.09
`305101361`, 17.09 `930334396`, 22.09 `438775617`, 24.09 и 26.09 `535580776`. У всех шести
цена в Юнитке — `FUNNEL_FALLBACK`, `Q` — из воронки. Проверено:
- заказов нет ни в одной загрузке `RAW_WB_ORDERS`, ни в живом ответе API 28.09 — это не потеря
  при загрузке;
- в соседних днях воронка и Orders API совпадают точно — это не сдвиг даты;
- `nm_id` тот же — это не привязка SKU.

Итог: воронка WB (`nm-report`) засчитывает заказы, которые Statistics Orders API не отдаёт
(ещё один такой заказ — 27.09 `535580776`: воронка 9, API 8). СПП для таких дней взять неоткуда;
вью строк не создаёт, `AB` останется пустым, Integrity Guard — `SPP_MISSING`.

## 6. Будущий контракт writer (этап 3, не реализован)

`AB_WRITE_VALUE = MAX(effective_spp_pct, 0)`, округление до 0,1; только строки вью, только
закрытые дни (≤ LCD), с 01.09.2026. Отрицательная СПП в `AB` не пишется, в слое — сохраняется.

## 7. Почему вне `sql/current`

`wb_mart` не канонизирован (`docs/architecture/CANONICAL_COVERAGE.md` §3): частичный манифест
дал бы в сверке R2C `UNEXPECTED_LIVE` для остальных объектов датасета, это закреплено тестом
`test_wb_views_stay_outside_sql_current`. WB-вью идут принятым путём PROMO-2: файл
`sql/unitka/spp2/wb_mart/V_WB_SPP_DAILY.sql`, развёртывание
`tools/spp_daily_view_deploy.py` (закрытый список, dry-run по умолчанию, `--apply` — только
слитый `main`). Перенос в `sql/current` — после решения владельца по варианту A/B §3.

## 8. Развёртывание (после слияния PR)

```bash
python tools/spp_daily_view_deploy.py --project project-fa311fc0-4d87-4781-986 --token-command "gcloud auth print-access-token"
python tools/spp_daily_view_deploy.py --project project-fa311fc0-4d87-4781-986 --token-command "gcloud auth print-access-token" --apply
bq query --use_legacy_sql=false < sql/unitka/spp2/v_wb_spp_daily_dq.sql
```

Откат: `DROP VIEW wb_mart.V_WB_SPP_DAILY` (потребителей нет — вью новая).
