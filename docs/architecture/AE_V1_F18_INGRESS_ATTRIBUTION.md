# AE v1 — F-18 semantics of the trusted audit: public ingress attribution

Status: implemented in `tools/autonomy/ingress_attribution.py` (audit source `…+ingress_attribution/v5`).
F-18 itself stays **OPEN**: `allUsers → run.invoker` on `evetis-wb-communications` is unchanged and no
exception is added. What changed is what the audit is allowed to conclude from it.

## 1. Root trust model

`evetis-wb-communications` is public because Telegram must reach `/telegram-webhook`. Anyone can send a
request; Cloud Run IAM logs say nothing about who. The only external writes (WB answers, Telegram Bot API)
happen inside request handlers after application-level authentication: scheduler secret (`/poll`),
Telegram secret header + allow-list (`/telegram-webhook`), admin token (`/admin/*`, not registered in
production). The audit identity (`sa-ae-reader`) holds none of these secrets.

So "AE caused no mutation through the public ingress" is provable only per window, from two immutable
sources joined by the trace id — never by time proximity:

* **platform request log** `run.googleapis.com/requests` — written by Cloud Run for every request, with its
  trace, revision, status and latency; routing to `_Default` without exclusions is proven by `routing_problems`
  (current state) and by the absence of logging-configuration changes in Admin Activity around the window;
* **service security events** `wbcomm.security.v1` on `run.googleapis.com/stdout` (D-19b, wb-communications
  ≥ 1.6.0, contract `services/wb-communications/SECURITY_AUDIT_EVENTS.md`).

Trace headers are **client-controlled** on a public service: the service records a primary trace and, if a
client sent a second different one, an `alt_trace_id`; the audit matches the platform trace against either.

**What ATTRIBUTED proves, and what not:** every write of the service — external (WB, Telegram) and internal
(its Firestore state and BigQuery `evetis_communications`, instrumented with `audited_store`) — followed the
service's OWN authentication of the same request. It does not prove who held the secret — that rests on the audited identity not holding the
scheduler/webhook/admin secrets (F-19 residual risk accepted by the owner 2026-09-28; D-19a/D-19d pending), and on
the audited identity not being able to write, delete or re-route log entries (`logging.logEntries.create`,
`logging.logs.delete`, sinks, exclusions, buckets, settings, views are in `FORBIDDEN_PROJECT_PERMISSIONS` and
self-checked live).

## 2. Decision per public Cloud Run service (in `run_audit`)

Services evaluated: every Cloud Run service of the audited project whose `roles/run.invoker` was public
(`allUsers`/`allAuthenticatedUsers`) at ANY moment of the window (IAM history replayed from Admin Activity), and
every service whose invoker IAM check was disabled (`run.googleapis.com/invoker-iam-disabled`/`invokerIamDisabled`).
Only the exact `roles/run.invoker` public binding of a PROVEN service is exempted from the policy problem; every
other public binding remains BLOCKED, as before.

Window: `since` = run `created_at`; `until` = last transition to `READY_FOR_PR` (`run_window_end`; `now` if the
run returned to agent work afterwards). A request counts if `[ts − latency, ts + latency]` overlaps the window
(receive- or completion-stamped — not relied on); missing latency = 3600 s (Cloud Run maximum). The window must
be settled: `now` (sampled before the reads) ≥ `until` + 3600 s + 300 s ingestion margin.

| class | proven when | otherwise |
|---|---|---|
| `NO_TRAFFIC` | no counted request; no security event in the window; **positive control**: the identical request filter matches ≥ 1 well-formed request of this service in the 7 days before the lookback (one page read) | BLOCKED |
| `ATTRIBUTED` | every counted request passes the checks below | BLOCKED |
| both | **spec proof** from Admin Activity (exact resource name and region, applied changes only — no rejected/validate-only): EVERY spec of the service from creation to `until` (older revisions stay invocable through tags/splits; a failed rollout leaves the old revision serving) has request-based CPU (`cpu-throttling` not false; v2: once `resources` is present, `cpuIdle` must be explicitly true) and no minimum or manual instances; a spec must exist before the lookback; **no logging routing change** (sinks, exclusions, buckets, settings, views, `DeleteLog`) from 7 days before the window up to the audit | BLOCKED |

Checks for `ATTRIBUTED` (numbering = owner ACK 2026-09-28, Part C):

1. public ingress exists — from the IAM history / spec history;
2. application authentication exists — `auth_ok` whose mechanism matches the route (`/poll`→scheduler secret,
   `/telegram-webhook`→Telegram secret, `/admin/*`→admin token; any other route cannot mutate);
3. every mutation-producing request has that `auth_ok`;
4. the auth event is on the same trace and correlation, **referenced by the attempt** (`auth_event_id`) and
   earlier in the request's own order (`seq`, not timestamps);
5. every mutation event is on the same trace and correlation as the request; WB writes from the Telegram
   webhook also reference (`authz_event_id`) an earlier `authz_ok` of a configured allow-list;
6. every attempt has exactly one later outcome; for EXTERNAL targets (WB, Telegram) it must be known
   (`success|rejected|error`; `outcome_unknown` → BLOCKED); internal state writes need the auth link, their
   outcome is informational;
7. no mutation event without a matching `auth_ok`;
8. no ambiguous/unattributed request: missing trace; `seq` not exactly 1..n; `request_start` not first or
   `request_done` not last; one correlation on two requests or with inconsistent trace pairs; duplicated
   `event_id`; revision or status mismatch; `mutation_attempts` ≠ attempts seen; any security event in the window
   that is not part of an attributed in-window request (checked per event, not per correlation). Every event must
   carry the required fields of its type (ids 32 hex, route and mechanisms from the contract): a missing field
   never matches anything. Client-controlled trace headers: a trace shared by several platform requests is
   tolerated only if none of them mutated (complete event pairs, statuses and revisions matching); a request whose
   events carry no trace (3+ conflicting headers) is paired only if it never authenticated and never mutated, with
   the platform requests left without events, by equal (revision, status) multisets — anything else is BLOCKED;
9. no secret leakage: closed field set, bounded values, no token/JWT/bearer shapes, no `<invalid>`, RFC 3339 `ts`;
10. instrumentation present in production for the window — proven **per request** (a request served by an
    uninstrumented revision has no events → BLOCKED). No revision allow-list and no back-dating.

All source reads retry only 429/5xx/network errors, at most 3 times (2, 4, 8 s); exhaustion is BLOCKED.

## 3. Historical commissioning window (run-20260927T121235Z-ab2e1c53)

Evaluated 2026-09-28 with this code (full path), read-only, owner credentials — evidence, **not** the trusted proof:

| item | value |
|---|---|
| window | 2026-09-27 12:12:35Z → 12:53:58Z (READY_FOR_PR), lookback to 11:12:35Z |
| services public during the window | only `evetis-wb-communications` (174 IAM events replayed); none with invoker IAM disabled (43 run spec events) |
| platform requests in [since − 1 h, until + 1 h] | **0** (nearest: `POST /poll` 11:00:06Z, latency 8.6 s — ended before the lookback) |
| security events | 0 (1.6.0 not deployed then — not needed for this class) |
| positive control of the filter | real requests of the service found by the identical filter in the 7 days before (45 in a full read) |
| spec in force | last change 2026-07-29 15:08:58Z: only `startup-cpu-boost`; traffic 100 % latest; no change in the window |
| logging routing | `_Default` default filter, no exclusions, retention 30 d; no logging-config change in Admin Activity |
| verdict | **`NO_TRAFFIC` → PROVEN** |

The proof uses pre-existing immutable sources only; new instrumentation is not applied retroactively.

**Deadline:** request logs live 30 days in `_Default`; `routing_problems` requires retention > window + 1 day.
The trusted re-audit of this run must happen before **2026-10-26** or the proof expires.

## 4. Remaining step for the existing candidate (d221e72), no inference

1. Owner merges this remediation into PR #202 and PR #202 into main (separate ACKs).
2. Publisher credential resolved (`AE_V1_PUBLISHER_CREDENTIAL_DESIGN.md`), or option C.
3. Owner ACK for an audit-only resume: `autonomy-run.yml` with `run_id=run-20260927T121235Z-ab2e1c53`,
   `AE_ENABLED=true` for that dispatch only, **no earlier than 65 min after the last READY_FOR_PR** of the
   window being proven (settle = 3600 s + 300 s; the gate's own sleep is only 300 s, so an earlier audit is
   BLOCKED "not settled"). Engineer/reviewer jobs exit on state `READY_FOR_PR` before any
   Claude call (verified in `autonomy-engineer.yml`/`autonomy-review.yml`); the trusted `audit` step evaluates
   the whole window with `sa-ae-reader`; only PASS lets `publish` run.

## 5. Residual (explicit)

* Organization-level intercepting sinks are not readable by the audit identity (`iam_check --live` of the owner).
* Candidate CI after publication runs after this audit; its requests would need a second audit-only pass.
* D-19a (Scheduler OIDC instead of the static secret), D-19c..f — not implemented.
