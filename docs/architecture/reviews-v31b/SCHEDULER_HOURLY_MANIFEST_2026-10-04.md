# Hourly Scheduler — отдельный review manifest, NOT APPLIED

CURRENT metadata snapshot: 2026-10-04T07:15:46Z. Project `project-fa311fc0-4d87-4781-986`, region `europe-west1`, job `evetis-wb-poll`.

| Поле | Before | Proposed target |
|---|---|---|
| Cron | `0 8,11,14,17,20 * * *` | `0 * * * *` |
| Timezone | `Europe/Moscow` | сохранить |
| Frequency | 5/day; шаг3h в дневном окне, overnight gap12h | 24/day, раз/час, не чаще |
| State | ENABLED | сохранить |
| Method / target | существующий POST `/poll` | сохранить точно |
| Auth | scheduler secret header present; OIDC отсутствует | сохранить точно, не выводить header/value |
| Attempt deadline | 180s | сохранить |
| Retry config | min5s, max3600s, doublings5, duration0s; retryCount отсутствует в JSON/default0 | сохранить |
| Service revision/digest | fpub-f1eb89d / sha256:354be62bad4c7d9229fe02d9326767c63b2d4576e136d6c995b41006291a4b8c | не менять |
| Env fingerprint | facb6ead9bcc0d625463a635ce966b7e90ce2be9d01403caf75863a1ce1b382b | не менять |

Cron target включает ночные часы. Это предложенный точный manifest для отдельного ACK; не притворяется уже применённым решением. Редакционный PR не меняет Scheduler.

## Validation evidence

- **CURRENT live observation:** 11 завершённых request log entries `/poll` за 2026-10-02 05:00Z — 2026-10-04 05:00Z, все HTTP200; maximum observed latency **52.248773518s**, latest21.212620924s. Нет пересечения наблюдаемых request intervals. HTTP200 не доказывает completeness; этот Scheduler audit не подменяет wbc-audit/1 E2E.
- **CURRENT implementation:** record claim транзакционно/idempotently пропускает pending/published/in-flight; обновления Telegram dedup+lease; публикация — отдельный operator action. Feedback reconciliation только наблюдает, без автоматической повторной WB записи. Регрессии покрыты offline suite.
- **UNPROVEN worst-case:** Cloud Run request timeout300s, concurrency80. Это HTTP timeout, не доказанный hard stop handler. Нет distributed whole-poll lease. Shadow budget ограничен отдельно, весь v2 poll не имеет такого global deadline. Manual/duplicate deliveries и второй instance могут пересечься. Record idempotency не доказывает poll-level isolation.
- **Scheduler semantics:** для одной job Scheduler не держит два outstanding dispatch одновременно, но timeout может закончить dispatch до завершения handler. Delivery at-least-once: duplicated delivery возможна. Default retryCount0 + maxRetryDuration0 означает отсутствие retry-after-failure; это не exactly-once.
- **CURRENT local throttle:** WB_MIN_INTERVAL_SECONDS=0.6, интервал защищён threading.Lock на экземпляре WBClient, максимум примерно1.67 request/s в этом экземпляре. Нет доказанного общего throttle на продавца между Cloud Run instances / другими приложениями.
- **UNPROVEN API limit:** официальный RU OpenAPI `https://dev.wildberries.ru/openapi/user-communication` недоступен при проверке (HTTP498 через web и direct public read). Точную актуальную квоту reviews/questions и aggregate budget не подтвердили. Квоту ping и сторонние/CN цифры не переносим на RU endpoints.

Официальные источники: [Scheduler dispatch/retry CLI contract](https://docs.cloud.google.com/sdk/gcloud/reference/scheduler/jobs/create/http), [RetryConfig](https://docs.cloud.google.com/scheduler/docs/reference/rpc/google.cloud.scheduler.v1), [at-least-once delivery](https://docs.cloud.google.com/scheduler/docs/overview), [Cloud Run request timeout](https://docs.cloud.google.com/run/docs/configuring/request-timeout).

**ACTIVATION: BLOCKED / NO ACK.** До включения hourly нужны подтверждение method-specific RU rate limits и доказательство целостной защиты от дублирующего/параллельного poll либо отдельно reviewed ограничение с owner decision. Это не повод добавлять lease/менять production попутно.

## Отдельная future operation, после закрытия gates и точного ACK

1. Повторно read-only сверить всю job definition и service/revision/env с before-image. Drift → STOP.
2. При отдельном ACK изменить только schedule на указанный cron; timezone и прочие поля сохранить. Никакого run-now.
3. Describe after: diff только schedule; дождаться естественных runs и проверить audit completeness, отсутствие duplicates, latency, WB429 и Telegram failures.
4. Rollback scope для будущего ACK: только вернуть `0 8,11,14,17,20 * * *`; не менять retries/headers/service/env и не запускать job.

Не выполнялись scheduler update/run, `/poll`, callback, WB publish, Firestore/BigQuery mutations. Metadata evidence — `SCHEDULER_READ_ONLY_EVIDENCE_2026-10-04.json`.
