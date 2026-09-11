# EVETIS OPERATIONS — Architecture Delta v1

**2026-09-11 · Status: FOR OWNER APPROVAL · Nothing implemented.** Revision 2 (after Stage 0 closure): §3 BOM expansion, §4 physical-unit rule, §8 blockers.
Supersedes the front-end part of `CT_STOCK_STATES_DESIGN_2026-09-11.md`; keeps its backend. Evidence and full specs:
`OPS_ARCHITECTURE_REVIEW_2026-09-11.md`.

## 1. Approved model

| Layer | Role | Never |
|---|---|---|
| **BigQuery** | Truth and all calculations: marketplace stock (API), FF states (journal), reservations, shipments, bundles, supply plan, BOM, conservation | — |
| **Google Sheets «EVETIS OPERATIONS»** | Operational workspace: supply plan, bundle build, pallet pick, shipment register, FF stock, settings. Reads one `V_OPS_*` view per tab; writes only by `CALL sp_ops_*` | a source of truth; a place where formulas decide |
| **Metabase** | BI: sales, revenue, ads, logistics, storage, commissions, penalties, contribution, trends. Keeps 2, 3, 4, 6; simplifies 5, 8, 9 | execution workflow |
| **Claude** | Conversation over the same `V_OPS_*` / `V_CT_*` | a second calculation |

Read-only towards WB and Ozon — nothing creates or edits a supply or an order (F‑18 unchanged).

## 2. Stock-state backend — kept, with refinements

Kept from the design: append-only `CT_STOCK_MOVEMENT` (reversal only), single write path `sp_ct_stock_move`
(whitelisted transitions, `qty ≤ balance`), `V_CT_STOCK_STATE`, "API never writes the journal".

| # | Refinement |
|---|---|
| R1 | **One grain:** component rows with `container_sku`; bundle quantity derived; bundle-integrity invariant. No separate bundle row. |
| R2 | **Shipment entity** `CT_SHIPMENT` + `CT_SHIPMENT_LINE` (channel, destination, WB `supply_id` / Ozon `order_id`, boxes, weight, status). |
| R3 | **Build order** `CT_BUNDLE_BUILD`, purpose `FBO_SHIPMENT` (assembled with the shipment) or `FBS_STOCK` (standing inventory). |
| R4 | **Logistics settings** `REF_SKU_LOGISTICS`, versioned: units per box, weights, min shipment, multiple, lead time, target cover, safety days, FBS reserve, assembly policy, lot weight limit. |
| R5 | **Hand-over derived from API:** a shipment leaves SHIPPED when the API is final; a shortfall is an open discrepancy until a human records its resolution. |
| R6 | **Seed correction** — §3. |
| R7 | **Atomic, idempotent write-back:** header + lines + movements in one BigQuery transaction; every call has a `request_id`. |
| R8 | **Placement (isolation rule):** new cross-channel operational objects live in a dedicated dataset `evetis_ops`, not in `wb_mart` / `evetis_ref`. `REF_SKU_LOGISTICS` is a reference and stays in `evetis_ref`. The existing `CT_*` (operational tables in `evetis_ref`) and `V_CT_*` (cross-channel views in `wb_mart` reading `ozon_raw`) are a **recorded pre-existing deviation**, not moved now; migration is a separate item. |

## 3. Seed correction — Ozon

The 09.09 snapshot records `in_transit_to_ozon_units = 369`. Open Ozon orders on 11.09, **BOM-expanded
through `V_CT_BOM_CURRENT`** (all lines mapped via `REF_SKU_CHANNEL_MAP`, 0 unresolved):

| State | Orders | Cards (selling units) | of which bundles | **Physical units** | Physically | Seed as |
|---|---|---|---|---|---|---|
| `READY_TO_SUPPLY` | 5 (created 07.09, pickup ≈17.09) | 235 | 47 | **314** | **on the FF shelf** | `RESERVED_OZON` per `order_id`, **314 component units** |
| `ACCEPTANCE_AT_STORAGE_WAREHOUSE` + `REPORTS_CONFIRMATION_AWAITING` | 3 | 114 | 20 | **150** | at Ozon | shipments in SHIPPED, pipeline counted **from API** |
| Total open | 8 | 349 | 67 | **464** | | |

**Reservations are always BOM-expanded.** A `READY_TO_SUPPLY` line of a bundle card reserves every component unit
of that bundle (`cards × component_qty`), not one "card". The manual 369 matches none of the derived figures
(349 cards, 314 or 464 physical units) and is retired. The 95-unit gap between 369 and 464 must be explained
**before** the ledger is opened; if it cannot be, stage B stops.

Seed test: FF total (physical) = pallet + shelf + 314 reserved component units; the first Ozon state change moves
no unit twice.

## 4. Rules that make it safe

- **Conservation is counted in physical units only** — one bottle / jar / tube = one unit. Cards, bundles, orders
  and shipment lines are selling units: they are converted through the BOM at the moment they touch stock
  (reservation, build, shipment, API evidence) and never added to physical balances directly.
- One global allocation of free FF stock across WB, Ozon, B2B, FBS and bundles (never per-channel caps).
- Recommendations rounded **up** to the box multiple and minimum shipment.
- Ten fail-closed gates at commit; commit blocked while data is stale.
- Pipeline counted from one source per shipment (API if visible, else journal).
- Sheets refresh rewrites only the hidden `DATA` tab; inputs are never overwritten.

## 5. Not in v1

WB FBS stock loader (not loaded today) · Golden Apple integration (B2B stays manual, stage H) · batch/FEFO picking ·
moving existing `CT_*` / `V_CT_*` (R8) · any Metabase deletion · any marketplace write.

## 6. Order of work after approval

0 production prerequisites → **B** backend in `evetis_ops` (additive, invariant-tested, no consumer) → **C** Sheets
read-only → **D** 1–2 weeks parallel with Control Tower → **E** write-back → **F** workflow moves → **G** Metabase
archive → **H** B2B. Each stage has rollback and acceptance tests in the review §14.

## 7. Decisions requested

1. Approve the model (§1) and R1–R8.
2. Approve the Ozon seed correction (§3).
3. Approve `evetis_ops` as the dataset for new operational objects (R8).
4. Confirm the refresh rule for «EVETIS OPERATIONS»: **automatic** read refresh of `DATA` — unlike the Unitka workbook,
   where autonomous writes run only manually. Owner inputs stay manual in both.

## 8. Blockers carried forward from Stage 0

| Blocker | Rule until resolved |
|---|---|
| **Production is ahead of `main`**: funnel-loader Terraform resources (`wb_funnel_loader.tf`, `secrets.tf` changes) were applied from a feature branch, and the loader image in production is built from `bbb4ec0` (`feat/unitka-2.0-funnel-reports-token`), which is not on `main` | **No plain `terraform apply` from `main`** (the plan would destroy the funnel resources) and **no `deploy-prod` from `main`** (it would roll the image back and remove the funnel loader). Only targeted applies with a reviewed plan. Lifted when the funnel work is merged into `main` through a PR and a full plan from `main` shows only the known cosmetic drift |
| Marketplace isolation (R8) | New operational objects go to `evetis_ops`, never extending `CT_*` in `evetis_ref` or `V_CT_*` in `wb_mart` |
| `runWbAdsDaily` time budget | Soft budget 5.3 min vs hard limit 6 min; on 11.09 the morning run died before closing the `ads` heartbeat and the manual re-run finished 18 s before the limit. Separate fix task |
