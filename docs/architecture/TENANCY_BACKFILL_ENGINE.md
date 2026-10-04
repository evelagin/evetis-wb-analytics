# Tenant Ozon backfill engine — WINDOW_V1

Design baseline: 2026-10-04, repository b50666b. This document is a contract/design;
code existence, tests and source captures do not prove deployed capability or full history.
Owner ACK authorizes engineering, exact-image client_001 deployment and bounded pilot only.
No Scheduler activation, destructive trial cleanup or automatic READY transition.

## Source contract matrix (before implementation)

All destinations below are in the configured tenant ozon_raw dataset. Keys preserve
existing RAW grain. Page caps protect completeness; they are not business thresholds.
History classifications describe available source shape, not verified tenant coverage.

| Domain | Exact endpoints | Grain / RAW destination / MERGE key | Source filter / pagination | History class and limit | Existing checkpoint / coverage |
|---|---|---|---|---|---|
| Catalog ALL | POST /v3/product/list; /v3/product/info/list | snapshot_date × sku; RAW_OZON_CATALOG; snapshot_date,sku | visibility=ALL excludes archived; last_id/total_items; info batches <=1000 | CURRENT_SNAPSHOT_ONLY; current product identity, not past status | Journal only; no dated historical coverage |
| Catalog ARCHIVED | Same | Same | visibility=ARCHIVED; separate traversal with stable product/SKU universe across both | CURRENT_SNAPSHOT_ONLY; retained archived identities, retention unproven | Same |
| FBO postings | POST /v3/posting/fbo/list | posting × SKU; RAW_OZON_POSTINGS_FBO; posting_number,sku | RFC3339 millisecond since/to, <=year; has_next/cursor; limit100 | HISTORICAL_BACKFILLABLE; earliest tenant date UNPROVEN | T5 date chunks/append-only checkpoint; no runtime leaf proof yet |
| Finance | POST /v1/finance/accrual/types; /v1/finance/accrual/by-day | accrual × type × nullable SKU; RAW_OZON_FINANCE_ACCRUAL; accrual_id,type_id,sku | date >=2022-01-01; last_id expires after15min | HISTORICAL_BACKFILLABLE; earliest activity UNPROVEN, late changes possible | T5 chunks; no per-day runtime proof yet |
| Prices | POST /v5/product/info/prices | snapshot_date × offer; RAW_OZON_PRICES; snapshot_date,offer_id | current snapshot; cursor/total | CURRENT_SNAPSHOT_ONLY; FORWARD_HISTORY_ONLY accumulation | No historical API coverage |
| Price commissions | Same | snapshot_date × offer × scheme × component; RAW_OZON_PRICE_COMMISSIONS; snapshot_date,offer_id,sale_scheme,commission_component | current snapshot alongside prices | CURRENT_SNAPSHOT_ONLY; FORWARD_HISTORY_ONLY accumulation | Same |
| Stocks | POST /v1/analytics/stocks | snapshot_date × SKU × warehouse; RAW_OZON_STOCKS; snapshot_date,sku,warehouse_id | <=100 SKUs per request; catalog list | CURRENT_SNAPSHOT_ONLY; FORWARD_HISTORY_ONLY accumulation | Same |
| Supply orders | POST /v3/supply-order/list; /v3/supply-order/get | order; RAW_OZON_SUPPLY_ORDERS; order_id | states, ORDER_CREATION DESC, last_id; no created-date filter; get<=50 | HISTORICAL_BACKFILLABLE retained orders/current state; first-ever boundary UNPROVEN | None; existing trial reached orders then timed out |
| Supplies | Same | order × supply; RAW_OZON_SUPPLIES; order_id,supply_id | nested in order detail | HISTORICAL_BACKFILLABLE retained objects/current state | None |
| Bundles/content | POST /v1/supply-order/bundle | bundle × SKU; RAW_OZON_SUPPLY_BUNDLES; bundle_id,sku | one bundle per traversal; limit100; has_next,last_id,total_count | HISTORICAL_BACKFILLABLE retained content; accepted quantity remains UNPROVEN | None; cursor checkpoint needed |
| Seller info | POST /v1/seller/info; /v1/rating/summary | snapshot_date; RAW_OZON_SELLER_INFO; snapshot_date | current only | CURRENT_SNAPSHOT_ONLY; no legal identifiers printed | Journal only |
| Clusters | POST /v1/cluster/list | snapshot_date × warehouse; RAW_OZON_CLUSTERS; snapshot_date,warehouse_id | current only | CURRENT_SNAPSHOT_ONLY | Journal only |
| Performance campaigns | GET /api/client/campaign | snapshot_date × campaign; RAW_OZON_ADS_CAMPAIGNS; snapshot_date,campaign_id | page/pageSize, total/list | CURRENT_SNAPSHOT_ONLY; deleted-campaign discoverability UNPROVEN | Journal only |
| Performance expense/statistics | GET /api/client/statistics/expense; /api/client/statistics/daily | date × campaign; RAW_OZON_ADS_EXPENSE_DAILY; date,campaign_id | dateFrom/dateTo; CSV; bounded day | HISTORICAL_BACKFILLABLE when source returns supported report; earliest retention UNPROVEN | Existing T5 dated chunks |
| Performance SKU statistics | POST /api/client/statistics; GET /api/client/statistics/{UUID}; /api/client/statistics/report | date × campaign × SKU; RAW_OZON_ADS_SKU_DAILY; date,campaign_id,sku | <=62days, <=10campaigns/report, one export concurrent; existing runtime60days | SOURCE_LIMITATION_UNPROVEN until bounded report and campaign cohort reconciled; CPC/SKU only | Existing T5 chunks; partial cohort must fail |

Seller source: official Swagger capture 2026-09-30, SHA256
93bbc82cab310c96be632464b4075996a9277404b3b910f8411cbac7500c0ddc,
https://docs.ozon.ru/api/seller/swagger.json. Capture is HISTORICAL, not live schema proof.
Performance source: official documentation https://docs.ozon.ru/api/performance,
T5 reviewed source capture 2026-09-28 and existing strict report contract. New query
variants require explicit safe transport review before use; GET alone does not imply read.

## History and economic finality

Owner evidence: supply history reaches at least2023-03-12; this is not an FBO/finance
boundary. Trial finance covers only2026-09-17..30. FBO trial exceeded200pages without
persisting RAW. Never use a populated RAW minimum as proof of source earliest history.
Existing history.py performs bounded source probes and records confidence/limitations;
a rejected earlier window is not EMPTY. Full discovery is separate from bounded pilot.
Snapshot domains record NO_HISTORY_AVAILABLE_FROM_SOURCE, not NOT_YET_BACKFILLED.
Catalog ALL+ARCHIVED provides an observed retained identity universe; it cannot guarantee
products that the source has deleted. Historical RAW snapshots are not rewritten to today's status.

Finance taxonomy descriptions come only from the source types endpoint. Missing84 or any
future code is retained as UNKNOWN and surfaces as a blocking taxonomy DQ result; source
labels are not automatically an approved P&L mapping. Completeness means fetched normally
as-of time, not final money. Existing30-day runtime lookback remains unchanged. Explicit
refresh generation re-observes old days idempotently; D3 finality delay requires owner input,
and no arbitrary new delay makes tenant READY.

## Reusable execution and state design

WINDOW_V1 is explicit opt-in for external tenants, strict paging and binding required.
Existing legacy collectors/lookbacks retain their source and key semantics. Journal insert
rejection now fails visibly for every caller; legacy deployment is not authorized by this
candidate. One entity per bounded execution. Pilot metadata requires VALIDATING or
CAPABILITY_DISCOVERY and a tenant-wide atomic lease; it cannot overlap full T5
BACKFILLING, READY or an active control execution. A terminal execution completion
marker permits the next CAS lease generation; a lost/ambiguous run remains locked. Inputs carry
exact scope, generation and checkpoint origin; tenant identifiers are never runtime branches.
FBO uses half-open millisecond UTC intervals derived from whole Moscow days. RAW order_date
remains source UTC date; coverage is Moscow-day coverage only when every leaf covers that day.
Adaptive bisection at the existing cap discards uncommitted parent rows; minimum window fails
visibly. Each completed leaf MERGEs then writes durable journal proof; crash before proof
replays MERGE. Backfill uses public QueryJob.dml_stats in pinned BigQuery3.25 to
account exact inserted/updated keys; no repeated whole-history COUNT scans. The
legacy caller keeps its previous accounting. Missing/contradictory DML stats fail. No premature coverage and no cap increase. Plan identity includes tenant,
entity, date scope, execution contract and refresh generation.

Supplies enumerate bounded order pages and detail batches, then bounded bundle pages;
continuation includes unresolved bundles/cursors. Each safe page can persist RAW, but the
order batch is complete only after its entire content traversal. Repeated/nonprogress
cursors, malformed terminal flags and contradictory totals fail closed. Source enumeration
is a current traversal: source changes during resume can require a fresh generation.

Additive evidence_json/backfill_plan_id/backfill_sequence on OZON_INGESTION_RUNS is the runtime proof transport; it contains
only scoped source progress/counts, not credential/identity payloads. Runtime reads typed MAX(sequence), then the exact clustered proof row to resume;
it never repeatedly scans full large JSON history. Only control/owner identity writes existing tenant_ops checkpoint,
coverage and history tables. No new runtime tenant_ops/IAM rights. Journal insert errors
are failures; an ACK without persisted evidence never means completion.

Pilot checkpoint namespace is disjoint from full T5 approved plan. Machine status mapping:
NOT_STARTED=PENDING; IN_PROGRESS=RUNNING; COMPLETE=DONE; source-unavailable/partial-limit
retain existing coverage NOT_AVAILABLE/PARTIAL plus explicit reason and evidence.
Pilot is owner-authorized while lifecycle is VALIDATING and does not change lifecycle,
create a full-history approval or activate automatic execution. Full backfill still requires
canonical T5 transitions, history boundaries, approved plan hash, leases, quota and DQ.

## Trial coexistence and validation

Trial rows are never deleted. Normal MERGE may replace provenance/current values on the
same natural key; this is existing upsert behavior. A source_payload_hash is often a key
hash, not a complete-content equality proof. Compare natural keys AND business columns,
excluding ingestion provenance, before/after. Trial OK records without WINDOW_V1 proof do
not satisfy new checkpoints. Historical snapshots remain on their actual observation date.

Offline/adversarial tests cover split/minimum/cursor failures, interruption/replay,
finance duplicate and unknown taxonomy, supply thousands/page continuation, archived
catalog identity, Performance cohort/limits, binding/project and transport security.
Applicable Ozon and Tenancy suites run on Python3.12, followed by CI, immutable build,
exact-image qualification, release record, reviewed additive Terraform plan, tenant-only
deploy/readback. Pilot requires bindingBOUND and pausedSchedulers before every execution.
No required check silently becomes optional. Failures/unknown evidence block completion.

## Boundary-authority safeguard

Pilot progress is not written as a pseudo earliest-history boundary: the existing T5
selector takes the latest HISTORY_BOUNDARIES row per entity and a pilot row could
silently shadow verified discovery. Existing bounded history.py discovery remains the
source of authoritative boundaries; pilot source intervals stay in checkpoint evidence.
Full-history approval and D3 maturity decisions remain separate.


## T5 integration and qualification gate

New external-tenant `claim-next` emits WINDOW_V1, exact project, binding/strict flags,
canonical approved plan hash as initial generation (an authorized REOPEN_CHUNK creates
an isolated deterministic refresh generation for that chunk), and the stable plan creation time
as journal origin. It stores the expected runtime plan with the existing RUNNING row.
`verify-chunks` requires matching scope and validated complete runtime proof for new
claims. IN_PROGRESS is neither DONE nor penalized failure; existing leases/visibility
still prevent early replacement. Previously recorded claims retain their legacy
interpretation and cannot satisfy a new WINDOW_V1 claim. Validated Moscow-day completion
uses explicit `done_windows_msk`; old UTC evidence keeps its conservative conversion.

The owner pilot coordinator rejects images without an exact matching built-artifact
WINDOW_V1 qualification and implementation hash (all19 application files). Terminal
receipt readback verifies job, image, runtime service account, critical overrides and
CAS lease/ACK provenance. A successful container exit is insufficient without journal
proof and source-to-persisted key reconciliation.

Performance report intents reserve the campaign export count durably before POST.
The existing conservative-floor T5 quota bounds own trailing24h reservations; unknown
ordinary exports block the calculation. Pilot-wide lease and no active control/runtime
execution protect account export concurrency. No report intent is created at an ordinary
unit/request deadline boundary. Ambiguous submission requires review rather than a
second POST. This budget cannot prove the absence of unrelated external-account writers.

No policy methods or credential profiles were added. New campaign GET pagination is
restricted to exact reviewed positive-integer page/pageSize parameters. Missing bundle
links are explicit source limitations; bundle traversal completion does not establish
accepted quantities. Source key duplication, absent stable product identity, changing
catalog totals/SKU ownership and checkpoint growth beyond900KB fail visibly, with no
premature COMPLETE. That bound is a safety limit, not evidence of unlimited tenant size.


## DoD target applicability — unresolved process gate

The current impact graph is scoped to legacy EVETIS, not deployed tenant targets. For
these runtime/schema changes it resolves49 objects and eight mandatory suites:
`ozon_unit`, `control_tower_phase1`, `control_tower_owner_screens`, `promotion_l3`,
`ubr012_revenue_source`, `promo_economics`, `inventory_context`, `ads_intel`.
Its37 downstream objects include `evetis_ops`, `evetis_mart` and legacy reference
contracts;32 objects lack registered contracts. This is the machine result, not a
waiver. client_001 has its own `ref`/tenant_ops/tenant project and this release is
not deployed to legacy EVETIS. Running those suites against the tenant cannot prove
legacy parity; probing foreign EVETIS would not prove tenant pilot acceptance either.

Under AGENTS/Definition of Done, retain BLOCKED for target-specific parity/data/health/
regression evidence until the owner resolves applicability or approves a documented
bounded exception. Do not merge/deploy solely because offline CI is green. Candidate
code, offline fixtures, PR and CI may be prepared independently. No framework gates,
registries or suite thresholds have been disabled or redefined by this candidate.

Proposed bounded substitution for owner review: exact-image security/portability tests;
reviewed additive client_001-only Terraform plan; live schema/template/binding parity;
per-domain source terminal/window proofs; exact persisted natural-key counts and current
retained SKU linkage; run/checkpoint/coverage readback; bounded trial before/after business
column reconciliation; no foreign-project write, Scheduler/IAM/secret/binding mutation;
full-history/financial-finality/READY remain UNPROVEN until their own contracts pass.
This proposal is not an approved exception and creates no production authorization.


D3 refresh: normal incremental finance preserves its existing30-day lookback; older
corrections require an explicit refresh generation or canonical owner REOPEN_CHUNK.
The latter changes only that chunk's WINDOW_V1 plan identity, so a prior complete proof
cannot incorrectly suppress a requested source re-observation. Ordinary retries retain
one stable generation. No new monetary finality delay/automatic repair is invented.
