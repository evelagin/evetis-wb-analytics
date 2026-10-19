# Оповещения арендатора client_001 (Heldi)

Корень Terraform: `infra/tenant-monitoring/` (отдельно от `infra/tenant`, см. `versions.tf`).
Проект: `mpa-t-client-001`. Канал: почта владельца (адрес передаётся при apply, в Git его нет).
Автоматического восстановления и автоматической паузы нет.

## Сигналы

| Политика | Условие | Письмо |
|---|---|---|
| Контроллер в STOP | stdout контроллера `status=STOPPED` за 10 мин | только на открытие |
| Scheduler не запускает контроллер | ≥ 2 отказа `tenant-backfill-tick` за 15 мин | открытие и закрытие |
| Нет успешных завершений контроллера | 30 мин без `completed_execution_count{result="succeeded"}` | открытие и закрытие |
| Данные сущности не обновлены 24 ч | нет сертификата суток (`RECONCILED_BOUNDARY`, `current_complete=true`) по сущности 24 ч, повтор 30 мин | открытие и закрытие, по инциденту на сущность |
| Проверка доставки | запись владельца в журнал `tenant-alert-delivery-test` | только на открытие |

- Одиночный штатный WAIT (`WAITING_READ_RETRY`, `WAIT_ACTIVE_CONTROLLER`) и одиночный 429 инцидента не открывают:
  соседние тики завершаются успешно, а 429 виден только как отсутствие сертификата суток.
- PromQL-инцидент открывается один раз и закрывается сам через 270 с после того, как условие
  перестало выполняться. Повторных писем на тиках нет.
- Окно свежести 24 ч — это `snapshot_sla_seconds = fbo_sla_seconds = 86400` политики суточного контура.
  Пользовательские лог-метрики PromQL видит не дальше 25 ч: 24 ч окна и 30 мин повтора укладываются в этот предел.
- Плановая или аварийная пауза `tenant-backfill-tick` откроет «Нет успешных завершений». Это ожидаемо;
  инцидент закроется после возобновления.
- STOP-инцидент закрывается и при паузе тика, хотя STOP при этом не разрешён. Поэтому письма о его
  закрытии нет. STOP снимается только новым OWNER ACK.

## Порядок включения

1. `terraform init -backend-config=bucket=mpa-platform-tfstate-777428383056 -backend-config=prefix=monitoring/tenants/client_001`
2. `terraform plan -var tenant_id=client_001 -var project_id=mpa-t-client-001 -var alert_email=<почта владельца> -out=alerts.plan`
   Ожидается: 11 to add (guard, канал, 4 лог-метрики, 5 политик), 0 to change, 0 to destroy.
3. `terraform apply alerts.plan`
4. Проверка доставки:
   `gcloud logging write tenant-alert-delivery-test '{"test_id":"<YYYYMMDD-HHMM>"}' --payload-type=json --project=mpa-t-client-001`
   Доставка доказана только подтверждением владельца: пришло письмо с этим `test_id`.
   Само создание правила доставку не доказывает.
5. Не раньше чем через 24 ч после шага 3 чтением убедиться, что у метрики `tenant_current_certified`
   есть точки для catalog, stocks, prices, finance_accrual. Затем тот же plan/apply с `-var freshness_enabled=true`
   (ожидается 0 to add, 1 to change).
   При включении сразу откроются инциденты по сущностям, которые сейчас действительно не обновляются:
   ads_campaigns, ads_expense_daily, ads_sku_daily, fbo_postings.

Откат: `terraform destroy` этого корня. Загрузку данных, контроллер и расписания он не затрагивает.
