# Incident-only WB Ads repair: 2026-09-30

Status: local implementation, not deployed or activated. Owner ACK: attachment
`9b2eae72-b451-41a7-8c14-a9a6a69f4d74`, LOCAL IMPLEMENTATION ONLY.
This contract describes this incident implementation; it does not approve execution.

## Interface and isolation

`runWbAdsRepair20260930({logicalDate:'2026-09-30', campaignHash, effectiveViewHash})`.
No other arguments, date, date range, population subset or allowlist is accepted.
Both hashes are required lowercase SHA-256 values established by a future authorized
read-only preflight and separately approved for execution. No placeholder is executable.

* `campaignHash`: SHA-256 of `adsDayCanonical_` applied to the complete array of
  `{advertId: STRING, status: STRING}`, sorted by numeric advertId ascending. This is
  the entire origin snapshot, not 9 controls or an inferred 31-ID tail.
* `effectiveViewHash`: SHA-256 of exact `BigQuery.Tables.get(...,
  'V_ADV_CAMPAIGN_STATS').view.query` bytes. Independent review must establish the
  effective view's canonical grain/filter/tie-breaker before approving this hash.
  The loader does not accept a different definition during execution.

Default OFF: Script Property `WB_ADS_REPAIR_20260930_ENABLED` must equal `1`.
The implementation never sets/deletes properties or installs triggers. It takes the
existing ScriptLock and rejects an active daily context or enabled resume mechanism.
The runtime is independent of WbAdsResume.gs/Runtime.gs and does not route through
that generalized recovery mechanism. No recovery table, checkpoint, continuation or
schema deployment is required. Ordinary daily behavior is unchanged.

Runtime prerequisites: exact project `project-fa311fc0-4d87-4781-986`, dataset
`wb_raw`, location EU, existing BQ sink enabled, exact existing RAW STRING schemas,
existing ingest journal schema, and existing WB_ADS_STATUS with exact 13 headers.
No schema creation/patch, dataset creation, RAW Sheet fallback or header repair.
Future deployment must establish actual script/container/runtime identity and source
parity, and exclude every concurrent Ads writer, including writers that do not use
ScriptLock. Local code and test PASS do not prove deployed state or source authority.

## Reused persisted evidence

Origin: `ADSRAW_20261001_050732_749`.
Failed parent: `INS_ADS_20261001050734_5253f5ba` (read only).
Require parent logical date 30.09, ads/apps_script, ERROR/ADS_PARTIAL, original
campaigns OK(431) and costs OK(74) summary. Require 431 unique snapshot IDs with
statuses 9:1, 11:19, 7:411; campaign source `promotion/count+adverts/v2`, raw/count_only,
and load timestamps within the parent's execution (Moscow RAW timestamps to UTC).
The nine controls must belong to the full approved snapshot/hash.

Require exactly one committed costs window from that origin: index 0, 17–30.09,
source adv/v1/upd, OK/http_success=true, returned_rows=74, rows_out_of_window=0,
commit timestamp inside parent execution, actual RAW window count=74.
Require canonical day coverage selecting that origin/window, requested_ok=true,
not_loaded=false, 6 rows/1737 RUB, and canonical billing campaign aggregates:
37563883: 1/12; 37727911: 1/323; 37727969: 1/251; 37727999: 2/688;
38085124: 1/463 (row count/RUB). These are validation evidence, not fullstats values.
Snapshot, parent, costs marker/count, canonical billing and coverage are hashed and
re-read unchanged before COMPLETE. No synthetic campaigns/costs source results.

Maturity flags are reported as observed; age_ok/stable_ok/billed_complete=false
are not completion blockers here. The implementation does not assert financial
finality, alter billing or reproduce those flags as fabricated true values.

## Preparation, population and source checks

Process all 431 IDs again; original 400 no_stats markers are not reused as new coverage.
Order: status 9 active, status 11 paused, status 7 completed. Within a status, controls
first, then numeric advertId ascending. Never drop status 7, deduplicate IDs, infer a
complete 30.09 population from previous activity, or use controls as an allowlist.

Use existing `wbAdsFullstatsCollect_`, `wbAdvFlattenFullstats_` and
`wbAdvCampaignStatNoStatsRow_`. The collector has an optional incident I/O argument
(fetch and capture) and suppresses implicit status fallback only when supplied.
All ordinary callers retain the original fetch/error behavior.
Requests are GET adv/v3/fullstats with exact 30→30 range, chunks <=50, existing
50→10→single error splitting. Incident HTTP retries are bounded to 3 attempts;
21s/backoff/Retry-After and deadline checks apply before each request and sleep.
Total execution budget is 330s; source stops 75s earlier, reserving publication and
finalization time. Native Google/WB calls cannot be interrupted by a JS deadline;
a platform termination leaves STARTED or uncertain RAW and never synthesizes success.
A future retry after interruption needs read-only state inspection and a separate ACK.

The complete payload stays in memory until all source/coverage/controls/date/grain
checks pass. Incomplete source work -> PARTIAL result/new ERROR journal, **zero RAW**.
Malformed/unrequested/repeated response campaign, non-array days/apps/nm/booster,
or any non-30.09 day/booster date fails closed. No filtering or source normalization.
`adsDayFullstatsSource_` validates the entire ORIGINAL response before control
evidence or collector acceptance; legacy flatten consumes only validated responses.
Each response must be an array or an object with its own `data` array. No missing
or malformed container is defaulted to empty. The consumed hierarchy is:

| Source structure | Required shape / absence rule |
| --- | --- |
| campaign array entries | Non-null object, not an array |
| campaign.days | Own array, required; each day is a non-null object |
| day.apps | Own array, required; each app is a non-null object |
| app.nm / app.nms | Exactly one own array, required; each item is a non-null object |
| campaign.boosterStats / booster_stats | At most one own array; both absent means no optional booster rows |
| booster array entries | Non-null object, not an array |

Required arrays preserve the previous incident adapter's requirements. Absent
boosters preserve the repository producer's optional-booster handling in
`wbAdvFlattenFullstats_` (WbAdsRawLoader.gs), not a live WB source-finality claim.
Present non-arrays (false, object, string, number, null, undefined) always fail.
Both aliases present always fail, even for equal/empty arrays or one valid alias:
`HTTP_NM_ALIASES` or `HTTP_BOOSTER_ALIASES`. No winner or merging is allowed.
Valid empty arrays remain structurally valid; the existing coverage/unresolved/control
predicates still decide completeness. Only legitimate omitted campaigns/empty response
arrays can generate the existing observational no_stats; malformed source cannot.

Consumed scalar aliases are also presence-checked before legacy selection:
advertId/advertID, date/dt, appType/appName, nmId/nm, sum_price/sumPrice, booster
nm/nmId and avg_position/position/avgPosition. Multiple names in any group fail
closed. Present consumed scalar leaves are validated against the complete scalar contract
below before flatten; objects, arrays and booleans are not business scalar values.
Missing/null/blank leaves retain existing optional semantics, with required RAW keys
validated again on the complete prepared payload. Date scope keeps its original errors.
Checked leaves are campaign identifier, day date, app type, all stat keys/name/metrics
read by flatten, and booster date/key/position. Opaque additional source fields
serialized in raw_json are not interpreted or subject to a new WB schema.
All selections preserve original arrays/objects and field values; no source is mutated.
Structural errors follow the existing preparation-failure path: `failures[].code`
and status evidence retain the structural reason, the local result is PARTIAL /
ADS_PARTIAL and the new journal attempt is ERROR / ADS_REPAIR_ADS_PARTIAL. Either
failure prevents BOTH RAW writers and COMPLETE, including errors in the final batch
after earlier data was prepared. The old parent heartbeat remains read-only.
Raw date bytes remain intact; exact YYYY-MM-DD or valid ISO text with 30.09 date
prefix is accepted, matching the logical day label, without converting it to UTC.

Whole prepared stat payload grain: **date / advertId / nmId / appType / source_level**,
the V_ADV_CAMPAIGN_STATS repository grain. NULL/empty key fields are rejected for
nm records. Every repeated key, conflicting or identical, is rejected before the
first RAW write. The effective view choosing a winner is not contractual authority
to silently deduplicate a response. No aggregation, winner selection, metrics filling
or billing-to-fullstats substitution. There is no DONE state; invalid grain cannot
reach publish or COMPLETE.

Booster grain is exactly **date / advertId / nmId**, directly defined by
`Wbadsbigquery.gs` for V_ADV_BOOSTER_STATS. `adsDayValidateBoosterGrain_` checks
the entire `p.boosters` inside the existing prepared-payload validator, before either
RAW writer. Identical duplicates and duplicates differing in avg_position or any
other value all fail with `BOOSTER_GRAIN_DUPLICATE`; neither destination is written
and the new attempt records ERROR rather than COMPLETE. Key comparison follows
the existing RAW STRING/NULL representation without changing payload rows.
The generic grain validator distinguishes different dates; the unchanged incident
date guard separately rejects every non-30.09 row. No business-key field is added.

Regression control: date=2026-09-30 / advertId=37563883 / nmId=909000001,
avg_position=5 and 99. The real narrow adapter with mock services must produce
zero stat/booster load calls and no COMPLETE; journal/status must identify the violation.

For each of the nine controls, persist population membership, HTTP request/result,
response presence/details, dates/nmIds/appTypes, classification, prepared RAW row
count and publication/readback outcome. Publication is NOT_ATTEMPTED before the
writer, UNKNOWN during/after an uncertain writer result, RAW_VERIFIED after exact
readback, EFFECTIVE_VERIFIED after downstream predicates. Uncertainty is never
reported as proof of no write. Controls:
37563883,37727911,37727969,37727999,38085124,37669244,37751908,37755348,39035441.
Each must return at least one valid nm stat record. An empty/omitted control is an
unresolved anomaly and blocks publication/COMPLETE. There is no manual override.
For other IDs, a successful omission is stored as the legacy **observational** no_stats
marker, not a numeric zero or proof of financial finality. A returned campaign with
no usable nm records remains unresolved. Requests and previous successful control
payloads remain inspectable after a later failure/deadline.

## Exact future writes

| Object | Allowed mutation |
|---|---|
| wb_raw.RAW_WB_ADV_CAMPAIGN_STATS | WRITE_APPEND of validated 30.09 nm stats plus new no_stats markers (empty date, period_from=period_to=30.09); new attempt run_id |
| wb_raw.RAW_WB_ADV_BOOSTER_STATS | WRITE_APPEND of returned validated 30.09 booster rows; new attempt run_id; no job if empty |
| wb_raw.INGEST_RUNS | Existing ingestRunStart_ ads/30.09/MANUAL creates one new attempt; existing Complete_/Error_ update ONLY that new STARTED attempt |
| Existing WB_ADS_STATUS tab | Append-only technical evidence: 9 control rows + summary PREPARED, then 9 + summary VALIDATED on success (20 rows); ERROR attempt adds available control rows + summary |

No campaigns/costs/query bids/query stats/search clusters/month loader, no Sheets RAW,
no history 24–29 writes, no mart/Unitka/LCD, no old heartbeat mutation. Journal
primitives' context is revalidated before every write, preventing config drift from
redirecting a journal mutation to another project. Status errors are propagated,
never swallowed or corrected by creating a tab/expanding headers.

At most one load job per nonempty destination, explicit new run-derived job ID,
CREATE_NEVER, WRITE_APPEND, maxBadRecords=0, ignoreUnknownValues=false, EU.
Caps: <10000 rows per destination; prepared JSON <5M JS characters; each encoded
load <5MB. SELECTs are Standard SQL, bounded to origin run/day/history 24–30 and
100MB billed per query, no destination/materialization, no remote function/procedure.
Truncated/paginated results, missing metadata or job uncertainty fail closed.
This patch has **no automatic republish/retry** or destructive rollback.

## COMPLETE predicates and before-images

Before the new attempt, establish hashes of every effective historical row 24–29
(all columns/multiplicity) and empty effective AND physical dated stat/booster30.
An existing target row or STARTED/COMPLETE attempt for ads30 aborts rather than
blindly appending a second repair.

Before COMPLETE require all 431 classified and resolved, failed/skipped=0, dates
and grain valid, nine controls resolved with successful request/response evidence,
all RAW jobs DONE without errors, output row counts and full-row run readback equal
to prepared payload, effective30 equal to all prepared raw stat rows, effective24–29
hash unchanged, reused evidence hash unchanged, own journal still STARTED, and all
PREPARED/VALIDATED status evidence read back. Then call ingestRunComplete_ directly
with actual fetched/loaded rows; require true plus separate COMPLETE readback.
Never call a generic finalizer with synthetic OK statuses.

Future owner-approved execution must separately preserve external before-images
of source/config/triggers, approved view definition/hash, origin/journal/RAW evidence,
and effective24–30. Runtime hashes and WB_ADS_STATUS are validation evidence, not a
full rollback backup. No source/config/property changes occur in the recovery itself.

A failure after the first append may leave dated30 RAW visible through the existing
effective view even though the journal is ERROR/STARTED. The mart gate remains closed
on a non-COMPLETE latest attempt. No transaction spans stat/booster/journal/Sheet.
Do not automatically retry, DELETE, hide data or modify an old heartbeat. Inspect
job IDs, physical/effective rows and actual terminal status, then obtain a separately
reviewed bounded recovery decision. If finalizer response/readback is uncertain,
COMPLETE may have committed after valid predicates: report uncertainty and inspect;
returning ERROR locally does not prove the journal is ERROR. The runtime never
rolls back a terminal COMPLETE.

## Permanent daily fix — separate approval scope

Propose active→paused→completed on the same full eligible population in daily loader.
If measured runtime still cannot cover it, add a small persisted cursor/resumption
for the remaining population. Do not solve hard platform limits by timeout increase,
drop completed campaigns, weaken mart's gate, or activate generalized recovery.
No permanent daily ordering, cursor or scheduler change is implemented here.

Next ACK: independent final review of this exact incident source/test/contract
package only. Deployment, activation/WB calls/recovery, mart and Unitka each remain
separate later ACKs. No production authorization follows from offline PASS.


## Complete scalar contract — bounded owner ACK, 2026-10-02

The previous candidate 49140997d5c12668022ce8ef06b2bc78eb41ec53a02580c565d7bdc6d77ceb18
failed independent review: `sum:false` could become RAW STRING `"false"`, pass byte
readback and reach COMPLETE despite failing the mart NUMERIC parse contract.
This correction closes the entire persisted scalar class, not a single boolean case.
CURRENT here means current local repository/implementation; deployment/live parity
remains UNPROVEN. No source payload or credential is acquired from production.

### Evidence and limits

* `WbAdsRawLoader.gs`, WB_ADV_RAW_CAMPAIGN_STATS_HEADERS_ (24) and
  WB_ADV_RAW_BOOSTER_STATS_HEADERS_ (11): authoritative persisted field inventory.
  `wbAdvFlattenFullstats_`: exact original aliases, values and metadata; no new fields.
  `wbAdvCampaignStatNoStatsRow_`: explicitly empty business payload cells for markers.
* `Wbadsbigquery.gs`: all RAW columns nullable STRING; effective keys remain exactly
  stat date/advertId/nmId/appType/source_level and booster date/advertId/nmId.
  STRING storage and winner selection do not validate business types.
* `sql/mart/pr_mart1_facts.sql` Ads parsed/parse-QC/NULL grain assertions and
  `sql/mart/pr_mart1_validation.sql`: identifiers/views/clicks/orders INT64,
  sum/sum_price NUMERIC after replacing comma with dot, logical date prefix parse.
* `sql/ads/ads3_campaign_performance.sql` parsed: additionally atbs/shks/canceled INT64.
* `docs/ADS3_CAMPAIGN_PERFORMANCE_2026-09-06.md` §2: historic producer verification
  corroborates integer counters, NUMERIC sums, FLOAT ctr/cpc/cr. It does not prove
  current live payloads. Booster position aliases and fractional numeric position
  come from the flatten contract; there is no integer position assertion/consumer.
* `wbAdsNow_` generates yyyy-MM-dd HH:mm:ss; `wbAdvRawJson_` permits truncation and
  empty text. Thus raw_json is optional opaque text, not mandatory parseable JSON.
* appType/appName is a source-preserved label in the STRING effective key: finite
  number or nonblank string, including name labels. No invented INT appName rule.

### Field/type inventory (raw rows)

R = required/non-null/nonblank. O = optional; undefined/null/empty string retained
as existing RAW NULL by adsDayRows_. Whitespace-only optional cells are also blank,
consistent with SQL TRIM parse-QC. No boolean/object/array is an allowed scalar,
even in optional fields. No generic stringify is used to establish validity.

| Field | Destination | Required | Primitive / semantics | NULL / numeric strings |
|---|---|---|---|---|
| load_ts | stat, booster | R | string; valid generated calendar timestamp YYYY-MM-DD HH:mm:ss | no / no |
| run_id | stat, booster | R | string; exactly the new attempt ID | no / no |
| period_from | stat, booster | R | string; exactly 2026-09-30 | no / no |
| period_to | stat, booster | R | string; exactly 2026-09-30 | no / no |
| source_method | stat, booster | R | string; adv/v3/fullstats | no / no |
| processed_status | stat, booster | R | string; raw, or stat-only valid no_stats marker | no / no |
| advertId | stat, booster | R | number or string; INT64, full approved population member | no / yes |
| date | stat, booster | R | string; pinned day or existing valid ISO day label, bytes unchanged | no / no |
| appType | stat | R | nonblank string or finite number (appType/appName label) | no / string labels allowed |
| nmId | stat, booster | R | number or string; INT64 business key | no / yes |
| name | stat | O | string (flatten preserves optional empty/null name semantics) | yes / text only |
| views | stat | O | number or numeric string; INT64 | yes / yes |
| clicks | stat | O | number or numeric string; INT64 | yes / yes |
| ctr | stat | O | finite number or numeric string; FLOAT | yes / yes |
| cpc | stat | O | finite number or numeric string; FLOAT | yes / yes |
| cr | stat | O | finite number or numeric string; FLOAT | yes / yes |
| atbs | stat | O | number or numeric string; INT64 | yes / yes |
| orders | stat | O | number or numeric string; INT64 | yes / yes |
| canceled | stat | O | number or numeric string; INT64 | yes / yes |
| shks | stat | O | number or numeric string; INT64 | yes / yes |
| sum | stat | O | number or numeric string; NUMERIC representable (38 digits/9 scale) | yes / yes; comma accepted by consumer |
| sum_price | stat | O | number or numeric string; NUMERIC representable (38 digits/9 scale) | yes / yes; comma accepted by consumer |
| source_level | stat | R | string; nm | no / no |
| avg_position | booster | O | finite number or numeric string; fractional position permitted | yes / yes |
| raw_json | stat, booster | O | opaque string; existing empty/truncated representation valid | yes / text only |

INT64 validation checks the exact resulting numeric primitive/string representation
against signed 64-bit bounds, without lossy Number-based string comparisons. Decimal
integer strings (sign/whitespace/leading zeros) and CAST-compatible hexadecimal
integer strings are accepted unchanged. Fractional counters are rejected. Numbers
must be finite integers and their actual serialized representation must be INT64.
NUMERIC uses decimal lexical/range/scale-rounding inspection, preserving comma and
exponent representations and precision; extra fractional digits may round in the
consumer, but rounding overflow is rejected. Tiny decimals rounding to zero are
accepted unchanged. Exponents must be safely representable integers for inspection.
FLOAT numeric text uses decimal/exponent syntax and finite conversion; NaN/Infinity
are not valid measurements. No new shared non-negative requirement is invented:
source-faithful signed numbers remain valid; later mart financial-bound assertions
remain separate and unchanged. There is no precision/range restriction invented for
labels, names or opaque raw_json beyond their defined primitive types.

For a legitimate stat no_stats row, the first seven metadata/advertId fields retain
the above required contract; all remaining business payload fields MUST be blank.
Booster no_stats rows remain prohibited. No zeros, filled metrics or copied billing
are manufactured to make absent fullstats look present. Both stat and booster RAW
keys must be non-null; the generic duplicate comparator still uses the existing
STRING/NULL grain and does not change source keys.

### Atomic pre-publication and failure evidence

`adsDaySourceScalars_` checks ORIGINAL present consumed leaves using the same field
contracts before evidence extraction, alias fallback or flatten. Both-alias failures
remain unchanged. Source null/absent/blank leaves preserve existing missing semantics;
required keys/date/population are enforced by existing guards and the complete row
check. Malformed scalar fields in empty branches also fail closed. Original date
scope guards retain HTTP_DATE_SCOPE / BOOSTER_DATE_SCOPE.

`adsDayValidateScalars_` visits every persisted field in every prepared stat and
booster row from inside `adsDayValidatePrepared_`, before PREPARED audit and before
io.publish. BOTH complete payloads pass before either destination can publish.
Malformed source is inspectable in existing HTTP_*_VALUE_SCHEMA preparation evidence:
PARTIAL/ADS_PARTIAL, new journal ERROR, no RAW/COMPLETE. Malformed prepared scalar is
an ERROR with RAW_STAT_SCALAR_<FIELD> or RAW_BOOSTER_SCALAR_<FIELD>, or an earlier
existing scope/key/date violation. No malformed scalar row reaches either writer.
The old failed heartbeat is never finalized/edited. Only the already defined new
attempt finalizer remains eligible after all validation/readback checks.

This owner ACK changes only incident core scalar definitions/guards, the narrow
source scalar helper, focused tests and this package documentation/evidence. It does
not change flatten, RAW schemas, effective views, mart SQL, ordinary/resume loaders,
pinned day, population/order, controls, campaigns/costs reuse, grains, writers,
heartbeat semantics, billing maturity or history verification. No deployment or
production execution is authorized. Fresh independent review has NOT been performed.
