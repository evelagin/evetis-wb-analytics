# CHANGELOG

## 1.5.3 — v2 knowledge base aligned with closed owner decisions (Phase 3 WP11, 2026-09-28)

Changes v2 draft wording (approved cleanup). Local verification: pytest green.

- ODR-02: customer-facing «Oud & Wood» / «Lost Cherry» -> «древесно-удовый аромат» /
  «вишнёвый аромат» (internal aliases kept for resolution).
- ODR-09: removed «прокачать 3–5 раз… почти всегда решает»; no numeric pump instruction.
- ODR-08: «направить в чат для замены» -> WB flow (обращение через личный кабинет Wildberries,
  фото; решение принимает площадка); no promises, no invented contact channel.
- KC-03: hand cream PAO «24 мес» -> 12 мес (T1 icon 12M).
- ODR-15: «6 типов церамидов» -> «комплекс церамидов (NS, NG, NP, EOP, AP, AS)».
- ODR-13: hand cream declared «для рук» (T1), «для тела» added to prohibited claims.
- New test `tests/engine/test_kb_owner_decisions.py` guards the KB bodies.

## 1.5.2 — Recovery card must be actionable (2026-09-28)

Local verification: **268/268 pytest passed** (10 new; 5 of them fail on 1.5.1).

- **Root cause (question ce1a68fb):** after a manual edit `_handle_message` sent a NEW
  card but ignored its `message_id`, so Firestore kept pointing at the superseded
  card. «Опубликовать» was pressed on the new card (pre-1.5.0 → «✅ Опубликовано»).
  1.5.1 reconciliation edited the OLD stored message to the warning; the card the
  operator sees stayed «Опубликовано» without a button.
- Manual edit now stores the new card id (`telegram_message_id`/`telegram_chat_id`)
  and strips the buttons from the superseded card (best effort).
- A background (poll) transition to `publish_unknown` no longer edits the stored card:
  it sends a NEW «⚠️ Публикация не подтверждена на Wildberries» card with
  «Опубликовать», stores its id and `recovery_card_sent_at`, retires the old card.
- Each `/poll` gives questions already in `publish_unknown` without
  `recovery_card_sent_at` one live card, after a read-only GET (answer meanwhile on WB
  → resolved as `published`/`answered_externally`, no button). Never writes to WB.
- An operator action records the pressed card as the doc's card. Background Telegram
  updates are best effort (state is persisted first; a failed recovery card is
  retried by the next poll).
- New Firestore field `recovery_card_sent_at`; new summary key
  `questions.reverified.recovery_cards`. Publication path unchanged (GET first,
  at most one PATCH, bounded read-back).

## 1.5.1 — Reconciliation of pre-1.5.0 «published» questions (2026-09-28)

Local verification: **258/258 pytest passed** (4 new).

- Before 1.5.0 a question became `published` on any 2xx with a wrong body, so that
  status is not evidence of an answer on WB. Each `/poll` now reconciles such legacy
  docs (question, `published`, no `publication_state`) with one read-only
  `GET /api/v1/question`: our text on WB → stays `published` + `verified_at`
  (silent); another text → `answered_externally`; no answer → `publish_unknown`
  with the «Опубликовать» button (the corrected path reads WB before writing).
  Never writes to WB; reviews untouched. `list_by_status` gains an optional
  `entity_type` equality filter (single-field indexes, no composite index).

## 1.5.0 — Question publication fix + verified publication (2026-09-28)

Local verification: **229/229 pytest passed** (26 new in `test_question_publication.py`).

- **Root cause of «опубликовано, но на WB нет ответа»:** `PATCH /api/v1/questions`
  was sent as `{"id","text","state"}` (1.4.0). The official contract (WB OpenAPI
  09-communications, example «AnswerQuestionOrEditAnswer») is
  `{"id","answer":{"text"},"state":"wbRu"}`. WB replied `200 {"error": false}` to the
  wrong body but created no answer; the service treated any 2xx as published.
- **Read-before-write** (`GET /api/v1/question?id=`, new `WB_QUESTION_PATH`): an
  already-answered question is never written again (re-tap, crashed lease,
  answer typed in the WB cabinet → new status `answered_externally`).
- **No false success:** a question is `published` only when the answer is read back
  from WB and matches. WB-accepted-but-not-visible (pre-moderation) →
  `publish_accepted` (re-verified on every `/poll`, read-only); outcome unknown →
  `publish_unknown` (operator re-tap = check first, then write). Telegram says
  «Опубликовано» only for `published`. `error: true` inside a 2xx body = failure.
- **No blind duplicate writes (reviews and questions):** publish writes retry only
  429 and connect-phase errors; read timeout / 5xx after send raise
  `WBPublishOutcomeUnknown` and are verified instead of re-sent.
- **Traceability:** each attempt appends a `publish_trace` entry on the Firestore
  document (attempt id, endpoint, method, payload sha256, http status, safe
  response excerpt, verification attempts/result, final state) and the same record
  goes to `communication_events.payload_json`. No BigQuery column changes.
- **Logging:** builds on 1.4.1 (token redaction, Telegram httpx lines dropped by host);
  publication traces carry only hashes and redacted WB response excerpts.
- **WP11 (owner-approved):** no silent fallback to `reviews_v1` when v2 is primary
  (item error, retried next poll); operator-typed answers go through the same
  validators as AI drafts (advisory flags); `cases/irritation.md` and
  `cases/allergy.md` rewritten to the approved S1 meaning (no «адаптация», no
  causality, no invented contact channels).
- New settings (defaults, no env change needed): `WB_QUESTION_PATH`,
  `WB_QUESTION_VERIFY_ATTEMPTS` (3), `WB_QUESTION_VERIFY_DELAY_SECONDS` (2.0),
  `WB_QUESTION_VERIFY_WINDOW_HOURS` (48).

## 1.4.1 — SECURITY: токен Telegram-бота больше не попадает в журналы (инцидент 2026-09-27)

httpx писал INFO `HTTP Request: POST https://api.telegram.org/bot<TOKEN>/…`, а шаблон
редакции `\b\d{6,}:` не срабатывал (между `bot` и цифрами нет границы слова): за 30 дней
245 записей stdout с полным токеном. Поведение сервиса не меняется.

- **Строки httpx с хостом `api.telegram.org` отбрасываются фильтром по хосту** (не regex).
  Строки WB/OpenAI остаются: их ключи в заголовках, а строка `POST feedbacks-api…` —
  единственное доказательство фактической публикации в WB. httpcore — не ниже WARNING.
- **Редакция по значению**: каждый секрет, прочитанный `get_secret` (env или Secret Manager),
  регистрируется и маскируется дословно, в URL-кодировке и половиной после `:` — в любом формате.
- **Шаблон Telegram-токена без `\b`** (ловит `/bot<TOKEN>` и `%3A`); редакция покрывает
  сообщение, исключение, стек, вложенные structured-поля.
- **uvicorn и Python warnings** идут через тот же редактирующий JSON-формат.
- **`TelegramClient`**: ошибка транспорта httpx (может нести URL с токеном) заменяется
  `TelegramError` без URL — по-прежнему транзиентная (Telegram доставит повторно).
- **Сбой форматирования** больше не уходит в обработчик ошибок logging (он печатал сырое сообщение в stderr):
  резервная строка уже отредактирована; `json.dumps(default=redact(str))`; `logging.raiseExceptions = False`.
- `Secrets`/`admin_token` не показываются в `repr`; `ADMIN_TOKEN` из окружения регистрируется для маскирования;
  `error_message` событий редактируется перед сохранением; исходная ошибка httpx не привязывается к `TelegramError`.
- Тесты `tests/test_log_redaction.py` — только на синтетическом токене.

## 1.4.0 — WB buyer questions (separate entity, own gates)

Local verification: **195/195 pytest passed**. Reviews contour unchanged; questions
ship OFF by default.

- **New entity `entity_type=question`** — questions are NOT masked as reviews.
  Dedup via `make_doc_id("wb","question",id)`; event-id hash now includes
  `entity_type` so review/question source_ids can never collide in BigQuery.
- **`Question` model** (`from_wb_question`), review-attribute-compatible so the same
  `EnginePromptService` builds a prompt with `communication_type=question`.
- **WB client**: `get_unanswered_questions` / `iter_unanswered_questions`
  (`GET /api/v1/questions`) and `publish_question_answer`
  (`PATCH /api/v1/questions`, body `{"id","text","state":"wbRu"}`).
- **Pipeline**: questions polled AFTER reviews (`_run_questions`), shared
  `_draft_and_send`; separate `build_question_card`; entity-aware `_publish`
  (own gate + WB endpoint + error hints), `_regenerate`/`_show_full`/edit all
  entity-aware. Questions always use the v2 engine.
- **First-run cap** `WB_QUESTIONS_FIRST_RUN_MAX` (20): bounds NEW question cards
  per poll so the historical backlog doesn't flood Telegram; nothing is lost
  (unprocessed stay unanswered on WB, picked up next poll, newest first).
- **Separate gates** `WB_QUESTIONS_ENABLED` (poll) and
  `WB_QUESTION_PUBLISH_ENABLED` (answer), both fail-closed; `WB_QUESTIONS_PATH`,
  `WB_QUESTION_ANSWER_METHOD`, `WB_QUESTION_ANSWER_STATE`.
- **deploy/env.production.yaml**, `.env.example`, DEPLOY.md (rollout steps) updated.
- **Tests** `tests/test_questions.py` (18): model, pagination, PATCH body, engine
  generation, dedup, first-run cap, disabled publish gate, publish, error codes
  (403/404/422/429/503), regenerate, edit, no review impact, off-by-default.

## 1.3.2 — detailed WB publish error messages (post go-live)

Local verification: **177/177 pytest passed**. First real WB publications confirmed
in production (two `published` events, no errors).

- **Operator-friendly publish errors.** On a WB publish failure the Telegram card
  now shows the HTTP code AND a plain-language reason instead of a bare number,
  e.g. «❌ Wildberries вернул 403 — Недостаточно прав API-токена…», with hints for
  400/401/403/404/409/422/429 and a generic transient note for 5xx. Helper
  `_wb_publish_error_message`; covers all WB error types (auth/rate-limit/server
  are subclasses of `WBApiError`, so all are caught).
- **Tests** `tests/test_publish_errors.py` (8): parametrized 403/401/422/404/429/503
  messages carry code + reason and never publish on failure; unit hints; happy path
  still clean.

## 1.3.1 — WB answer endpoint aligned to current spec (publish still off)

Local verification: **169/169 pytest passed**. `WB_PUBLISH_ENABLED` stays false.

- **Default publish endpoint corrected** to the current WB OpenAPI spec: a first
  answer to a feedback is `POST /api/v1/feedbacks/answer` (was `PATCH
  /api/v1/feedbacks`). Body `{"id","text"}` and raw-token auth unchanged; listing
  stays on `/api/v1/feedbacks`. Still fully overridable via
  `WB_ANSWER_METHOD`/`WB_ANSWER_PATH`. Confirm on live Swagger before enabling.
- Updated `config.py` defaults, `deploy/env.production.yaml`, `.env.example`,
  `wb_client` docstring, DEPLOY.md pre-publish checklist.
- Tests: `test_publish_sends_id_and_text` now asserts `POST /api/v1/feedbacks/answer`;
  added `test_publish_endpoint_is_configurable` (method/path override still works).

## 1.3.0 — Communication Engine v2 as PRIMARY generator (opt-in)

Local verification: **168/168 pytest passed**. WB publish gate unchanged (still off).

- **New flag `COMMUNICATION_ENGINE_V2_PRIMARY`** (env, default `false`). With
  `_ENABLED=true` + `_PRIMARY=true`, the Telegram draft is generated by the
  verified-KB engine + validators instead of `reviews_v1`. This fixes drafts that
  repeated stale `system_v1.txt` data (e.g. «касторовое масло» for the hand cream),
  because v2 grounds on the corrected knowledge base.
- **Pipeline**: `_generate_answer()` selects reviews_v1 vs v2; the engine's
  `prompt_version` (`engine_v1:…`) is recorded. `run_poll` and `_regenerate` both
  route through it. Validator errors / unresolved product / unsupported marketplace
  surface as a visible «⚠️ Проверка v2» line on the card — still human-approved.
- **Never regresses**: a non-transient v2 prompt-build failure falls back to
  reviews_v1 for that item (logged, flagged on the card); OpenAI errors behave as
  before. In PRIMARY mode the redundant shadow-comparison run is skipped.
- **Wiring**: `build_primary_engine()` builds the adapter only when enabled+primary;
  `build_shadow_components()` returns nothing in primary mode; the shadow_only
  fail-closed guard now applies only when neither shadow nor primary is set.
- **deploy/env.production.yaml**, `.env.example`, DEPLOY.md (Step 4) updated with
  the additive enable/rollback commands.
- **Tests** `tests/test_v2_primary.py` (6): engine generates the draft; validator
  flag on card; unresolved-product flag; fallback-to-reviews_v1 on engine fault;
  default still reviews_v1; wiring matrix.

## 1.2.2 — explicit BigQuery shadow-schema migration (pre-deploy)

Local verification: **162/162 pytest passed**. No deploy.

- **`ensure_shadow_schema()`** — idempotent migration: creates the shadow table
  if missing; otherwise `ALTER TABLE ADD COLUMN IF NOT EXISTS` for any missing
  column (crucially `shadow_id` on a table left by 1.2.0); re-reads the live
  schema; **fail-fast** `ShadowSchemaError` if a required field is missing or
  mistyped (INTEGER/INT64, BOOLEAN/BOOL aliases treated as compatible).
- **`scripts/migrate_shadow_table.py`** — pre-deploy command; **non-zero exit** on
  any failure so a deploy halts. Run once, before enabling shadow. Not called on
  `/poll` (the poll path never migrates BigQuery).
- **DEPLOY.md** — the shadow rollout now has an explicit Step 2 migration between
  the (v2-off) deploy and turning the shadow flag on.
- **Tests** `tests/test_shadow_migration.py`: new table created with `shadow_id`;
  old 23-col table gets ADD COLUMN; idempotent re-run; failed migration raises
  (not masked); wrong-type raises; legacy INTEGER/BOOLEAN aliases accepted.
- **`test_two_real_evaluations_different_versions_both_persist`** — replaces the
  hand-mutated row test with two real `evaluate()` calls (same review_id, two
  products → two prompt versions → two shadow_ids), exercising the whole path.

## 1.2.1 — shadow pre-deploy fixes (env safety, fail-closed, persistence, shadow_id)

Local verification: **156/156 pytest passed**. No deploy.

1. **Prod env not overwritten.** `deploy/env.production.yaml` keeps the LIVE
   values (`OPENAI_MODEL=gpt-5.6-terra`, `TELEGRAM_ALLOWED_USER_IDS=302044578,868383129`);
   only the three shadow keys are added. DEPLOY.md prefers additive
   `--update-env-vars` for turning shadow on.
2. **`COMMUNICATION_ENGINE_V2_SHADOW_ONLY` is now fail-closed.** `enabled=true` +
   `shadow_only=false` builds NO engine and logs CRITICAL — no fallback to any
   non-shadow/publish path. Decision extracted to `build_shadow_components()`.
3. **Shadow persistence failures are surfaced.** `_run_shadow` checks the
   `insert_shadow` return; a False logs a `shadow persistence failed` warning and
   does NOT break reviews_v1.
4. **Unique `shadow_id`.** New `SHADOW_SCHEMA` column + deterministic
   `compute_shadow_id(review_id, old_pv, v2_pv, run_version)` used as the BigQuery
   `insertId`, so re-shadowing a review on a different prompt version is not
   deduped; an exact retry stays idempotent.

## 1.2.0 — Communication Engine v2 shadow integration (no publish)

Shadow-only observation of the v2 engine alongside the production `reviews_v1`
pipeline. Local verification: **150/150 pytest passed**. **No deploy. v2 OFF by
default. `WB_PUBLISH_ENABLED` stays false.**

- **Two env flags, OFF by default.** `COMMUNICATION_ENGINE_V2_ENABLED=false`,
  `COMMUNICATION_ENGINE_V2_SHADOW_ONLY=true`. With ENABLED=false the v2 engine is
  never constructed or called — behaviour is byte-for-byte the same as before.
- **Parallel v2 run in `/poll`.** After the reviews_v1 card is sent, `_run_shadow`
  runs Review → ReviewInput → classification → PromptBundle → OpenAI → validators.
  It does NOT publish to WB and does NOT send a second Telegram message.
- **Isolated.** The whole shadow call is wrapped so any v2 failure (unsupported
  marketplace, unresolved product, generation error, schema/validation failure) is
  logged and swallowed — reviews_v1 keeps running; the failure is recorded with
  `v2_usable=false`.
- **Separate BigQuery table** `communication_engine_shadow` (own `SHADOW_SCHEMA`,
  `insert_shadow`, `ensure_dataset_and_tables`). Never mixed with the publication
  history. Row captures: review_id, old/v2 prompt_version + answers, classification,
  product & marketplace resolution status/method, needs_manual_moderation,
  validation passed/issues, latency, model, timestamp.
- **`deploy/env.production.yaml`** ships both flags OFF; DEPLOY.md documents a safe
  deploy (v2 off) and a SEPARATE post-deploy command to switch the shadow flag on.
- **Tests** `tests/test_shadow_integration.py` (9): disabled→not called;
  enabled→reviews_v1 unchanged; v2 error isolated; row recorded; no WB publish;
  unresolved product recorded; validation failure recorded; Telegram payload
  unchanged.

## 1.1.2 — edit-rollback / event-enqueue / outbox-lease patch (pre-deploy)

Third audit response. Local verification: **65/65 pytest passed**, smoke passed,
compile OK, app imports, `/health` 200. **No deploy. `WB_PUBLISH_ENABLED` stays false.**

### C0 — roll back the EDITING lock if starting the edit fails
- `app/services/pipeline.py::_start_edit` — `send_force_reply` + `set_editing_session`
  are wrapped; on ANY failure `cancel_draft(doc_id, token)` releases the EDITING
  lock (→ pending_approval), then the exception is re-raised (webhook 503, Telegram
  redelivers; a later tap can edit again). No more record stuck in EDITING until lease.
- Test: `tests/test_v112.py::test_start_edit_rollback_on_telegram_failure`
  (begin_edit ok → TelegramError on send_force_reply → status back to pending_approval
  → raises → a later edit tap starts cleanly).

### C1a — event enqueue no longer silently swallows transient Firestore errors
- `app/services/repository.py` — `FirestoreRepository.enqueue_event` now RAISES
  `FirestoreTransientError` on a transient Firestore failure (permanent errors still
  swallowed to avoid a poison loop).
- `app/services/pipeline.py::_emit_event` — gained `best_effort` flag. `/poll` emits
  are best-effort (transient logged & skipped, never reverts a shown card); webhook
  outcome emits (published/failed/skipped/regenerated/manually_edited) are **critical**
  (`best_effort=False`) → a transient enqueue propagates so the webhook returns 5xx.
- Tests: `test_webhook_event_enqueue_transient_raises`,
  `test_poll_event_enqueue_transient_is_best_effort`.

### C1b — outbox lease (pending → sending → delivered)
- `app/services/repository.py` — `claim_outbox_event` (pending or expired-sending →
  sending, leased) in both repos; `list_pending_events` includes leased rows for
  recovery; `mark_event_*` manage the lease; `OUTBOX_LEASE_SECONDS` config.
- `app/services/pipeline.py::flush_events` — CLAIMS each event before the BQ write, so
  parallel flushes never send the same event at once (returns `skipped` count too).
- `app/config.py` — `outbox_lease_seconds` (default 60).
- Test: `tests/test_v112.py::test_outbox_claim_is_exclusive_until_lease_expires`.

### Docs
- `.env.example` — `OUTBOX_LEASE_SECONDS`.
- `DEPLOY.md` — "Обязательные секреты" block: `EVETIS_SCHEDULER_SECRET` and
  `EVETIS_TELEGRAM_BOT_TOKEN` are mandatory; `WB_PUBLISH_ENABLED` stays false; no deploy.

### Files touched
```
app/config.py   app/services/pipeline.py   app/services/repository.py
.env.example    DEPLOY.md                  NEW: tests/test_v112.py
tests/test_outbox.py (flush return shape)
```

### Known limitations / next steps
- `flush_events` still runs inline (poll end + post-webhook). A dedicated periodic
  `flush_outbox` Scheduler job remains the next step for events whose BQ writes keep
  failing between traffic (now safe to run concurrently thanks to the outbox lease).
- A webhook-outcome event that fails to enqueue returns 5xx to surface the loss, but
  after the business mutation is committed a redelivery is `stale`; a fully
  transactional outbox (event written in the same transaction as the state change)
  is the stronger next step if zero event loss is required.
- WB publish endpoint still unconfirmed; gated by `WB_PUBLISH_ENABLED=false`.

## 1.1.1 — C0/C1 concurrency & durability patch (pre-deploy)

Second audit response. Local verification: **61/61 pytest passed**, smoke test
passed, full compile OK, app imports, `/health` 200. **No deploy. `WB_PUBLISH_ENABLED`
stays false.**

### C0-1 — edit/regenerate race conditions
Edit and regenerate are now **locked intermediate states** with a lock token and
version guard, so a concurrent publish/skip/second-draft cannot race a stale
write back into `pending_approval`.
- `app/domain/statuses.py` — new `REGENERATING`, `EDITING` statuses; added to
  `LEASED_STATUSES`/`ALLOWED_ACTIONS`; `DRAFTABLE_FROM`.
- `app/services/repository.py` —
  `begin_regenerate`/`commit_regenerate` (status==REGENERATING + token check),
  `begin_edit`/`commit_manual_answer` (status==EDITING + token + `expected_generation`),
  `cancel_draft`, lease-based recovery of crashed EDITING/REGENERATING; editing
  session now stores `lock_token` + `expected_generation`. Removed the old
  status-only `begin_action("edit"/"regenerate")` and `set_manual_answer`.
- `app/services/pipeline.py` — `_regenerate` locks → OpenAI → commit (unlock on
  failure via `cancel_draft`; transient re-raised for webhook 5xx); `_start_edit`
  locks + stores token/version; `_handle_message` commits under token+version,
  rejects stale edits; `/cancel` and expiry call `cancel_draft`.
- Tests: `tests/test_concurrency.py` — (1) regenerate vs publish, (2) regenerate
  vs skip, (3) edit vs publish, (4) edit vs skip, (5) two concurrent regenerate
  + stale-token commit rejected, (6) stale edit after another edit rejected.

### C0-2 — Firestore/GCP infrastructure error classification
- `app/domain/exceptions.py` — `FirestoreTransientError(TransientError)`.
- `app/services/repository.py` — `translate_fs_errors` decorator + `is_firestore_transient`
  wrap `ServiceUnavailable`, `DeadlineExceeded`, `InternalServerError`, `Aborted`,
  `TooManyRequests`, transport errors → `FirestoreTransientError`; applied to all
  transactional methods. `complete_update`/`release_update` are safe (swallow their
  own errors). Update de-dup gained `attempts` + `giveup` poison guard.
- `app/routes/telegram_webhook.py` — **default is transient**: any unknown/infra
  exception → `release_update` + **503** (Telegram redelivers); `giveup` state →
  200 (bounded, no infinite loop). Events flushed after `complete_update`.
- Tests: `tests/test_firestore_errors.py` — translator maps ServiceUnavailable/
  DeadlineExceeded (passes ValueError through); route test: transient repo error
  during a callback → **HTTP 503**, update released, **not** marked processed.

### C1-1 — BigQuery exactly-once claim replaced with an outbox
No longer claims "exactly-once". Events go to a **Firestore outbox** and are
delivered to BigQuery separately; a failed BQ write leaves the event `pending`.
- `app/services/repository.py` — `enqueue_event` (create-if-absent by `event_id`),
  `list_pending_events`, `mark_event_delivered`, `mark_event_failed`
  (`status`,`attempts`,`next_retry_at`). Removed `claim_event`.
- `app/services/pipeline.py` — `_emit_event` enqueues (deterministic `event_id`);
  new `flush_events()` delivers pending → BQ, marks delivered/keeps pending.
  Flush runs at end of `run_poll` and after webhook handling.
- Tests: `tests/test_outbox.py` — first BQ insert fails → pending; second flush
  succeeds → delivered, **exactly one** BQ row; enqueue idempotent by `event_id`.

### Publish crash-recovery guard
- `app/config.py` — `wb_verify_before_publish` (default true).
- `app/services/repository.py` — `begin_publish` returns `recovered_from_publishing`
  when it recovers an expired PUBLISHING lease.
- `app/services/pipeline.py` — on recovery, **refuses to re-publish**; marks
  `publish_failed` and asks the operator to verify WB state first (because WB PATCH
  idempotency is not yet confirmed). Never a silent second public answer.

### Files touched
```
app/config.py                    app/domain/exceptions.py     app/domain/statuses.py
app/routes/telegram_webhook.py   app/services/pipeline.py     app/services/repository.py
.env.example                     README.md
NEW: tests/test_concurrency.py   tests/test_firestore_errors.py   tests/test_outbox.py
```
Unchanged from 1.1.0 and still green: wb_client/openai_client retry classification,
pagination, publish gate, state machine (skip/show), leases, existing idempotency/edit tests.

### Known limitations / next steps
- Transactional semantics verified via the `MemoryRepository` twin; a one-time run
  against the Firestore emulator/prod remains recommended (DEPLOY smoke steps).
- Outbox flush is inline (poll end + post-webhook). A dedicated periodic
  `flush_outbox` job (Scheduler) is the next step for retrying events whose BQ
  writes keep failing between traffic.
- WB publish endpoint still **unconfirmed** vs live Swagger; gated by
  `WB_PUBLISH_ENABLED=false`; crashed-publish recovery requires manual verification.

---

## 1.1.0 — C0/C1 hardening patch (pre-deploy)

Auth architecture (public Run + scheduler secret + admin fail-closed), Firestore
location, transactional state machine (skip/show), leases, update de-dup rework,
`WB_PUBLISH_ENABLED=false` gate, WB pagination, retriable/non-retriable exception
split, editing hardening (reply-scoped + WB-limit). 47/47 tests. See git history /
prior CHANGELOG entry for the full file map.
