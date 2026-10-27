# Mig1b-0 — подготовка боевого контура остатков WB (Draft, без эффекта на production)

Основа — план `docs/ops/MIG1B_STOCKS_CUTOVER_PLAN.md` (PR #381).
Этот PR меняет только исходники. **Ни merge, ни CI ничего не применяют в production.**

## Что меняется в исходниках
| Файл | Изменение |
|---|---|
| `cloud/src/loaders/stocks/targets.ts`, `index.ts` | Проверка «контур ↔ таблицы» **до любой записи** и до WB API. Shadow пишет только в `__CR`. Prod — ровно в `RAW_WB_STOCKS` / `WB_STOCKS_SNAPSHOTS` / `RAW_WB_STOCKS_T5`, с `STOCKS_T5_MODE=on`. Иначе — `STOCKS_TARGET_MISMATCH`, и не тронуты ни RAW, ни manifest. Закрывает ловушку: у prod-Job env пустой, а без него загрузчик взял бы `__CR` по умолчанию. |
| `infra/terraform/cloud_run_jobs.tf` | `wb_stocks_prod`: `args` `["noop"]` → `["stocks"]`. `local.prod_stocks_env` используется только при CREATE: у существующего Job env в `ignore_changes`. В `ignore_changes` добавлены `client` и `client_version` — та же нормализация, что у shadow. |
| `infra/terraform/bigquery.tf` | `sa-loaders-prod` получает `roles/bigquery.dataEditor` ровно на 3 таблицы Apps Script: `RAW_WB_STOCKS`, `RAW_WB_STOCKS_T5`, `WB_STOCKS_SNAPSHOTS`. |
| `.github/workflows/deploy-prod.yml` | Новый вход `only_job`: `all` (по умолчанию, прежнее поведение) или `wb-stocks-prod`. `wb-stocks-prod` выкатывает **только этот Job**: образ = текущий образ `wb-stocks-shadow`, боевой env остатков, readback. Workflow не запускает Job, не трогает Scheduler и `args`. WIF не меняется: привязка deployer к `deploy-prod.yml@main` уже есть. |
| `tools/stocks_prod_deploy_check.py` | Pre (до любого update):<br>• digest = образ shadow, из реестра проекта;<br>• **происхождение** — `sha256(манифест) = digest`, `sha256(конфиг) = manifest.config.digest`, метка `git-sha` конфига = `source_git_sha` (её ставит `deploy-shadow.yml` при сборке);<br>• у текущего `wb-stocks-prod` — проект (`namespace` = номер проекта, `GCP_PROJECT_ID`), `BQ_RAW_DATASET=wb_raw`, SA = полный email `sa-loaders-prod@<проект>.iam.gserviceaccount.com`.<br><br>Post: те же проект / датасет / SA, а также образ, env, неизменные `args`; **ни один другой Job не изменился** (образ, generation, env, args).<br>Формат — живой JSON `gcloud run jobs describe/list --format=json`; проверено read-only 27.10. Тег AR `<sha>` доказательством не служит: повторная сборка того же SHA перевешивает тег, а метка конфига адресована самим digest. |
| `sql/health/dro1_02_probe.sql`, `dro1_03_evaluate.sql` | Полнота `stocks_snapshot` = `control_status = OK` **И** `t5_rows_written > 0`. Новая причина `T5_DETAIL_MISSING` с той же серьёзностью, что у `CONTROL_MISMATCH`. |

## Проверено до PR (только чтение, 26.10.2026)
- **Локальный targeted plan** (`-lock=false`, цели — `wb_stocks_prod` и 3 IAM): 3 create / 1 update / 0 destroy.
  - У `wb_stocks_prod` поатрибутно меняется только `args[0]`: `noop` → `stocks`.
  - IAM — `sa-loaders-prod`, `dataEditor`, три таблицы.
  - no-op — только `google_service_account.loaders_prod`. Guard OK.
- **DRO-1 на живой истории Apps Script за 60 дней:** `T5_DETAIL_MISSING` = 0. 37 снимков COMPLETE; 22 прежних `CONTROL_MISMATCH` (02–25.09) не изменились.
- **Тесты:**
  - `cloud`: остатки + `stocks_targets`;
  - `tools/tests`: `test_mig1b0_stocks_prod`, `test_dro1_health`, `test_public_repo_security`, `test_tf_plan_guard`, `test_terraform_apply_roles`, `test_autonomy_wif`.

## ⚠️ Что делает merge этого PR сам по себе
- `deploy-shadow.yml`: изменение `cloud/**` перевыкатывает образ `wb-stocks-shadow` и `unitka-engine-shadow`. Новое поведение для shadow — только проверка `__CR`, она проходит.
  Но образ под приёмкой паритета сменится, поэтому **сливать после подписанного паритета** (или с явным согласием, что дни после merge считаются на новом образе).
- Terraform, DRO-1 SQL и `deploy-prod.yml` merge не запускает: все три — ручные и под ACK.

## Квалификация образа перед продвижением (обязательна)
Паритет Mig1c (24–28.10) принят на образе `ec0bce74…` (main 69d6926). Merge этого PR выкатит в shadow **новый** образ, и продвигать в prod разрешается только его. Порядок:
1. После merge #382 дождаться автоматического `deploy-shadow` и записать из его summary пару (**D′**, `source_git_sha`).
2. Дождаться **одного штатного** запуска `wb-stocks-shadow` (06:30 МСК) на D′. Ручной запуск не используется: суточный `snapshot_id` и guard.
3. Запуск считается чистым, если:
   - COMPLETE, `control_status = OK`, `availability_status = OK`;
   - `t5_rows_written > 0`, T5/T6 полноценные, дублей 0;
   - QUERY 1 `sql/mig1c_stocks_t5_parity.sql` за этот день — EXACT или объяснённый DATA_DRIFT (QUERY 3).
4. Только после этого — продвижение **именно D′** в `wb-stocks-prod` (шаг 1 ниже). Другой digest, в том числе более свежий от следующего merge в `cloud/**`, требует своей квалификации.
5. Пять дней паритета **не повторяются**, если изменения не затрагивают расчёты и квалификационный запуск чистый. Mig1b-0 меняет только проверку контура до записи (`targets.ts`), а расчёт T5/T6 не трогает. Если квалификационный запуск не чистый — STOP, разбор, отдельное решение владельца.

## Production-операции, требующие OWNER ACK MIG1B (в этом порядке)
День **X** — первый день после квалификации образа D′. Окно main согласовано с Heldi.

1. **X, после 06:35 МСК — `deploy-prod.yml`, `only_job=wb-stocks-prod`.** Входы: `image_digest` = **D′** (квалифицированный образ `wb-stocks-shadow`) и его `source_git_sha`. Pre-check в workflow (до update): происхождение D′ (манифест → конфиг → `git-sha`), контур текущего prod-Job'а (проект, `wb_raw`, SA). Readback в workflow:
   - образ = D′;
   - `STOCKS_*` боевые, `STOCKS_T5_MODE=on`;
   - SA prod;
   - остальные Job не изменились.
2. **X — `infra.yml` plan, затем apply** с ref `main`:
   - `targets` = `google_cloud_run_v2_job.wb_stocks_prod,google_bigquery_table_iam_member.prod_write_raw_stocks,google_bigquery_table_iam_member.prod_write_raw_stocks_t5,google_bigquery_table_iam_member.prod_write_stocks_snapshots`;
   - `expected_changes` = `google_cloud_run_v2_job.wb_stocks_prod=update,google_bigquery_table_iam_member.prod_write_raw_stocks=create,google_bigquery_table_iam_member.prod_write_raw_stocks_t5=create,google_bigquery_table_iam_member.prod_write_stocks_snapshots=create`;
   - поатрибутно: у Job только `args[0]` `noop` → `stocks`.
   Readback: `args`, IAM на 3 таблицы, Scheduler `wb-stocks-prod` в GCP — **PAUSED**.
3. **X — DRO-1 SQL** (`dro1_02_probe.sql`, `dro1_03_evaluate.sql`) — по порядку `docs/ops/DRO1_DETECTION_ALERTING_2026-10-04.md`. После этого ручной `CALL` детектора и проверка, что `stocks_snapshot` COMPLETE на снимке Apps Script дня X.
4. **Ручного prod-запуска нет.** Снимок дня X уже записал Apps Script; ручной запуск дал бы второй снимок за тот же период.
5. **X, вечер (18:00 – 05:30 МСК X+1) — владелец в редакторе Apps Script:**
   - `wbStocksRemoveTrigger()`;
   - проверка только чтением `wbFinanceBackfillAuditTriggers()`: `runWbStocksSnapshot` отсутствует, прочие триггеры не тронуты.
6. **X, вечер — `scheduler-control.yml`: resume `wb-stocks-prod`.** Проверка в GCP: ENABLED, `30 6 * * *`, Europe/Moscow.
7. **X+1, 06:30 — первый боевой снимок Cloud Run.** Контроль — §4 плана Mig1b (PR #381):
   - ровно один снимок дня, Apps Script за X+1 нет;
   - COMPLETE, `control_status = OK`, `t5_rows_written > 0`;
   - `V_WB_STOCKS_*_CURRENT`, FACT, Control Tower и DRO-1 — зелёные.

## STOP и откат
**STOP при любом из событий в X+1…X+3:**
- нет COMPLETE после фактического завершения исполнения, или ERROR / `STOCKS_TARGET_MISMATCH`;
- `control_status` ≠ OK;
- `t5_rows_written = 0` (теперь это видит и DRO-1: `T5_DETAIL_MISSING`);
- дубли;
- пустые `V_WB_STOCKS_*_CURRENT`;
- нет FACT за день;
- в prod-RAW `__CR` или чужой `source_api`.

**Откат** — по решению владельца, данные не удаляются:
1. `scheduler-control.yml`: pause `wb-stocks-prod`.
2. Владелец: `wbStocksInstallDailyTrigger()`. Если валидного снимка за день нет — однократно `runWbStocksSnapshot()`: более поздний снимок станет текущим для потребителей.
3. Строки Cloud Run в prod-RAW остаются: таблица append-only, потребители берут последний COMPLETE.
4. По желанию:
   - `args=["noop"]` через `infra.yml` с `expected_changes=google_cloud_run_v2_job.wb_stocks_prod=update`;
   - возврат DRO-1 SQL — `dro1_99_rollback.sql` / предыдущая версия файла.

Слепого повтора prod-запуска нет: суточный `snapshot_id` и guard. Повтор после ERROR — только отдельным решением.

## Что не входит
- Отключение триггера Apps Script, resume Scheduler, любой prod-запуск — только OWNER ACK MIG1B.
- Остальные потоки миграции (поставки, REF, реклама, заказы/продажи, финансы) — после безопасного cutover остатков.
