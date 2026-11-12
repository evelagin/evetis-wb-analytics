# РНП владельца — доступ учётки сайта к `analytics_share` (узкое исключение из T6)

Статус: код, Terraform и тесты. **Реальных прав не выдано**: в живом реестре `rnp_access` отсутствует
(= `NONE`). Включение — отдельный однострочный PR реестра (P1b) и `apply` арендатора по OWNER ACK
«РНП Heldi в облаке».

## Зачем

Веб-РНП Heldi (Cloud Run `heldi-rnp` за IAP, вход только владельца; код — приватный репозиторий
`rnp-heldi`, собственное состояние `rnp/client_001`) читает **только** `analytics_share` от имени
`sa-rnp-web@mpa-t-client-001.iam.gserviceaccount.com`. ACL датасетов арендатора авторитативный
(`infra/tenant/main.tf`): доступ можно выдать только здесь. Вью `analytics_share` читают `ozon_mart` и
`tenant_ops`, поэтому нужны те же авторизованные датасеты, что и в T6.

## Что именно выдаётся (`rnp_access.mode = OWNER_RNP_SERVICE`)

| Объект | Изменение |
|---|---|
| `analytics_share` | + READER `sa-rnp-web@<проект арендатора>` |
| `ozon_mart` | + авторизованные датасеты VIEWS: `analytics_share`, `tenant_ops` |
| `tenant_ops` | + VIEWS: `analytics_share` |
| `ozon_raw` | + VIEWS: `ozon_mart` |
| `ref` | + VIEWS: `ozon_mart`, `tenant_ops` |

Итого план арендатора: **5 датасетов in-place, 0 add, 0 destroy**; 1 READER + 6 рёбер. Рёбра — ровно
`client_access.AUTHORIZED_VIEW_EDGES` (сверяются с SQL пакета); при одновременно включённом T6 не дублируются.

Не выдаётся: ролей проекта (роль запуска запросов `rnpHeldiQueryRunner` — в Terraform РНП), прав на
`ozon_raw`/`ref`/`ozon_mart`/`tenant_ops`/`tenant_locks` напрямую, клиентских идентичностей, Cloud Run, секретов.

## Почему это не ослабление T6

- `client_access.principal` не менялся: клиентские получатели — только `user:`/`group:`, SA отвергаются.
- Исключение — отдельный блок реестра с одним допустимым значением: учётка `sa-rnp-web` **своего** проекта
  арендатора, только арендаторы из `rnp_access.ALLOWED_TENANTS = ("client_001",)`.
- Проверки на трёх уровнях: схема и `validation.py` (реестр), `variables.tf` (Terraform: точное имя учётки,
  датасет `analytics_share`, рёбра только из внутренних датасетов), `plan_scan.py` (ACL каждого датасета ровно
  равен контракту; лишняя запись, другая учётка, WRITER, `tenant_locks`, лишнее ребро — отказ).

## Тесты

`tools/tests/test_tenancy_rnp_access.py`, `infra/tenant/tests/rnp_access.tftest.hcl`: только эта учётка;
ни одной клиентской идентичности и роли проекта; дельта контракта — только `rnp_access`; дельта ACL — ровно 5
датасетов и 7 записей; сканер принимает ровно эту форму и отвергает 9 видов расширения.

## Откат

Удалить `rnp_access` из `tenants/client_001/tenant.json` → план арендатора (5 датасетов in-place, записи
удаляются) → `apply` по ACK. Рёбра остаются, только если включён T6 (`bi_access = ANALYTICS_SHARE_VIEWER`).
