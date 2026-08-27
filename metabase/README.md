# Metabase — снимок для аварийного восстановления

Каталог хранит **версионируемые определения** управленческих объектов Metabase.
Это второй, независимый слой восстановления: первый — физическая копия application DB,
которая в Git **не хранится**.

Снимок снят с `http://localhost:3000`, Metabase **v0.63.15.1** (OSS, 2026-08-25, `ab5ffba`).
Актуальный состав, идентификаторы, связи и контрольные суммы — в `manifest.json`.

## Что здесь лежит

```
metabase/
  README.md          — этот файл
  manifest.json      — состав снимка, ID, связи, sha256 каждого JSON
  collections/       — 3 коллекции: 5 EVETIS Analytics, 6 «01 Executive», 7 «02 SKU Performance»
  dashboards/        — 2 дашборда: 2 Executive, 3 SKU Performance
  cards/             — 32 карточки, 40–71
```

Имена файлов включают **числовой ID**: `card-42-zakazy-sht.json`, `dashboard-3-evetis-wb-sku-performance.json`.
Полагаться на транслитерированную часть имени нельзя — она косметическая; идентификатором
служит ID, и он же продублирован внутри JSON и в `manifest.json`.

## Чего здесь НЕТ и не будет

* реквизитов подключения к BigQuery (`details`, ключи сервисного аккаунта);
* API-ключей, паролей, токенов, сессий;
* пользователей и прав;
* самих данных — только определения запросов и оформления.

Из выгрузки удалены волатильные поля (`updated_at`, `view_count`, `last_used_param_values`
и другие; полный список в `manifest.json` → `excluded_volatile_fields`), иначе каждый
повторный экспорт давал бы шум в diff. Смысловая часть — `dataset_query`,
`visualization_settings`, `parameters`, раскладка `dashcards` — сохранена полностью.

---

## PHYSICAL RESTORE — копия application DB

**Восстанавливает Metabase целиком**: объекты, пользователей, права, историю, настройки.

Копия лежит **вне репозитория**: `~/Backups/evetis-metabase/metabase-h2-<timestamp>.tar.gz`
плюс файл `.sha256` рядом. В Git архив не коммитится.

Снятие копии (только при остановленном контейнере — H2 нельзя копировать под записью):

```bash
docker stop metabase
docker run --rm -v metabase-data:/data:ro -v "$HOME/Backups/evetis-metabase":/backup alpine \
  tar czf "/backup/metabase-h2-$(date +%Y%m%d-%H%M%S).tar.gz" -C /data .
docker start metabase
```

Восстановление:

```bash
shasum -a 256 -c metabase-h2-<timestamp>.tar.gz.sha256   # сверить целостность
docker stop metabase
docker run --rm -v metabase-data:/data -v "$HOME/Backups/evetis-metabase":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/metabase-h2-<timestamp>.tar.gz -C /data'
docker start metabase
```

Простой при снятии копии — порядка минуты. BigQuery, Cloud Run-загрузчики и Cloud Scheduler
не затрагиваются: Metabase только читает данные и в конвейере не участвует.

---

## LOGICAL REBUILD — пересборка из JSON

**Это источник истины для реконструкции, а не восстановление одной командой.**

Проверено на этой сборке: команд импорта у `mb` CLI нет; Enterprise-механизмы недоступны —
`/api/ee/serialization/export` отдаёт **HTTP 404**, `mb git-sync` — `Not found:
/api/ee/remote-sync/current-task`. Значит автоматического roundtrip «экспорт → импорт»
в OSS не существует, и обещать его нельзя.

Что доступно: `mb collection create`, `mb card create`, `mb dashboard create` — то есть
пересборка выполняется скриптом, который читает эти JSON и создаёт объекты заново.

Ограничения, которые надо учитывать при пересборке:

1. **ID присваивает сервер.** Восстановленные карточки почти наверняка получат другие
   номера. Значит `parameter_mappings` и `dashcards.card_id` в дашбордах нужно
   переотобразить со старых ID на новые — карта старых связей есть в
   `manifest.json` → `dashboard_to_cards` и `card_to_collection`.
2. **Порядок обязателен:** коллекции → карточки → дашборды.
3. **Field-фильтры ссылаются на числовые ID полей Metabase** (`["field", {}, 983]`).
   Эти ID зависят от синхронизации схемы БД и на чистой инсталляции будут другими:
   после подключения BigQuery нужно выполнить `mb db sync-schema <id>` и переотобразить
   `template-tags[].dimension`.
4. **Подключение к BigQuery создаётся руками** — реквизитов здесь нет намеренно.

Поэтому при полной потере машины правильный порядок такой: сначала пробовать
PHYSICAL RESTORE, и только если архива нет или он повреждён — LOGICAL REBUILD.

---

## Ограничение архитектуры

Application DB работает на **H2 в файле** (`MB_DB_FILE=/metabase-data/metabase.db`).
H2 — вариант «из коробки»; сам Metabase не рекомендует его для production, поскольку
файловая БД уязвима к повреждению при некорректном завершении и не поддерживает
онлайн-копирование.

Долгосрочно application DB следует вынести в **PostgreSQL** — тогда появятся
`pg_dump` без простоя и штатная репликация. **Миграция сейчас сознательно не выполняется**
и требует отдельного согласования: она меняет способ запуска контейнера и требует
переноса данных.

До миграции минимальная дисциплина — снимать физическую копию после каждого
существенного изменения дашбордов и обновлять этот каталог тем же коммитом.
