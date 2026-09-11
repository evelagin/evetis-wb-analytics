# EVETIS OPERATIONS — Architecture Review
## BigQuery truth · Google Sheets operations · simplified Metabase · Claude on the same data

**Date:** 2026-09-11 · **Mode:** read-only review. Nothing was coded, deployed, created or modified.
**Base:** local `main` = `3c29231` (Control Tower Phase 1.2) + `CT_STOCK_STATES_DESIGN_2026-09-11.md` (untracked).
**Evidence:** live BigQuery `INFORMATION_SCHEMA` / `__TABLES__` / views, Cloud Scheduler, Cloud Run, IAM, repo code.
Metabase was **not** verified live — the local container on `localhost:3000` was down; dashboard contents are taken
from the Phase 1 / 1.1 / 1.2 implementation records.

Labels: **FACT** = verified today · **INFERENCE** = derived from facts · **UNKNOWN** = must be verified before coding.

---

## 0. Production state that blocks this work (FACT, 2026-09-11 14:30 MSK)

These are not architecture questions, but every stage below depends on them. **Resolved in Stage 0 on 2026-09-11** — see `docs/ops/STAGE_0_CLOSURE_2026-09-11.md`.

| # | Finding | Evidence | Impact |
|---|---|---|---|
| P1 | **`wb-mart-prod` has been PAUSED since 2026-09-10 09:26 MSK** (~29 h). The Stage B cutover window stopped at checkpoint 1 and was never resumed. | Cloud Scheduler `state=PAUSED`; last `scheduler-control` run `34445212869` (pause). `MART_SKU_DAILY` max day = 2026-09-09, built 10.09 09:37. | MART is missing 2026-09-10. `H2_MART_LAG` tolerance is 2 days — it breaches tomorrow. Control Tower's `freshness_gate` only **warns** on a stale MART (it does not fail), so a CT refresh would have run on stale data with a WARN — the Sheets banner must treat WARN as stale. |
| P2 | **Today's WB ads run is STUCK** (`INS_ADS_20260911050728`, STARTED 05:07, never completed). Campaigns, billing, fullstats, booster landed by 05:12; **query bids and query stats did not** (last 10.09). | `wb_raw.INGEST_RUNS`, `__TABLES__.last_modified`. Billing window was 7 days (B4a not transferred — correct). | Unrelated to the cutover (INFERENCE: Apps Script 6‑min limit). The bid snapshot is point-in-time and **cannot be backfilled** — a manual re-run of the bids loader today is the only way to keep 11.09. |
| P3 | **Control Tower auto-refresh is not deployed.** `ct_refresh.tf` exists in code; production has no `ct-refresh-prod` scheduler and no `sa-ct-refresh`. | `gcloud scheduler jobs list`, `gcloud iam service-accounts list`. `CT_*` tables last modified 10.09 22:16 (manual). | Owner Home / Daily Brief show 10.09 data. A Sheets front end built on CT views would inherit this. |
| P4 | **Control Tower source is not on GitHub.** Phase 1 / 1.1 / 1.2 (`075f45a`, `7736eb0`, `3c29231`) exist only on local `main`; `origin/main` = `39c10fa`. | `git log origin/main..main`. | The backend being reviewed lives on one laptop + production BigQuery. |
| P5 | **Action web-app is not deployed.** `CtOwnerActions.gs` is in the repo; `CT_CONFIG.action_webapp_url` is empty, so the ✅/▶ columns in Metabase are blank. | Phase 1.2 doc §11; design doc §1.1. | The only write path from the owner UI does not exist yet — cheap to change direction now. |

---

## 1. Current implementation — what is useful, redundant, misplaced

### 1.1 Inventory (FACT)

`evetis_ref` — 14 tables: `CT_PLAN_VERSION`, `CT_SEASON_PLAN_MONTHLY` (264), `CT_DAILY_CURVE` (424), `CT_SEASON_PLAN` (7 710),
`CT_OPEX`, `CT_EXPIRY_BATCH`, `CT_STOCK_SNAPSHOT` (11), `CT_BUNDLE_PLAN` (83), `CT_OWNER_ACTION_QUEUE` (29),
`CT_ACTION_STATUS_LOG`, `CT_ACTUAL_DAILY` (11 413), `CT_INVENTORY_SNAPSHOT_DAILY` (11), `CT_REFRESH_LOG`, `CT_CONFIG`.

`wb_mart` — 21 views `V_CT_*`. Procedures: `sp_ct_refresh_daily`, `sp_ct_generate_actions`, `sp_ct_action_update`.
Metabase collection 9: dashboards **5** Owner Home, **6** Sales Plan, **8** Details, **9** Daily Brief.
Existing BI: **2** WB Executive, **3** WB SKU Performance, **4** Search Queries.

### 1.2 Assessment

| Area | Verdict | Why |
|---|---|---|
| Versioned plan (`CT_PLAN_VERSION`, `CT_SEASON_PLAN*`, `CT_DAILY_CURVE`, `V_CT_PLAN_ACTIVE`) | **Useful — keep** | Exactly the "planned daily sales" input the supply plan needs; versioned, BOM-aware. |
| Actuals (`CT_ACTUAL_DAILY`, `V_CT_PLAN_VS_ACTUAL_DAILY`, `V_CT_SKU_CONTROL`, `V_CT_CASH_CONVERSION`, `CT_OPEX`) | **Useful — keep, Metabase** | Commercial / financial analytics — BI territory. |
| Marketplace-side inventory (`V_CT_INVENTORY_TRUTH(_LIVE)`, `V_CT_SUPPLY_FLOW`, `CT_INVENTORY_SNAPSHOT_DAILY`) | **Useful — keep as the API half** | Correctly derives WB/Ozon on-marketplace and in-transit stock from API; already separates Ozon seed-era vs post-snapshot orders. |
| BOM (`V_CT_BOM_CURRENT` over `REF_BUNDLE_COMPONENTS`) | **Useful — keep** | Single BOM truth; solo SKU expands to itself, so one logic for all cards. |
| Expiry (`CT_EXPIRY_BATCH`, `V_CT_HAND_CREAM_CONTROL`) | **Useful — keep** | Hard deadline 31.12.2026 for hand cream; needed later for pick priority. |
| Refresh engine (`sp_ct_refresh_daily`: fingerprint guard, freshness gate, DML-only; `CT_REFRESH_LOG`, `V_CT_FRESHNESS`) | **Useful — keep** | This is the heartbeat the Sheets header will show. Not scheduled (P3). |
| Supply need (`V_CT_SUPPLY_NEED`) | **Right layer, wrong model — modify** | See §1.3. |
| Bundle status (`V_CT_BUNDLE_STATUS.assemblable_now_ff`) | **Modify** | Computed from **total** FF stock, not free stock → reserved units are "assembled" again. |
| Execution actions in `CT_OWNER_ACTION_QUEUE` (`PULL_FROM_PALLETS`, `ASSEMBLE_BUNDLES`, `REPLENISH_WB/OZON`) | **Misplaced** | An action is a proxy for a physical movement, but DONE changes no stock. The 03.09 work order sat un-executed for 6 days with nothing in the system noticing (`USEND_LK_AUDIT_REVIEW` §6). Belongs in a shipment / build / pick workflow. |
| Decision actions (`DECISION`, `PRICE_ACTION`, `ADS_REVIEW`, `DRR_*`, `OVERSTOCK_*`) | **Useful — keep in queue** | These are owner decisions, not stock moves. |
| Seed `CT_STOCK_SNAPSHOT.in_transit_to_ozon_units = 369` | **Misclassified** | Includes the 5 Ozon orders in `READY_TO_SUPPLY` (235 cards, pickup ≈17.09) that are **physically on the FF shelf**. |
| Metabase execution blocks (Owner Home B/C, Details shipments/bundles/queue, Daily Brief "Сегодня сделать") | **Misplaced in BI** | Metabase cannot write to BigQuery (Actions support Postgres/MySQL/H2 only — Phase 1.1 §1), headers don't wrap, no buttons, no inputs. |
| `CtOwnerActions.gs` web-app | **Redundant once Sheets exists** | Never deployed. Its validation + parameterized `CALL` pattern is reusable inside a Sheets-bound script. |
| `ct_refresh.tf` | **Keep — apply** | Correct design (Scheduler → BigQuery jobs API → `CALL` under a dedicated SA), just not applied. |

### 1.3 `V_CT_SUPPLY_NEED` — why it cannot drive shipments as is (FACT)

| Defect | Evidence |
|---|---|
| Target cover hard-coded: 45 days | `plan_units_next_45d`, `gap_units_45d` |
| No lead time, no safety stock | FF reserve is a constant: `LEAST(gap, ff_total_units − 20)` |
| **No box / min-shipment rounding** | Today: `EVT-FT-MOIST-150` WB **2** units, Ozon **8**; `EVT-FS-MOIST-30` Ozon **12** |
| Component grain only | Grain = `marketplace × internal_sku`; no split into solo shipment vs bundle production |
| **Per-channel cap against total FF** (latent double claim) | WB and Ozon rows are each capped by the same `ff_total_units`. Not active today (FF holds thousands, needs are hundreds), but will over-commit on low-stock SKUs (e.g. `EVT-FC-MOIST-50`, FF = 2). |
| Uses total FF, not free FF | Units already reserved for Ozon orders are offered again to WB. |

### 1.4 Data sources relevant to operations (FACT)

| Source | State it can prove | Gap |
|---|---|---|
| `V_WB_STOCKS_T5_CURRENT` | WB FBO on-marketplace; lost/claimed; to-client | WB **FBS stock is not loaded** |
| `V_WB_SUPPLIES_CURRENT` + `_GOODS_CURRENT` | WB shipment created / in transit / accepted (`accepted_quantity`) | `status_id` semantics (1/3/5) **UNKNOWN**; 16 draft supplies without warehouse since May |
| `RAW_OZON_SUPPLY_ORDERS` + `_SUPPLY_BUNDLES` | Ozon order `READY_TO_SUPPLY` (reserved, still on FF) → `ACCEPTANCE_*` (in transit) → `COMPLETED` (`quantity_accepted`) | No "left FF" moment |
| `RAW_OZON_STOCKS` | Ozon FBO on-marketplace | — |
| Usend FF | Nothing via API. LK counters are **not** a movement register; pallet→shelf moves are undocumented (negative shelf balances) | Accuracy ±50 units; pallet truth is the owner's file "ИП Елагина ДХ.xlsx" |
| Packaging (units per box, box/unit weight, min shipment, multiple) | — | **Absent in BigQuery.** Only in the old Sheets schema (`apps-script/Sheetsschema`: `units_per_box`, `units_per_masterbox`). WB has a 25 kg lot limit per PVZ shipment (CT‑0015/16). |
| Bundle assembly policy | `REF_BUNDLE_COMPONENTS.assembly_model` | All 14 bundles = `FF_ASSEMBLED` — no FBO-just-in-time vs FBS-stock distinction |

Open Ozon orders today: `READY_TO_SUPPLY` 5 orders / 235 cards (created 07.09, arrival 17.09); `ACCEPTANCE_AT_STORAGE_WAREHOUSE` 2 / 83 cards and `REPORTS_CONFIRMATION_AWAITING` 1 / 31 cards (arrival was 07.09 — four days of acceptance lag).

---

## 2. Re-evaluation of `CT_STOCK_STATES_DESIGN`

**Verdict: the backend stays. The front end changes. Seven refinements are needed before coding.**

### 2.1 Keep as designed

| Element | Why it is right |
|---|---|
| `CT_STOCK_MOVEMENT` — append-only, corrections by reversal | FF has no API; the only honest truth for FF-side states is a journal of human-confirmed events. |
| `sp_ct_stock_move` — single write path, white-listed transitions, `qty ≤ balance`, `RAISE` otherwise | Makes negative balances and illegal jumps impossible by construction — the basis of "one bottle, one state". |
| `V_CT_STOCK_STATE` — journal states + API states | Clean split: FF-side states from the journal, marketplace-side states from API. |
| "API never writes the journal" | The only reliable guard against double counting. |
| B2B as `channel = 'B2B'` on the same journal | No redesign later. |
| Additive-only change to existing views; `sellable_units` untouched | Keeps Phase 1 tests and Metabase intact. |

### 2.2 Refinements

| # | Refinement | Reason |
|---|---|---|
| R1 | **One grain for bundles**: component rows only, with `container_sku` (the bundle). Bundle quantity is derived. | The design writes component rows **and** a bundle row — two representations of the same bottles invite double counting. Add a bundle-integrity invariant instead. |
| R2 | **Shipment entity**: `CT_SHIPMENT` (header) + `CT_SHIPMENT_LINE`. | The design links movements by `ref_id` only. A shipment register, per-shipment API reconciliation and the dedup rule (§7) need a header carrying channel, destination, marketplace reference, boxes, weight, status. |
| R3 | **Build order**: `CT_BUNDLE_BUILD` with `purpose = FBO_SHIPMENT \| FBS_STOCK` and optional `shipment_id`. | FBO bundles are assembled right before shipment; FBS bundles are standing inventory. Today nothing distinguishes them. |
| R4 | **Logistics settings**: `REF_SKU_LOGISTICS` (per card × channel, versioned). | Box multiple, weights, min shipment, lead time, target cover, safety days, FBS reserve, assembly policy — none exist in BigQuery. |
| R5 | **Handover is derived, not written**: shipments leave the SHIPPED bucket when API is final; shortfall becomes an open discrepancy item until a human records its resolution. | In the design SHIPPED is the last journal state; without netting it grows forever. Deriving it keeps "API never writes the journal". |
| R6 | **Seed reclassification, BOM-expanded**: 235 `READY_TO_SUPPLY` cards = **314 physical units** → `RESERVED_OZON` on FF (ref = the 5 order ids); 114 cards at Ozon acceptance = 150 physical units → pre-existing shipments in SHIPPED with API refs; the manual 369 (which matches neither 349 cards nor 464 units) disappears. Conservation is always in physical units — see Delta v1 §3–§4. | Otherwise the first API state change counts the same bottles twice. |
| R7 | **Atomic, idempotent write-back**: shipment header + lines + movements in one BigQuery multi-statement transaction; every call carries `request_id`. | A partially written shipment is worse than none; a double click must not reserve twice. |

---

## 3. Target architecture

```
                 ┌───────────────── BigQuery (truth + calculations) ─────────────────┐
 WB / Ozon API ─►│ wb_raw / ozon_raw ─► FACT/MART ─► V_CT_* (plan, actuals, API stock) │
 (read-only)     │ evetis_ref: REF_SKU_LOGISTICS · REF_BUNDLE_COMPONENTS · CT_PLAN_*   │
                 │             CT_STOCK_MOVEMENT · CT_SHIPMENT(+LINE) · CT_BUNDLE_BUILD│
                 │             CT_B2B_ORDER (stage H)                                  │
                 │ wb_mart:    V_CT_STOCK_STATE ─► V_OPS_* (one view per Sheets tab)   │
                 │ procedures: sp_ops_* (validate + write, fail-closed, transactional) │
                 └───────▲───────────────────────┬────────────────────────┬───────────┘
                         │ CALL sp_ops_*          │ SELECT V_OPS_*          │ SELECT V_CT_* / V_DASH_*
                 ┌───────┴───────────────────────▼───────┐        ┌───────▼────────┐
                 │ Google Sheets «EVETIS OPERATIONS»      │        │ Metabase — BI  │
                 │ bound Apps Script: refresh + commit    │        └────────────────┘
                 └────────────────────────────────────────┘        Claude reads the same V_OPS_* / V_CT_*
```

Principles:

1. **All math in BigQuery views.** Sheets shows results, accepts inputs, and computes only non-authoritative display
   formulas (e.g. "cover after my override"). Gates are enforced in procedures, never in formulas.
2. **One view per tab** (`V_OPS_SUPPLY_PLAN`, `V_OPS_BUNDLE_PRODUCTION`, `V_OPS_PICK_LIST`, `V_OPS_SHIPMENTS`,
   `V_OPS_FF_STOCK`, `V_OPS_SETTINGS`, `V_OPS_FRESHNESS`). The contract between BigQuery and Sheets is explicit, and
   Claude answers from exactly the numbers the owner sees.
3. **Sheets is never a source of truth.** Uncommitted drafts may live in cells; anything committed lives in BigQuery and
   is re-read.
4. **Read-only towards marketplaces.** Nothing in this design creates or edits a WB supply or Ozon order. (This keeps the
   F‑18 gate intact — the new component writes only to internal BigQuery.)

### 3.1 BigQuery target objects

| Object | Kind | New / existing | Purpose |
|---|---|---|---|
| `REF_SKU_LOGISTICS` | table, versioned (`valid_from/to`) | new | Per card × channel: `units_per_box`, `unit_weight_kg`, `box_weight_kg`, `min_shipment_units`, `shipment_multiple`, `lead_time_days`, `target_cover_days`, `safety_days`, `fbs_reserve_units`, `assembly_policy` (`ON_SHIPMENT` / `PREASSEMBLED`), `max_lot_weight_kg` |
| `REF_BUNDLE_COMPONENTS` | table | existing | BOM (unchanged) |
| `CT_STOCK_MOVEMENT` | table, append-only | new (design) | Component grain + `container_sku`; states in §5 |
| `CT_SHIPMENT`, `CT_SHIPMENT_LINE` | tables | new | Header: `shipment_id`, `channel`, `counterparty`, `destination`, `marketplace_ref` (WB `supply_id` / Ozon `order_id`), `planned_date`, `status`, `boxes`, `weight_kg`, `request_id`, audit. Line: `card_sku`, `qty_planned / reserved / assembled / shipped`, API `qty_accepted` (derived) |
| `CT_BUNDLE_BUILD` | table | new | `build_id`, `bundle_sku`, `qty`, `purpose`, `shipment_id`, `status` |
| `CT_OPS_REQUEST_LOG` | table, append-only | new | Every Sheets call: `request_id`, procedure, payload hash, result, `changed_by` — idempotency + audit |
| `CT_B2B_ORDER` | table | new (design, stage H) | Commercial document for B2B |
| `V_CT_STOCK_STATE` | view | new (design) | `internal_sku × state × location × channel × container × doc` |
| `V_OPS_*` | views | new | Tab contracts |
| `sp_ct_stock_move` | procedure | new (design) | Internal: one movement, all checks |
| `sp_ops_shipment_create / _advance / _cancel`, `sp_ops_build_confirm`, `sp_ops_pick_confirm`, `sp_ops_settings_upsert`, `sp_ops_ff_count` | procedures | new | The only entry points Sheets may call |
| `sp_ct_refresh_daily` | procedure | existing | + step `stock_state` (compute, no journal writes) |
| `V_CT_SUPPLY_NEED`, `V_CT_BUNDLE_STATUS`, `CT_OWNER_ACTION_QUEUE` | existing | modify later | See matrix §13 |

---

## 4. Google Sheets workbook «EVETIS OPERATIONS»

Separate spreadsheet (like «Юнитка_Evetis Cosmetics»), with its own bound Apps Script project. Russian labels
(project rule). Creating it requires the owner's explicit permission — stage C.

### 4.1 Visual language (reuse `UnitkaR75.gs`)

| Element | Rule |
|---|---|
| Frozen | header rows + first two columns (SKU, name) |
| Colors | critical `#fce8e6` / `#a61c00`, warning `#fff2cc` / `#7f6000`, OK `#e6f4ea` / `#274e13`, neutral `#efefef` / `#434343` |
| Input cells | pale blue fill, blue font, framed — the only editable cells; everything else protected (warning mode) |
| Header notes | on BigQuery-fed columns: «Пишет BigQuery. Руками не вводить.» |
| Numbers | units as integers, ₽ with thin-space thousands, days with one decimal only where it changes a decision |
| Status banner | row 1 of every tab: last refresh · source freshness · stale warning (§11) |

### 4.2 `01_SUPPLY_PLAN` — main owner workspace

Three stacked sections, top to bottom = the order of work on the floor: **plan → build → pick**.
Each section is split by channel: **Wildberries**, **Ozon**, later **B2B / Golden Apple**.

**Section A · План поставок** (grain: card × channel; bundles are cards too)

| Col | Label | Source | Type |
|---|---|---|---|
| A | SKU | `V_OPS_SUPPLY_PLAN` | read-only |
| B | Товар | | read-only |
| C | Соло / набор | | read-only |
| D | На площадке, шт | API | read-only |
| E | В пути, шт | API ∪ journal (dedup per shipment) | read-only |
| F | Резерв и отгружено (не подтв.), шт | journal | read-only |
| G | Продажи в день, факт 30 дн | actuals | read-only |
| H | Продажи в день, план | active plan | read-only |
| I | Покрытие, дн | (D+E+F) / max(H, G) | read-only |
| J | Цель, дн | settings | read-only (edited on 04) |
| K | Срок поставки, дн | settings | read-only |
| L | Потребность (расчёт), шт | `(J + K) × rate − (D+E+F)` | read-only |
| M | Кратность / в коробе | settings | read-only |
| N | Мин. партия | settings | read-only |
| O | **Рекомендация, шт** | L rounded **up** to M; 0 if `< N` unless cover < K (then N) | read-only |
| P | Коробов | O / units_per_box | read-only |
| Q | Вес, кг | | read-only |
| R | Свободно на ФФ (после всех резервов), шт | global allocation (§12) | read-only |
| S | Покрытие после отгрузки, дн | display formula on T | display |
| T | **Кол-во (правка)** | owner | **input** |
| U | **Утвердить ☐** | owner | **input** |
| V | Комментарий | owner | **input** |
| W | Результат | script | written by commit: `ЧЕРНОВИК` · `ПРОВЕДЕНО SH-0012` · `ОТКАЗ: <reason>` |

Section footer: totals per channel — units, boxes, weight, share of `max_lot_weight_kg` (WB 25 kg lots).

**Section B · Сборка наборов** (grain: bundle)

Набор · нужно WB · нужно Ozon · резерв FBS (цель) · уже собрано (FBS-склад) · уже в документах (зарезервировано под
отгрузки) · **собрать сейчас** · ограничивающий компонент · можно собрать из свободного · [input] **Собрать, шт** ·
[input] **Подтвердить сборку ☐**. Below it — BOM decomposition: for each bundle to build, components × qty to take
from the FF.

**Section C · Снять с хранения** (grain: component)

Компонент · под соло-отгрузки · в наборы · под FBS · **итого нужно** · на полке · на паллетах · **снять с паллет** ·
после снятия на полке · [input] **Снято фактически** · [input] **Подтвердить ☐**.
Physical conservation shown in the section header: `нужно ≤ полка + паллеты` per component, red otherwise.

### 4.3 `02_SHIPMENTS` — shipment register

One row per shipment line, grouped and banded by shipment:

ID · Канал · Склад назначения · Создана · Зарезервировано · Собрано · Отгружено · Принято (API) · Расхождение ·
Статус · Ссылка WB/Ozon · Проверка API (`СОВПАЛО` / `НЕДОПРИНЯТО` / `НЕТ В API > 2 дн` / `В API БЕЗ ОТГРУЗКИ`) ·
[input] **Следующий шаг ▼** (only transitions legal from the current status) · [input] **Факт, шт** ·
[input] **Номер поставки / документа** · Результат (script).

### 4.4 `03_FF_STOCK` — fulfilment state (read-only except recount)

Компонент · всего на ФФ · паллеты · полка · **свободно** · резерв WB · резерв Ozon · резерв B2B · резерв FBS ·
в собранных наборах (по наборам) · FBS готово · отгружено, не подтверждено · расхождение с пересчётом ·
[input] **Пересчёт ФФ, шт** + дата → `sp_ops_ff_count` (adjustment movement with reason).

### 4.5 `04_SETTINGS`

Editable table keyed by SKU × channel (the columns of `REF_SKU_LOGISTICS`), a "Сохранить" action that writes a new
version, and a read-only BOM view from `REF_BUNDLE_COMPONENTS` (BOM edits stay a controlled BigQuery change).

### 4.6 `DATA` (hidden)

Raw query output, one block per `V_OPS_*` view, plus a metadata block (refresh time, CT refresh run id, fingerprint).
**Refresh rewrites only this tab.** Visible tabs read it with lookups keyed by SKU / shipment id, so a refresh can
never overwrite an input cell.

---

## 5. Read / write responsibilities

| Area | Who writes | How |
|---|---|---|
| Marketplace stock, in transit, velocity, plan, cover, need, recommendation, FF free stock, reservations, BOM, shipment API status | **BigQuery** | `DATA` tab via refresh → lookups; protected ranges |
| Qty override, approve, comment (01) | **Owner** | local cells until commit |
| Build qty / confirm build (01‑B), picked qty / confirm pick (01‑C) | **Owner or FF** | commit → `sp_ops_build_confirm`, `sp_ops_pick_confirm` |
| Next step, fact qty, document number (02) | **Owner or FF** | commit → `sp_ops_shipment_advance` |
| FF recount (03) | **Owner** | `sp_ops_ff_count` |
| Settings (04) | **Owner** | `sp_ops_settings_upsert` (new version) |
| Result column | **Script** | writes the procedure's answer; never a number that BigQuery owns |

### 5.1 Write-back protocol

1. Owner fills inputs and ticks «Утвердить».
2. Menu **EVETIS → Провести** (or a button) collects the ticked rows of one channel, assigns a `request_id`.
3. Script calls `sp_ops_shipment_create(payload_json, request_id)` via `BigQuery.Jobs.query` with parameters — the
   pattern already proven in `CtOwnerActions.gs` and `UnitkaR7.gs`. Runs as the owner's Google account: no keys, audit
   shows the owner's email.
4. Procedure: re-reads state, runs every gate (§12), writes header + lines + `AVAILABLE → RESERVED` movements in **one
   transaction**, logs `CT_OPS_REQUEST_LOG`. Any failure → nothing written, a readable Russian reason returned.
5. Script refreshes `DATA`, writes `ПРОВЕДЕНО SH-…` or `ОТКАЗ: …` into «Результат», clears the tick.
6. Same `request_id` twice → the procedure returns the original result (idempotent).

Rejected alternative (kept from Phase 1.1 analysis): Sheet as a BigQuery external table — needs Drive scope for SAs,
no validation at input, format easily broken, and makes the sheet a second truth.

---

## 6. Stock movement lifecycle

### 6.1 Shipment status (header)

```
DRAFT ──► RESERVED ──► ASSEMBLED* ──► SHIPPED ──► IN_TRANSIT† ──► ACCEPTED† ──► CLOSED
  │           │            │                                         │
  └───────────┴────────────┴──► CANCELLED (releases reservations)    └──► DISCREPANCY ──► CLOSED (after resolution)
  * only if the shipment contains bundles      † derived from API, never set by hand
```

### 6.2 Unit states (journal, component grain)

| State | Where | Enters from | Leaves to |
|---|---|---|---|
| `AVAILABLE(PALLET)` | FF | inbound, recount | `AVAILABLE(SHELF)` (pick), `WRITTEN_OFF` |
| `AVAILABLE(SHELF)` | FF | pick, cancel, disassembly | `RESERVED_*`, `ASSEMBLED`, `FBS_READY`, `WRITTEN_OFF` |
| `RESERVED_WB / _OZON / _B2B` | FF | shipment create | `ASSEMBLED` (bundle lines), `SHIPPED`, `AVAILABLE` (cancel) |
| `ASSEMBLED` (container = bundle; purpose FBO / FBS) | FF | build confirm | `SHIPPED` (FBO), `FBS_READY` (FBS), `AVAILABLE` (disassembly) |
| `FBS_READY` (solo or container) | FF | FBS allocation / FBS build | `SOLD_FBS` (sales report), `AVAILABLE` |
| `SHIPPED` | pipeline | advance with fact qty + marketplace ref | leaves the pipeline when API is final (derived) |
| `DISCREPANCY` | open item | derived: shipped − accepted on a final shipment | resolution movement: `LOST_CLAIM`, `RETURNED_TO_FF`, `FOUND_LATER` |
| `WRITTEN_OFF`, `LOST_CLAIM` | terminal | — | — |

Marketplace states `IN_TRANSIT`, `ON_MARKETPLACE`, `SOLD` come **only** from API.

### 6.3 Invariants (tests, fail-closed)

- **I‑1 FF balance:** `ff_total = AVAILABLE(P+S) + RESERVED_* + ASSEMBLED + FBS_READY` per component.
- **I‑2 No negative balance:** `sp_ct_stock_move` refuses `qty > balance(from_state)` → a unit cannot leave a state it is not in, so it cannot be in two.
- **I‑3 Bundle integrity:** for every container × state × document, units of each component = `k × component_qty` with the same `k`.
- **I‑4 Pipeline single source:** per shipment, pipeline qty = API qty if the API sees it, else journal SHIPPED — never both.
- **I‑5 Season conservation:** opening + inbound − (sold + written off + lost) = FF + pipeline + marketplace.
- **I‑6 Marketplace stock is API-only.** The journal never writes it.

---

## 7. Bundle lifecycle

| Quantity | Definition | Source |
|---|---|---|
| Theoretical capacity | per bundle, `min_c floor(AVAILABLE_free_c / qty_c)` — flagged «общие компоненты» because bundles share components and capacities are **not additive** | `V_OPS_BUNDLE_PRODUCTION` |
| Planned production | WB bundle need + Ozon bundle need + (FBS target − FBS bundles on hand) | supply plan + settings |
| Reserved components | `RESERVED_*` movements whose document is a build or a shipment line with a bundle | journal |
| Physically assembled | `ASSEMBLED` units / `component_qty`, by purpose | journal |
| FBS-ready bundles | `ASSEMBLED` with purpose FBS → `FBS_READY` | journal |

- **FBO:** build order is created **with** the shipment (`purpose = FBO_SHIPMENT`, `shipment_id` set); assembled bundles
  go straight to SHIPPED. Assembled FBO bundles older than N days without shipping → warning (they are tying up components).
- **FBS:** build order `purpose = FBS_STOCK`; bundles are standing inventory in `FBS_READY` until an FBS sale.
- Assembly policy per card × channel comes from `REF_SKU_LOGISTICS.assembly_policy` — today everything is `FF_ASSEMBLED`,
  which cannot express the difference.

---

## 8. WB / Ozon reconciliation

| Moment | Manual | API verifies later |
|---|---|---|
| Reservation | owner approves in 01 | Ozon `READY_TO_SUPPLY` order exists (evidence) |
| Assembly | FF / owner confirms build | — |
| Pick pallet → shelf | FF / owner confirms | — |
| Physical shipment | FF / owner: fact qty + **marketplace ref** (WB `supply_id` / Ozon `order_id`) | WB supply with warehouse + date; Ozon `ACCEPTANCE_*` |
| Acceptance | — | WB `is_completed` + `accepted_quantity`; Ozon `COMPLETED` + `quantity_accepted` |

Rules:

1. **Linking** is explicit: the ref is entered at SHIPPED. The view proposes candidates (same channel, date window,
   SKU set) for the owner to confirm — never a silent auto-link.
2. **No double count of open Ozon orders:** a `READY_TO_SUPPLY` order is reservation evidence, not pipeline. If FF has
   not marked the reservation, the view shows `резерв без отметки ФФ` and **subtracts it from free stock** (conservative),
   in a separate bucket so I‑1 still holds.
3. **Under-acceptance** (WB accepted 1 210 of 1 350): shipment → DISCREPANCY, 140 units open on 02 in red until the owner
   records `LOST_CLAIM` (claim to WB), `RETURNED_TO_FF` (recount) or `FOUND_LATER`.
4. **Over-acceptance** (API > shipped): data-error signal; nothing is auto-increased.
5. **In API without a shipment** (e.g. created in the WB cabinet directly): signal «в API без отгрузки»; owner links it
   to a shipment or creates one retroactively.
6. **WB drafts** (`status_id = 1`, no warehouse) are ignored as evidence.
7. **UNKNOWN before coding:** WB `status_id` semantics (1/3/5) must be confirmed against WB documentation.

---

## 9. Golden Apple / B2B (design only)

Three entities, no special cases:

| Entity | Object | What it carries |
|---|---|---|
| Commercial document | `CT_B2B_ORDER` (design §4.2) | counterparty, lines, price, discounts / retro bonuses (negative lines), payment terms, invoice / paid dates |
| Physical flow | `CT_SHIPMENT` with `channel = 'B2B'`, `counterparty` | reservation → assembly → SHIPPED reduces FF at shipment; acceptance = УПД signed (manual) |
| Economics | `V_CT_B2B_LEDGER` → UNION branch into actuals, `economics_basis = 'B2B_MANUAL'` | revenue, COGS from `V_PRODUCT_COGS_EFFECTIVE`, our logistics, contribution by the shared formula, receivable = invoiced − paid |

B2B stays **outside the marketplace plan** until a plan version adds a B2B channel. Sell-out and returns after 6–12 months
are later tables (`CT_B2B_SELLOUT`, return movements) — nothing in the core changes.

---

## 10. Metabase simplification

| Dashboard | Recommendation | Detail |
|---|---|---|
| **2** WB Executive | **Keep** | Financial BI |
| **3** WB SKU Performance | **Keep** | Commercial BI |
| **4** Search Queries | **Keep** | Advertising BI |
| **6** Sales Plan | **Keep** | Plan vs fact, channels, SKU control — pure commercial analytics |
| **5** Owner Home | **Simplify** (after Sheets go-live) | Keep: KPI row, plan of the month, risks, hand-cream control, stock-in-money. Remove: B «Требует решения» and C «Исполнить сегодня» → one tile «Операции → EVETIS OPERATIONS» |
| **8** Details | **Simplify** | Keep: freshness, refresh log, full SKU × channel table. Archive: shipments, bundles, action history cards |
| **9** Daily Brief | **Repurpose** | Commercial brief only (yesterday · plan · deviations · data). Execution sections move to Sheets; Claude remains the conversational brief |

Nothing is deleted. Cards are archived (reversible) only after two weeks of parallel run.

---

## 11. Data freshness

| Layer | Cadence (MSK) | Depends on |
|---|---|---|
| `wb-mart-prod` | 07:00 · 09:00 · 12:00 · 16:00 | Apps Script RAW loads |
| `ct-refresh-prod` (to be applied) | 07:40 · 09:40 · 12:40 · 16:40 · 19:40 | MART D‑1 COMPLETE (freshness gate) |
| Sheets `DATA` refresh (time trigger) | 08:00 · 10:00 · 13:00 · 17:00 · 20:00 + after every commit + menu «Обновить» | latest OK run in `CT_REFRESH_LOG` |

Banner on every tab: «Обновлено 10:00 · Control Tower 09:40 ● · WB остатки ● · Ozon ● · ФФ журнал ●».
Red if `DATA` older than 3 h in working hours, CT refresh not OK, or any source STALE; yellow if a source is one day behind.
**Commit is blocked while the banner is red** — stale data must not produce a reservation.

INFERENCE on the mechanism: Apps Script time trigger in the bound project, `BigQuery.Jobs.query` into `DATA`
(proven in this project). Connected Sheets is an alternative but **UNKNOWN** for a consumer `@gmail.com` account — verify
before relying on it. Quota is not a concern: 5 refreshes × ~30 s ≈ 3 min/day against 90 min/day.

---

## 12. Conservation and safety gates (inside `sp_ops_*`, fail-closed)

| Gate | Check | Failure message (Russian, to «Результат») |
|---|---|---|
| G1 Free stock | per component: requested (BOM-expanded) ≤ `AVAILABLE(P+S) − all open reservations − API-evidenced unconfirmed reservations` | «Не хватает свободного: <SKU> нужно N, свободно M» |
| G2 Global allocation | Σ reservations across **all** channels and documents ≤ available — one pass, not per-channel caps | «Резервы превысят остаток по <SKU>» |
| G3 BOM completeness | every component of every bundle line available | «Набор <X>: не хватает <компонент>» |
| G4 Box / min | `qty % shipment_multiple = 0` and `qty ≥ min_shipment_units`, unless override with a mandatory comment | «Не кратно коробу (M шт)» |
| G5 No double reservation | `request_id` unique; same line in an open shipment → explicit confirmation required | «Уже зарезервировано в SH-…» |
| G6 Non-negative result | FF balance after the operation ≥ 0 for every component | «Остаток ФФ станет отрицательным» |
| G7 Resulting cover | computed and **stored** with the shipment (audit); warning if above `target + 30 d` | informational |
| G8 Freshness | data behind the decision is fresh (§11) | «Данные устарели — обновите» |
| G9 Weight | total ≤ `max_lot_weight_kg` × lots, where the channel has a lot limit | «Превышен лимит лота» |
| G10 Legal transition | shipment / unit transition in the white list | «Шаг недоступен из статуса …» |

---

## 13. Reuse vs retire matrix

| Object / component | Current purpose | Target purpose | Decision | Reason |
|---|---|---|---|---|
| `CT_PLAN_VERSION`, `CT_SEASON_PLAN(_MONTHLY)`, `CT_DAILY_CURVE`, `V_CT_PLAN_ACTIVE` | Versioned season plan | Planned daily sales for supply plan and Metabase | **KEEP** | Correct and needed by both |
| `CT_ACTUAL_DAILY`, `V_CT_ACTUAL_DAILY(_LIVE)`, `V_CT_PLAN_VS_ACTUAL_DAILY`, `V_CT_SKU_CONTROL` | Plan vs fact | Same (Metabase, Claude) | **KEEP** | Commercial BI |
| `V_CT_CASH_CONVERSION`, `CT_OPEX`, `CT_EXPIRY_BATCH`, `V_CT_HAND_CREAM_CONTROL` | Money & expiry | Same | **KEEP** | BI |
| `V_CT_BOM_CURRENT` | BOM | BOM for all ops views | **KEEP** | Single BOM truth |
| `V_CT_INVENTORY_TRUTH(_LIVE)`, `CT_INVENTORY_SNAPSHOT_DAILY`, `V_CT_SUPPLY_FLOW` | Inventory truth | API half of stock state | **MODIFY** (additive columns) | FF half moves to the journal |
| `CT_STOCK_SNAPSHOT` | Manual FF seed | One-time journal seed, then history | **DEPRECATE** after seed | Replaced by `CT_STOCK_MOVEMENT` |
| `V_CT_SUPPLY_NEED` | What to ship | Superseded by `V_OPS_SUPPLY_PLAN` | **DEPRECATE** after parallel run | No rounding / lead time / global allocation |
| `V_CT_BUNDLE_STATUS` | Bundle status | Commercial part stays; ops part → `V_OPS_BUNDLE_PRODUCTION` | **MODIFY** | `assemblable_now_ff` uses total, not free |
| `CT_BUNDLE_PLAN` | Weekly build plan (seed) | Input for FBS targets / planned production | **MODIFY** | Execution goes to `CT_BUNDLE_BUILD` |
| `CT_OWNER_ACTION_QUEUE`, `CT_ACTION_STATUS_LOG`, `sp_ct_action_update` | All actions | Decision actions only | **MODIFY** | Execution actions become shipment / build / pick states |
| `sp_ct_generate_actions`, `V_CT_ACTION_CANDIDATES` | Rules → actions | Decision + signal rules; execution rules read shipment state | **MODIFY** | Avoid a second workflow |
| `V_CT_OWNER_HOME`, `V_CT_ATTENTION` | KPI row, alerts | Same + ops signals (discrepancy, stuck reservation) | **KEEP / MODIFY** | |
| `V_CT_DAILY_BRIEF(_LINES)` | Brief text | Commercial brief; execution lines point to Sheets | **MODIFY** | |
| `V_CT_FRESHNESS`, `V_CT_REFRESH_STATUS`, `CT_REFRESH_LOG` | Freshness | Sheets banner + Metabase | **KEEP** | |
| `sp_ct_refresh_daily` | Refresh | Same + `stock_state` compute step | **KEEP** | |
| Stock-state proposal (`CT_STOCK_MOVEMENT`, `sp_ct_stock_move`, `V_CT_STOCK_STATE`) | Design | Backend truth for FF states | **KEEP** + R1–R7 | §2 |
| Metabase 2, 3, 4, 6 | BI | BI | **KEEP** | |
| Metabase 5 Owner Home | KPI + execution | KPI only | **MODIFY** | |
| Metabase 8 Details | Everything else | Freshness + SKU table | **MODIFY** (archive cards) | |
| Metabase 9 Daily Brief | Brief + execution | Commercial brief | **MODIFY** | |
| `CtOwnerActions.gs` web-app | Status links from Metabase | — | **DEPRECATE** (never deployed); **reuse code** | Sheets-bound script replaces it |
| `ct_refresh.tf` | Auto-refresh (not applied) | Heartbeat for Sheets | **KEEP — apply** | Prerequisite |
| Supply execution, pallet picking, bundle orders, reservations, task workflow | Metabase lists | «EVETIS OPERATIONS» | **MOVE TO SHEETS** | |

---

## 14. Migration plan (no big bang)

| Stage | Content | Production impact | Rollback | Acceptance |
|---|---|---|---|---|
| **0 · Prerequisites** | Owner decision on the open Stage B window (finish C–J→K, or resume `wb-mart` in the validated Phase B state); re-run today's ads bids snapshot; push CT commits to origin; apply `ct_refresh.tf` | Restores MART and CT freshness | Pause again / `terraform destroy -target` the CT scheduler | MART D‑1 COMPLETE; `CT_REFRESH_LOG` OK within 1 h of each slot; `origin/main` contains `3c29231` |
| **A · Freeze architecture** | ACK on this review + a short design delta (R1–R7, `REF_SKU_LOGISTICS`, shipment & build entities, `V_OPS_*` contracts) | none | — | Owner ACK |
| **B · Stock-state backend** | New tables / procedures / views, seed from 09.09 snapshot with Ozon reclassification; **no consumer yet** | Additive BigQuery objects only | DROP the new objects; nothing existing changed | I‑1…I‑6 pass; seed = snapshot; 235 cards in `RESERVED_OZON`; reversal works; illegal transition rejected |
| **C · Sheets read-only** | Create workbook (explicit permission), `DATA` refresh, tabs 01–04 read-only, banner | New spreadsheet; no writes to BigQuery | Stop trigger, archive spreadsheet | Every visible number = its `V_OPS_*` row; refresh leaves inputs untouched; stale banner fires when forced |
| **D · Validate vs Control Tower** | 1–2 weeks parallel: `V_OPS_SUPPLY_PLAN` vs `V_CT_SUPPLY_NEED`, `V_OPS_FF_STOCK` vs `V_CT_INVENTORY_TRUTH` | none | — | Every difference explained (rounding, lead time, allocation, reservations); FF totals equal |
| **E · Write-back** | `sp_ops_*`, commit menu, idempotency, gates | Writes to new ops tables only | Remove menu; reverse movements; DROP procedures | G1–G10 each proven by a failing and a passing case; double-click test; stale-data block test |
| **F · Move workflow** | Execution actions derived from shipment/build state; Metabase execution blocks hidden; brief repurposed | Changes CT rules and cards | Previous views from Git; Metabase snapshot | No execution action without a shipment/build; owner runs a real week in Sheets |
| **G · Simplify Metabase** | Archive superseded cards after 2 weeks of non-use | Metabase only | Unarchive | Dashboards 2/3/4/6 untouched; 5/8/9 match §10 |
| **H · B2B** | `CT_B2B_ORDER`, B2B channel in shipments, ledger, actuals branch | Additive | DROP | A test order flows reservation → shipment → invoice → payment; contribution by the shared formula; marketplace plan unchanged |

---

## 15. Main risks

1. **Human discipline is the real system.** FF has no API; if movements are not recorded, the journal drifts. Mitigation:
   only three recording moments (reserve, ship, confirm build/pick), recount entry, drift and «нет в API > 2 дн» signals.
2. **Confident stale numbers.** Today MART and CT are both stale (§0). Mitigation: banner + commit blocked on stale.
3. **Sheets becoming a second truth.** Mitigation: `DATA`-only refresh, protected ranges, commit-or-nothing, results re-read.
4. **Shared components across bundles** make capacity non-additive. Mitigation: one global allocation (G2), never per-bundle.
5. **Apps Script runs as the owner.** Revoked consent or a changed password stops refresh silently. Mitigation: the banner
   and an ops-health check on the `DATA` timestamp.
6. **WB supply semantics unverified** (`status_id`). Mitigation: confirm against WB docs before stage E.
7. **Seed correctness** (369 → 235 reserved + 114 pipeline). Mitigation: explicit seed test.
8. **Source concentration.** CT code is not on GitHub (§0 P4).
9. **Scope creep toward marketplace writes.** This design must stay read-only towards WB/Ozon; any "create supply via
   API" idea is blocked by F‑18.

---

## 16. Recommended next implementation stage

**Stage 0 first** — it is small, and nothing downstream is trustworthy until it is done:
the Stage B window decision (MART is 29 h stale), today's ads bids re-run, pushing the CT commits, and applying
`ct_refresh.tf`.

Then **stage A (design delta, one short document) → stage B (stock-state backend, BigQuery only, additive)**.
The Sheets workbook starts only after B passes its invariants, and starts read-only.
