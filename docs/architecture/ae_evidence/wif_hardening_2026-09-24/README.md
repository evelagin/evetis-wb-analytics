# WIF hardening (AE_V1_RUNBOOK.md §2, фаза 1: M1 + M2) — 2026-09-24

**Итог: `WIF_HARDENING_VERIFIED`.** ACK владельца только на M1 и M2. M3 и дальнейшие шаги не
выполнялись. AE runtime, федерация Anthropic, Engineer/Reviewer, ввод в эксплуатацию, UBR-011
и наблюдатель не включались. Данные production, WB/Ozon и `sa-ct-refresh` не трогались, PR #165
не сливался.

## Предпроверки

- `origin/main` = `4d2f3fa8`, голова PR #165 = `a20503a2` (draft), `wif.tf` в рабочем каталоге
  побайтно совпадает с головой PR. В процессе ни один workflow не исполнялся.
- Живое состояние до изменений: `pre_provider.json`, `pre_policy_*.json` (с etag),
  `pre_live_snapshot.json`. Совпадает с утренним снимком `quality/autonomy/wif_live_snapshot_2026-09-24.json`.
- Модель на живом состоянии до изменений: `pre_wif_check.json` — **24/25 FAIL**;
  `pre_wif_check_phase1.json` — 24/25 FAIL, **24 утечки привилегий**.
- Откат подготовлен до мутации: `rollback.sh`.

## M1 — маппинг атрибутов провайдера

- План: `terraform_m1_plan.txt` — `0 to add, 1 to change, 0 to destroy`, только `+` трёх
  атрибутов (`workflow_ref`, `job_workflow_ref`, `tf_plan_workflow`).
- Применён сохранённый план: `terraform_m1_apply.txt` — `1 changed`, 12 с.
- Промежуточная проверка (`after_m1_provider.json`, `after_m1_wif_check.json`): 7 атрибутов,
  4 прежних без изменений, условие провайдера то же, привязки SA не менялись, легитимные A1–A5
  сохранили доступ. GCP принял CEL-выражения при обновлении.

## M2 — привязки привилегированных SA

- План: `terraform_m2_plan.txt` — `5 to add, 0 to change, 3 to destroy`, только три ресурса
  привязок. Перед применением in-flight прогонов не было.
- Применён сохранённый план: `terraform_m2_apply.txt` — `5 added, 3 destroyed`.

| SA | До (`workloadIdentityUser`) | После |
|---|---|---|
| `sa-deployer` | `attribute.repository/evelagin/evetis-wb-analytics` | `attribute.workflow_ref/…/deploy-prod.yml@refs/heads/main`, `…/deploy-shadow.yml@refs/heads/main` |
| `sa-terraform-apply` | `attribute.repo_ref/evelagin/evetis-wb-analytics@refs/heads/main` | `attribute.workflow_ref/…/infra.yml@refs/heads/main`, `…/scheduler-control.yml@refs/heads/main` |
| `sa-terraform-plan` | `attribute.repository/evelagin/evetis-wb-analytics` | `attribute.tf_plan_workflow/infra.yml` |

Файлы: `after_m2_policy_*.json`, `after_m2_live_snapshot.json`. Больше ни у одного SA проекта нет
федеративных привязок.

## Проверка на живой конфигурации

`python -m tools.autonomy.wif_check --live --phase wif-hardening` → **код 0, 25/25 PASS, утечек 0**
(`after_m2_wif_check_live_phase1.json`).

`python -m tools.autonomy.wif_check --live` (полная целевая матрица) → 21/25
(`after_m2_wif_check_live.json`). Четыре FAIL (A6, A7, B1, B2) означают одно: ожидался
`sa-ae-reader`, получено «ничего», утечки нет. Этот SA создаёт M3, который не разрешён. Режим
`--phase wif-hardening` снимает только это ожидание и сам падает, если `sa-ae-reader` появится
раньше времени. Тесты: `test_phase1_*` в `tools/tests/test_autonomy_wif.py`.

| Требование | Случай | Результат на живой конфигурации |
|---|---|---|
| `deploy-prod.yml@main` получает только deployer | A1 | `['sa-deployer']` |
| `deploy-shadow.yml@main` — по политике | A2 | `['sa-deployer']` |
| `infra.yml@main` — только Terraform-идентичности | A4 | `['sa-terraform-apply', 'sa-terraform-plan']` |
| Engineer не получает deployer, apply, plan | B1 | `[]` |
| Reviewer не получает ни одной | C1 | `[]` |
| ветки кандидатов не получают ничего | D1–D6 | `[]` |
| двойник или изменённый путь | E1, E2, E9 | `[]` |
| привилегированный файл с не-main ref | D1–D3, E3, E4 | `[]` |
| вызов привилегированного файла как переиспользуемого | E5, E6 | `[]` |

## Реальная авторизация (обмен токена GitHub → GCP)

- `infra.yml action=plan` из `main` (run 35967352483): авторизация `sa-terraform-plan` через
  новый `tf_plan_workflow` — **success**.
- `infra.yml action=plan` с feature-ветки `arch/autonomous-engineering-v1` (run 35967355482):
  первая попытка упала на блокировке state — параллельно шёл первый прогон, авторизация до GCS
  прошла. Повтор — **success**, `No changes`.

Это доказывает, что GCP вычисляет новое CEL-выражение так же, как модель, и что легитимный infra
plan не заблокирован. Положительный обмен для `sa-deployer` и `sa-terraform-apply` не выполнялся:
его дал бы только реальный deploy или apply. Для них доказательство — модель на живой
конфигурации и точные строки привязок в политиках.

Реальные отрицательные пробы — подменённые `deploy-prod.yml`/`infra.yml` на ветке не из `main` и
на `ae/*`, только запрос токена и HTTP-статус — **не выполнены**. Инструмент автоматического
режима заблокировал запрос токенов привилегированных SA. Запускать их или нет — решение владельца.

## Terraform state

Локальный targeted plan по четырём ресурсам после применения — `No changes`
(`terraform_post_plan.txt`). State соответствует конфигурации ветки PR #165.

## Побочный эффект (важно)

В `main` лежит **старый** `wif.tf`. Plan из `main` показывает откат маппинга (run 35967352483:
`- attribute.job_workflow_ref / tf_plan_workflow / workflow_ref -> null`) и для привязок — возврат
широких. Пока PR #165 (или хотя бы его `wif.tf`) не в `main`, **нельзя** применять из `main` ни
провайдер, ни `*_wif`: откат маппинга лишит deploy-* и infra авторизации (новые привязки
ссылаются на эти атрибуты), а откат привязок вернёт риск S1. Целевые apply других ресурсов
(Unitka и т.п.) это не затрагивает. Полный apply из `main` запрещён `CLAUDE.md` и без этого.

## Откат

`rollback.sh`: вернуть три старые привязки, убрать пять новых, вернуть маппинг из 4 атрибутов.
Не выполнялся: проверка зелёная.
