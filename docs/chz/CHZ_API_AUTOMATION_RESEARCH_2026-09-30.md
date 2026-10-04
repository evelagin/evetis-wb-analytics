# Автоматизация «Честного знака» через API — исследование (2026-09-30)

Статус: исследование, решения владельца нет. Ничего не развёрнуто.

## 1. Вывод

Автоматизировать полный цикл (заказ кодов → нанесение → ввод в оборот → наборы → проверка
статусов) можно, и почти всё для этого уже есть на этом Mac. Единственное, что нельзя
обойти: **каждый документ подписывается УКЭП владельца**, а ключ лежит на Рутокен Lite.
Значит, автоматика работает только там, где физически вставлен токен, — на этом Mac,
а не в Cloud Run.

Рекомендуемый путь: **локальный инструмент `tools/chz/` (Python) на этом Mac**,
подпись через установленный КриптоПро CSP, БЕЗ облачного компонента. Готовые сервисы
(SelSup и др.) — запасной вариант, если подпись из командной строки не заработает.

⚠️ По правилу проекта (CLAUDE.md, F-18) новые компоненты с правом ЗАПИСИ во внешние
сервисы не добавляются без отдельного решения владельца. ГИС МТ — внешний
государственный реестр, запись в него необратима. Запуск инструмента — только по ACK.

## 2. Что проверено на этом Mac (факты)

| Что | Результат |
|---|---|
| КриптоПро CSP | Установлен: `/opt/cprocsp/bin` (`cryptcp`, `csptest`, `certmgr`), CSP 5.0.13800, macOS ARM64 |
| Общая лицензия CSP | **Истекла** (`License is expired`), `cryptcp` отказывает на старте |
| Сертификат УКЭП | ЕЛАГИНА Н.А., УЦ ФНС, действует 31.08.2026 – 30.11.2027, SHA1 `89055ada…1bbf` |
| Носитель | `SCARD\rutoken_lt_433b4b8f` (Рутокен Lite, ключ неэкспортируемый) |
| Встроенная лицензия | `Embedded License: CryptoPro CSP` — **есть в сертификате** |
| Браузерный плагин | `CryptoPro_ECP.app` установлен — им и подписывается ЛК сегодня |
| pycades | не установлен; для macOS официальной сборки нет — используем CLI |
| Интеграции ЧЗ в репозитории | нет |

Открытый вопрос №1: сработает ли встроенная лицензия в командной строке.
`cryptcp` проверяет лицензию до выбора сертификата и падает. `csptest -sfsign`
работает напрямую с контейнером — нужен один тест с токеном и PIN (см. §6, шаг 1).
Если нет — годовая лицензия CSP на одно рабочее место стоит ~1 850 ₽, бессрочная ~3 700 ₽
(прайс cryptopro.ru; проверить актуальность).

Команда подписи (отсоединённая CMS, base64) — кандидат:

```
/opt/cprocsp/bin/csptest -sfsign -sign -detached -base64 -add \
  -my 89055ada4e246b343790f541fec9c94469f21bbf -password <PIN> \
  -in body.json -out body.sig
```

## 3. Как устроено API (сверено с первоисточниками)

Два разных API, два разных входа:

| | True API (ГИС МТ) | API СУЗ 3.0 |
|---|---|---|
| Адрес | `https://markirovka.crpt.ru/api/v3/true-api` | `https://suzgrid.crpt.ru/api/v3` |
| Что делает | ввод в оборот, наборы, вывод из оборота, статусы кодов и документов | заказ кодов, выдача кодов, отчёт о нанесении |
| Вход | `GET /auth/key` → подписать `data` УКЭП (присоединённая CAdES-BES, base64) → `POST /auth/simpleSignIn {uuid, data, unitedToken: true, inn}` → токен, ≤10 ч | тот же токен + `omsId`; подписываемые POST несут `X-Signature` |
| Подпись документов | отсоединённая подпись **байтов документа**, base64 в поле `signature` | отсоединённая подпись **точных байтов тела** запроса |

Схема токена: с **30.09.2026** обязательна новая логика UUID (пара из `/auth/key`);
JWT поддерживается до конца 2026. Начинать сразу с новой схемы.

Операции:

- **Заказ кодов** — `POST /order?omsId=…`: `productGroup`, `products[{gtin, quantity,
  serialNumberType: OPERATOR, cisType: UNIT|SET, templateId}]`,
  `attributes.releaseMethodType: PRODUCTION | IMPORT`. Лимиты: ≤10 GTIN в заказе,
  ≤100 активных заказов, коды хранятся в СУЗ 30 дней (потом сгорают, деньги не возвращают).
- **Получение кодов** — `GET /codes?omsId=…&orderId=…&gtin=…&quantity=…`.
- **Отчёт о нанесении** — `POST /utilisation?omsId=…`: для косметики обязательны дата
  производства (не будущая), срок годности, содержание этилового спирта (0, если нет).
- **Ввод в оборот «Производство РФ»** — `POST /lk/documents/create?pg=chemistry`,
  `type: LP_INTRODUCE_GOODS`, `document_format: MANUAL` (JSON), внутри — то же, что мы
  отправляли вручную 29.09: `participant_inn, producer_inn, production_type: OWN_PRODUCTION,
  products[{uit_code, tnved_code, production_date, certificate_document_data[{certificate_type:
  CONFORMITY_DECLARATION, certificate_number, certificate_date}]}]`.
- **Формирование набора** — тот же метод, `type: SETS_AGGREGATION`, тело
  `{participantId, aggregationUnits[{unitSerialNumber, sntins[…]}]}`. Поле
  `aggregationType` внутрь **не** класть (в ручном экспорте ЛК оно есть, в API — тип документа).
- **Статус кодов** — `POST /cises/info?pg=chemistry`, ≤1000 кодов, ≤50 запросов/с.
- **Статус документа** — `GET /doc/{docId}/info?pg=chemistry`; HTTP 200 при создании ≠ обработан.
- **Списание/вывод** — `WRITE_OFF`, `LK_RECEIPT`; для косметики списание нанесённых кодов
  в ЛК недоступно (проверено 29.09), через API не проверялось.

Правила набора (официально, markirovka.ru «Формирование наборов», косметика):

1. Вложения все **«Нанесён»** → у всех один тип эмиссии, набор «Нанесён» с тем же типом;
   после формирования нужен отдельный ввод набора в оборот.
2. Вложения все **«В обороте»** → набор «Нанесён» и **«Произведён в РФ»**; после
   формирования набор сам становится «В обороте».
3. Смешивать статусы вложений нельзя. Состав и количество — строго по карточке НК.

Это объясняет ошибку 29.09: вишня «В обороте», крем для рук из нового заказа, скорее всего,
«Нанесён» (смешение), и/или коды набора …130 выпущены как «Ввезён в РФ» (правило 2).

Песочница: `https://markirovka.sandbox.crptech.ru` (ГИС МТ), `https://suz.sandbox.crptech.ru`
(СУЗ). Регистрация отдельная, с УКЭП; реквизиты прод-контура туда не переносить.

Документация: полное «Описание True API» и «API СУЗ 3.0» — только в ЛК
(«Помощь» в ГИС МТ, «Руководства» в СУЗ). Публично — устаревшие копии.

Подключение СУЗ: «Управление заказами → Устройства → Создать устройство»
(тип АСУ ТП, режим «Автоматически») → `omsConnection` в столбце «Идентификатор
подключения»; `omsId` — идентификатор СУЗ участника там же.

## 4. Открытые библиотеки

Ни одной поддерживаемой полной. Python `jqxl/py_cz_api` — заброшен; PHP `kilylabs/true-api-cli`
и C# `FairMark/FairMarkClient` — незавершённые; MCP-обёртки (`ilyautov/chestny-znak-mcp-ru`,
`theYahia/chestnyznak-mcp`) — только чтение, не верифицированы. Писать своё: HTTP-слой
тривиален, вся сложность в подписи и в JSON-схемах конкретной товарной группы.

## 5. Готовые сервисы (запасной путь)

SelSup — единственный с подтверждённым собственным API маркировки (`api.selsup.ru/marking`),
поле `aggregateCodeId`; поддержка `SETS_AGGREGATION` для косметики не подтверждена.
МойСклад — JSON API 1.2 есть, операций эмиссии/наборов в нём не подтверждено.
GetMark, Контур.Маркировка, Клеверенс, 1С — без подтверждённой матрицы функций.
Перед выбором запросить письменно: косметика, наборы, авто-подпись без ручного
подтверждения, стоимость для ИП. Ни один не снимает необходимость УКЭП.

## 6. План внедрения (по шагам, каждый — по ACK)

1. **Тест подписи** (5 минут, с токеном и PIN): `csptest -sfsign` на тестовом файле.
   Проверка: подпись создаётся, `-verify` проходит. Если нет — купить годовую лицензию CSP.
2. **Скачать официальные PDF** из ЛК: «Описание True API» (Помощь) и «API СУЗ 3.0»
   (Руководства) → `docs/chz/vendor/` (в git не коммитить, если запрещено лицензией ЦРПТ).
3. **Устройство в СУЗ**: создать, записать `omsId` и `omsConnection` в `.env` (не в git).
4. **Песочница**: регистрация ИП в sandbox, карточки НК, устройство. Весь код сначала там.
5. **MVP `tools/chz/`** (Python, только этот Mac):
   `auth` → `order` → `codes` → `utilisation` → `introduce` → `set` → `status`.
   Обязательно: журнал каждого шага (JSONL: заказ, коды, документ, статус); dry-run;
   предпроверка перед отправкой (статусы вложений через `cises/info`, состав по НК,
   даты); опрос статуса документа; идемпотентность (не создавать документ повторно
   после таймаута); PIN — из Keychain, не из аргументов командной строки.
6. **Первый боевой прогон** — один заказ на 1 GTIN, по ACK, с ручной проверкой в ЛК.

Ограничения: работает, пока Mac включён и токен вставлен; каждая подпись — ввод PIN
(политику «PIN на сессию» настроить в КриптоПро); Cloud Run исключён без переноса ключа
или облачной подписи (КриптоПро DSS — отдельный проект и деньги).

## 7. Источники

- markirovka.ru: «Формирование наборов (Косметика…)», «Подача отчёта о нанесении
  (Косметика…)», «Ограничения при заказе и получении кодов», «Как добавить устройство в
  интерфейсе СУЗ», «Подпись, передаваемая в X-Signature, не проходит проверку»,
  анонсы единого токена UUID (05.06, 30.07, 05.08.2026, перенос на 30.09.2026).
- честныйзнак.рф: cosmetics/instructions, cosmetics/registration (адреса sandbox).
- selsup.ru «API Честного ЗНАКа 2026» (10.09.2026), habr.com/1058282 (CAdES-BES,
  `unitedToken`, `inn`), cryptopro.ru прайс и KB-388, GitHub-репозитории из §4.
- Локальные проверки: `certmgr -list`, `csptest -keyset`, `cryptcp` (30.09.2026).

## 8. Дополнение 30.09, вторая половина дня: по официальным документам из ЛК

Документы лежат в `docs/chz/vendor/` (в git не попадают, см. `.gitignore`):
«Описание True API» (docs13, 937 стр.), «API СУЗ 3.0» (761 стр., ред. 05.08.2026, СУЗ 5.01),
«Инструкция по получению динамического клиентского токена» (docs14),
«Планируемые изменения в True API» (docs12, v.611.0 от 29.09.2026), прочие.

**Что подтверждено на этом Mac.** Тест подписи прошёл: `csptest -sfsign -sign -detached
-base64 -add -addsigtime -my <thumbprint>` → `-verify`: «Detached Signature was verified OK».
Встроенной лицензии сертификата ФНС хватает, `cryptcp` не использовать. Владелец дал ACK на
канал записи в ГИС МТ (F-18) и создал устройство в СУЗ. Реквизиты — `~/.config/evetis-chz/.env`.

**Что меняется в плане.**

1. **Наборы делать через `AGGREGATION_DOCUMENT`, а не `SETS_AGGREGATION`.** В «Планируемых
   изменениях» v.611.0: «Запланировано отключение документа „Формирование набора“
   (SETS_AGGREGATION). Формирование наборов будет возможно только через документ
   „Формирование упаковки“ (AGGREGATION_DOCUMENT)» — все группы, «в ближайшие 1–2 месяца».
   Текущий `AGGREGATION_DOCUMENT` уже умеет наборы: `aggregationUnits[{unitSerialNumber,
   unitSerialNumberType: PRODUCT_SET, aggregationType: SET, sntins[…]}]`. Правила те же:
   вложения все «Нанесён» (один тип эмиссии, набор «Нанесён») либо все «В обороте» (набор
   «Нанесён» и `LOCAL` — «Произведён в РФ», вводится сам). Смешивать нельзя.
   Там же: отключаются «Перемаркировка» и «Трансформация упаковки» для всех групп.
2. **Авторизация.** True API: `GET /auth/key` → `{uuid,data}` → присоединённая CAdES-BES подпись
   `data` → `POST /auth/simpleSignIn {uuid, data, unitedToken: true}` → `{token, uuidToken,
   expireDate}`. Для СУЗ: `POST /auth/simpleSignIn/{omsConnection}` → `clientToken` (10 ч), один
   токен на устройство, повторный запрос гасит предыдущий; после первого динамического токена
   статические отключаются навсегда.
3. **СУЗ, косметика (`chemistry`):** длина серийного номера 6 (SELF_MADE — 5);
   `releaseMethodType`: `PRODUCTION` («Произведён в РФ») / `IMPORT`; атрибуты заказа —
   таблицы 49–50 (`attributes` объектов Order/OrderProduct), расширение отчёта о нанесении —
   раздел 4.4.11.1.19 (дата производства, срок годности, этиловый спирт). Извлечь при реализации.
   `X-Signature` обязателен для подписываемых POST (заказ, отчёт о нанесении), подписываются
   точные байты тела; для GET — путь с query.
4. **Статусы:** коды — `POST /api/v3/true-api/cises/info?pg=chemistry` (≤1000 в массиве);
   документ — `GET /api/v4/true-api/doc/{docId}/info?pg=chemistry`.
5. **Ввод в оборот** `LP_INTRODUCE_GOODS` — JSON совпадает с тем, что ЛК экспортировал 29.09
   (`production_type: OWN_PRODUCTION`, `certificate_document_data` на каждый код).

**Следующие шаги (по порядку).** (1) Владелец вписывает `CHZ_OMS_ID` и `CHZ_OMS_CONNECTION` в
`~/.config/evetis-chz/.env`. (2) Регистрация ИП в песочнице `markirovka.sandbox.crptech.ru`
(нужна УКЭП, отдельные карточки НК и устройство СУЗ). (3) `tools/chz/` MVP: `auth` → `status`
(только чтение) → `order` → `codes` → `utilisation` → `introduce` → `set`. Первые два шага —
read-only, их можно гонять на проде сразу после (1).


## 9. Operational CURRENT check: остаток 56 наборов (OWNER TASK aed8115e)

**Historical audit snapshot. OWNER clarification b2e4543e от 02.10.2026 в разделе11
отменяет physical child pairing/relabeling assumptions этого раздела для новой allocation
50 наборов. Snapshot статусов сохраняет свою дату; прежние blockers не являются текущим gate.**

Обновлено по CURRENT readback 2026-10-01 **23:16:03–23:16:08 Europe/Moscow**
(20:16:03–20:16:08 UTC), OWNER TASK `aed8115e-adc6-4954-aea6-2bfb767f43e0`.
Это отдельная operational task; V2/R3 не изменяется и не получает acceptance evidence.
**READBACK COMPLETE; introduction preview BLOCKED; business result NOT_EXECUTED.**

### Historical exact scope / provenance


Восстановлены 56 уникальных exact КИ в памяти процесса из существующих private файлов
`Поставки_разбор/Ozon_2026-09-24/set_codes_from_pdf.json` и
`Поставки_разбор/Щеглово_2026-09/pdf_codes.json`. Каждый КИ нормализован до GS, длина 24;
дублей нет. В Git/context полные КИ не записываются. Alias = SET- + первые 12 hex SHA256(КИ).
Exact scope проверяется локально по source PDF name, ordinal в `codes` и alias; источники не изменены.

Historical scope SHA256 (canonical JSON sorted exact КИ list): `5ead3b0bd52a633ee68b4d3a55b5bf0e675925c519dc90f684617f12329008cc`.
50/6 подтверждены как **LEGACY-HISTORICAL**, не CURRENT:

| GTIN | Historical count | Источник / причина |
|---|---:|---|
| 04619689656130 | 27 = 17 Ozon + 10 WB | Ожидание атрибута 23821=2, moderation/signature; feed 473238228 |
| 04619689656147 | 23 = 13 Ozon + 10 WB | Ожидание атрибута 23821=2, moderation/signature; feed 473388492 |
| 04619689656222 | 1 Ozon, Пермь №5 | Скан сыворотки не найден в ГИС МТ |
| 04619689656093 | 1 Ozon, Невинномысск №4 | В строке ФФ тоник УВЛ вместо АКНЕ |
| 04619689656185 | 4 Ozon, Тюмень №1–4 | Отсутствуют сканы четырёх тоников УВЛ |

Для …222 excluded identity дополнительно доказана разностью 12 PDF parents и 11 parents
`Ozon_2026-09-24/set_222_doc_11.json` (ровно один остаток, Пермь №5).
…093 Невинномысск №4 прямо указан во вчерашней session line 1824; …185 Тюмень — все четыре
parents соответствующего PDF, session lines 2381/2393. Нельзя выбирать исключения по одному
лишь suffix/количеству или считать отсутствие local document отсутствием операции ЧЗ.

Вчерашняя Claude session `2179d8ed-27c8-4ce5-b83d-b23eed6aa5b0`: 2432 валидных records;
malformed physical lines 55–58 пропущены без исполнения. Опорные валидные physical lines
1824, 2358, 2393, 2408, 2423, 2433. Последняя запись 2026-09-30T14:17:05.069Z — historical moderation pending.
Более ранняя формулировка «…147 не исправлена» superseded: владелец отправил correction
feed 473388492 (line 2408), затем оба feeds остались на moderation (line 2433).

### CURRENT cards / moderation

Все пять карточек CURRENT `published`, `isSet=true`, signed/mark/turn flags=true
в True API и НК. Составы GTIN совпадают после zero-padding NK GTIN до 14 знаков.
Оба feeds `473238228` и `473388492` CURRENT `Signed`; shared feed проверен только
по feed-level status, без unrelated card payload. Подтверждение требуемых двух карточек
получено отдельно через feed-product и product/info: 23821 уже равен 2.
**Расхождение с historical:** прежний moderation/signature blocker для 50 снят.
В этом запуске карточки не подписывались и не изменялись.

| GTIN | Parents в scope | НК card ID | 23821 CURRENT | CURRENT flags | Состав GTIN × quantity |
|---|---:|---|---:|---|---|
| 04619689656093 | 1 | 1052072955 | 3 | published / signed / mark / turn | 04619689656000 × 1, 04619689656055 × 1, 04619689656031 × 1 |
| 04619689656130 | 27 | 1069744482 | 2 | published / signed / mark / turn | 04619689656079 × 1, 04619689656260 × 1 |
| 04619689656147 | 23 | 1069760598 | 2 | published / signed / mark / turn | 04619689656260 × 1, 04619689656062 × 1 |
| 04619689656185 | 4 | 1099695447 | 2 | published / signed / mark / turn | 04619689656048 × 1, 04619689656017 × 1 |
| 04619689656222 | 1 | 1131474489 | 1 | published / signed / mark / turn | 04619689656000 × 1 |

True API product/info observed_at: 20:16:05 UTC. NK per-card observed_at:
…093 20:16:05, …130 20:16:06, …147/…185 20:16:07, …222 20:16:08 UTC.
Некоторые карточки имеют innerUnitCount=1; это не attr 23821. Число маркированных
товаров берётся из markedProductsQuantityInSet и NK good_attrs[attr_id=23821].

### CURRENT parents / relations / classification

У всех 56 уникальных exact parents успешно получен cisInfo без per-item error:
`status=APPLIED`, `statusEx=EMPTY`, `emissionType=LOCAL`, `packageType=SET`,
`ownerInn` совпадает с participant `682962587600`, `markWithdraw=false`.
requestedCis и returned cis покрывают exact scope 56/56, без лишних/пропущенных/дублированных КИ.
cises/info: у каждого `child=[]`, parent отсутствует; aggregated/list: ровно 56 exact keys,
каждое значение `{}`. В manual для empty вложений описано `[]`; observed shape `{}`
зафиксирован как schema deviation, вывод об отсутствии зарегистрированных вложений
подтверждается независимым успешным cisInfo child=[].

| Категория | CURRENT count | Основание |
|---|---:|---|
| ALREADY_INTRODUCED | 0 | Ни у одного parent нет status INTRODUCED |
| READY_FOR_INTRODUCTION | 0 | Нет доказанного пригодного exact introduction document/child scope |
| BLOCKED | 56 | APPLIED SET без зарегистрированных вложений; нет validated exact child scope |
| UNKNOWN / REQUIRES_RECONCILIATION | 0 | Parent status/identity доказаны для всех 56 |

**BLOCKED — решение readiness gate для запрошенной операции, не server status ГИС МТ
и не доказательство отказа в отправке.** UNKNOWN=0 касается состояния parents;
пригодность children, физический состав и applicability отдельного introduction остаются UNPROVEN.
APPLIED сам по себе не является ошибкой или запретом ввода, но не доказывает готовность набора.
Historical 50 — потенциальные кандидаты на дальнейшее формирование после exact child validation,
не готовый подтверждённый payload introduction.

### Document semantics / immutable preview

Historical процесс был `AGGREGATION_DOCUMENT` с `aggregationType=SET` и
`unitSerialNumberType=PRODUCT_SET`. При пригодных INTRODUCED children LOCAL set
мог вводиться в оборот как результат формирования. Это самостоятельная business operation;
её нельзя молча подменить типом `LP_INTRODUCE_GOODS` или назвать read-only шагом.
Official local True API manual p.11 разделяет формирование упаковки и ввод в оборот;
конкретная применимость отдельного introduction к этим unformed parents не доказана.
Новый OWNER TASK прямо запрещает aggregation/set formation; allocation queue codes также не разрешён.
Поэтому formation/signature/document submission не выполнялись.

Historical …130 draft 74383dc0 непригоден: cherry children 644–660 частично использованы
в …123 (session line 2084). Нельзя переиспользовать stale child payload/pointer.
Требуются exact физический состав и новые подтверждённые child identities/availability;
никаких замен или резервирования production очереди в этом запуске.

Контекст прошлых batch docs, **не доказательство операции для остаточных 56**:
…222 c94e703e-f974-447d-9f0b-bfacdb824cb4; …093 Ozon dddb0822-7172-45a6-8f23-75c61a75e3d1;
…185 Ozon 4daf46cd-5dd6-4fb9-85fa-67259fe9d095. Их CURRENT status не запрашивался.
Exact external doc IDs/operation IDs для оставшихся 56 не установлены;
отсутствие локального файла не считается доказательством отсутствия попытки.
CURRENT APPLIED/empty relations доказывают наблюдённое состояние, не отсутствие pending requests.

Immutable introduction preview **не создан**, actionable approval UI **не подготовлен**:
READY=0, нет validated exact child scope, подтверждённого типа/параметров документа.
Canonical payload, payload_sha256, operation_id отсутствуют; scope hash ниже не является payload hash.
Нельзя показывать кнопку «ввести 50» или заменять её разрешением на aggregation.
Future operation result: **NOT_EXECUTED**, business submissions=0.

### Отдельные historical 6

Все шесть CURRENT APPLIED/LOCAL/SET, child=[]; карточки опубликованы/подписаны.
Historical scan/physical-content reasons не перепроверялись: это другие child objects,
не вошедшие в разрешённый bounded scope. Причины нельзя объявить устранёнными.
Owner exclusion сохраняется до отдельного решения даже при исправлении scans.

| Alias | GTIN | Supply / PDF ordinal | Historical reason | CURRENT reason | Что необходимо |
|---|---|---|---|---|---|
| SET-155986c1bdaa | 04619689656093 | Ozon, Невиномысск, №4 | Невинномысск №4: тоник УВЛ вместо АКНЕ в строке ФФ | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Проверить физический состав; правильный скан АКНЕ тоника; отдельное owner decision |
| SET-786dd317f72a | 04619689656185 | Ozon, Тюмень, №1 | Тюмень: отсутствует скан тоника УВЛ для данного набора | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Получить соответствующий скан УВЛ тоника; exact pairing с сывороткой; отдельное owner decision |
| SET-0f10b04e2b98 | 04619689656185 | Ozon, Тюмень, №2 | Тюмень: отсутствует скан тоника УВЛ для данного набора | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Получить соответствующий скан УВЛ тоника; exact pairing с сывороткой; отдельное owner decision |
| SET-9efb78a888a7 | 04619689656185 | Ozon, Тюмень, №3 | Тюмень: отсутствует скан тоника УВЛ для данного набора | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Получить соответствующий скан УВЛ тоника; exact pairing с сывороткой; отдельное owner decision |
| SET-ed67d6ab99cc | 04619689656185 | Ozon, Тюмень, №4 | Тюмень: отсутствует скан тоника УВЛ для данного набора | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Получить соответствующий скан УВЛ тоника; exact pairing с сывороткой; отдельное owner decision |
| SET-bb375fe14554 | 04619689656222 | Ozon, Пермь, №5 | Пермь №5: скан сыворотки не найден в ГИС МТ | BLOCKED: нет вложений; scan reason UNPROVEN; owner exclusion | Перескан сыворотки; разрешённый CURRENT child readback; отдельное owner decision |

Для каждой строки observed_at=2026-10-01T20:16:03+00:00; relations=20:16:04 UTC.
Exact private identity/source PDF/ordinal, historical/current reasons и evidence references
сохранены в private classification.json; full КИ в repository отсутствуют.

### Полный masked inventory 56

Exact local lookup: private historical-scope.json / classification.json либо source JSON →
PDF key → codes[ordinal-1]; вычисленный alias должен совпасть.
Для всех строк: APPLIED/LOCAL/SET, child=[], owner match; observed_at=20:16:03 UTC.
Reason R1 = no registered children / no validated exact child scope / introduction applicability unproven.
R2 = R1 + historical exclusion, separate owner decision; scan reason не перепроверен.

| Alias | GTIN | Supply | City | PDF ordinal | Historical group | CURRENT classification / reason |
|---|---|---|---|---:|---|---|
| SET-155986c1bdaa | 04619689656093 | Ozon | Невиномысск | 4 | OWNER_EXCLUDED_6 | BLOCKED / R2 |
| SET-010d0db8b637 | 04619689656130 | Ozon | Невиномысск | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-6ecb9c2a2a2e | 04619689656130 | Ozon | Невиномысск | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-468ab517b70d | 04619689656130 | Ozon | Невиномысск | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-926b33f57f40 | 04619689656130 | Ozon | Пермь | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-e71e4837130f | 04619689656130 | Ozon | Пермь | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-95ad57cd0d09 | 04619689656130 | Ozon | Пермь | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-8dce40689267 | 04619689656130 | Ozon | Тюмень | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-c8d6fa64c985 | 04619689656130 | Ozon | Тюмень | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-92b8a3e29818 | 04619689656130 | Ozon | Тюмень | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-3f54194c1a40 | 04619689656130 | Ozon | Тюмень | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-2987e4084780 | 04619689656130 | Ozon | Тюмень | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-283323443599 | 04619689656130 | Ozon | Уфа | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-802d00ccbb57 | 04619689656130 | Ozon | Уфа | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-bc7091ec7965 | 04619689656130 | Ozon | Уфа | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-b2414c6655c2 | 04619689656130 | Ozon | Уфа | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-104858fd67ea | 04619689656130 | Ozon | Уфа | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-17e27f7c0cac | 04619689656130 | Ozon | Уфа | 6 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-361b9728f552 | 04619689656130 | WB | Щеглово | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-82c2535c50c2 | 04619689656130 | WB | Щеглово | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-d39f1a962d27 | 04619689656130 | WB | Щеглово | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-31ae89c3355d | 04619689656130 | WB | Щеглово | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-bd43d17064ef | 04619689656130 | WB | Щеглово | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-ce378cfcc3c9 | 04619689656130 | WB | Щеглово | 6 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-962a31627995 | 04619689656130 | WB | Щеглово | 7 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-1dfdfb3a2850 | 04619689656130 | WB | Щеглово | 8 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-8686e50deba7 | 04619689656130 | WB | Щеглово | 9 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-0faa757f1e3d | 04619689656130 | WB | Щеглово | 10 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-20a11be565bc | 04619689656147 | Ozon | Невиномысск | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-ea57c8c9eb68 | 04619689656147 | Ozon | Невиномысск | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-2b54c28efb03 | 04619689656147 | Ozon | Невиномысск | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-07ad1f300296 | 04619689656147 | Ozon | Невиномысск | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-44507317b6af | 04619689656147 | Ozon | Невиномысск | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-a26f6ba41b2d | 04619689656147 | Ozon | Пермь | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-954ffed09d56 | 04619689656147 | Ozon | Пермь | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-7999b757c69f | 04619689656147 | Ozon | Пермь | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-9178bcb72599 | 04619689656147 | Ozon | Тюмень | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-043bcc978f09 | 04619689656147 | Ozon | Тюмень | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-c0962493ea94 | 04619689656147 | Ozon | Тюмень | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-aeed28724ab7 | 04619689656147 | Ozon | Тюмень | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-8b1f9168ba2e | 04619689656147 | Ozon | Тюмень | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-d33b05de6030 | 04619689656147 | WB | Щеглово | 1 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-c36e20836bda | 04619689656147 | WB | Щеглово | 2 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-288c85f85963 | 04619689656147 | WB | Щеглово | 3 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-480b79b298fb | 04619689656147 | WB | Щеглово | 4 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-a61ab7db7ada | 04619689656147 | WB | Щеглово | 5 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-b2445f81b5ef | 04619689656147 | WB | Щеглово | 6 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-9c00ed85dd98 | 04619689656147 | WB | Щеглово | 7 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-06aa9d7d4b9c | 04619689656147 | WB | Щеглово | 8 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-7074835c72f8 | 04619689656147 | WB | Щеглово | 9 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-e67c8e9f3ede | 04619689656147 | WB | Щеглово | 10 | HISTORICAL_CANDIDATE_50 | BLOCKED / R1 |
| SET-786dd317f72a | 04619689656185 | Ozon | Тюмень | 1 | OWNER_EXCLUDED_6 | BLOCKED / R2 |
| SET-0f10b04e2b98 | 04619689656185 | Ozon | Тюмень | 2 | OWNER_EXCLUDED_6 | BLOCKED / R2 |
| SET-9efb78a888a7 | 04619689656185 | Ozon | Тюмень | 3 | OWNER_EXCLUDED_6 | BLOCKED / R2 |
| SET-ed67d6ab99cc | 04619689656185 | Ozon | Тюмень | 4 | OWNER_EXCLUDED_6 | BLOCKED / R2 |
| SET-bb375fe14554 | 04619689656222 | Ozon | Пермь | 5 | OWNER_EXCLUDED_6 | BLOCKED / R2 |

### Private evidence / authorization / safety limits

Evidence directory: `/Users/evgenelagin/.config/evetis-chz/current-check-20261001T231601-95f19b1a`
(0700, все JSON 0600). 15 evidence files: historical-scope, auth-metadata, cises-info,
aggregated, product-info, пять nk-GTIN, два feed-ID, access-trace, classification и evidence-manifest.
Manifest фиксирует SHA256 каждого evidence file, source scope hash и helper/classifier hashes.
classification.json SHA256: `b6ba9cef799d6be466f6f791627000ba06041cd3cc0c3cc2ba61d5fc8c767678`.
Полные КИ и exact API responses находятся только в private evidence; tokens/headers/PIN туда не записаны.
Evidence — bounded observation, не atomic snapshot across APIs и не бессрочное approval.

Две initial attempts были отклонены automatic approval review до CreateProcess:
attachment ACK не признан прямым trusted transcript permission для auth/network/private writes.
Обхода не было. Затем владелец прямо подтвердил в чате bounded 56/5/2 PROD readback,
одну auth challenge подпись/token refresh и private evidence; business writes запретил.
После этого разрешённый запуск завершился: 12 HTTP requests = 2 auth + 3 True readbacks +
5 NK cards + 2 feed statuses; одна подпись только auth challenge, один auth submission.
Allowlist исключал business endpoints и redirects, detached/business signing запрещён wrapper.
Это runtime trace и ограничение helper, не общий security proof legacy runtime.
Ни SUZ, ни NK mutation/signing, ни business documents не вызывались.
True API token cache и существующий auth journal обновлены разрешённым auth mechanism;
утверждение, что всё ~/.config осталось неизменным, было бы неверным.

Initial diagnostic ошибка: shape renderer вывел 12 полных legacy КИ через JSON dictionary keys
в tool output. Это нарушение ограничения вывода identifiers; значения не повторяются и не
перенесены в Git/context. Tokens/PIN/private-key material не выводились. Renderer больше
не используется; дальнейший output — explicit safe metadata/aliases. Нельзя заявлять «утечек не было».

Git branch recovery/chz-automation-2026-10-01, HEAD b94ca335b98ce8146f925295f45d8789db5ed46c;
index/refs/commit/push/PR/deploy не изменялись. В этой задаче изменён только этот CHZ documentation
file; существующие четыре ingestion modifications и 23 untracked status entries сохранены.
Legacy CHZ scripts не изменены; queue/CSV/index/pointers не открывались writer и не изменялись задачей.
V2 не использован и не изменён. Original source JSON SHA256 совпадают с pre-readback evidence.
Это подтверждение действий данного helper; внешние concurrent changes runtime не аудировались.

### Unresolved / next owner gate

Для продолжения нужен отдельный OWNER ACK на bounded preparation/read-only validation
exact child scope для исторических 50 и решение о **формировании наборов**, если оно требуется.
Такой ACK должен назвать private child evidence/допустимые queue reads, limit и document semantics;
allocation/reservation/formation execution требуют отдельного scope, сейчас не разрешены.
Затем exact immutable formation/introduction preview, hashes, business params и новая explicit
approval только одной mutating attempt. Подмена operation type запрещена.
Шесть excluded остаются отдельными; исправленные physical scans и отдельное owner decision необходимы.
Перед future write: fresh state/relations, reconcile любые pending historical attempts, подтверждённые
children/ownership/status/absence incompatible aggregation; при ambiguity STOP без retry.
**STOP BEFORE BUSINESS WRITE.**

## 10. Exact child recovery / CURRENT validation исторических 50 (OWNER ACK 9bf06245)

**Historical forensic snapshot. Требования доказать старый physical child pairing и
трактовка 12 obsolete draft mappings как конфликта новых parents superseded разделом11.
Повторное использование consumed children по-прежнему запрещено.**

OWNER ACK: attachment `9bf06245-a8e6-4490-9a40-7c68a8f25d6b` от 02.10.2026.
Scope: только historical 50 (…130 ×27, …147 ×23); шесть excluded не включались.
Раздел 9 сохраняется как snapshot 01.10; этот раздел — отдельный более поздний child-readiness gate.
**CURRENT readback: 02.10.2026 07:25:30–07:26:01 Europe/Moscow** (04:25:30–04:26:01 UTC).
**READBACK COMPLETE; READY scope empty; business result NOT_EXECUTED.**

### Recovery / provenance / permitted boundary

Восстановлены exact draft composition для **17/50**, но доказанных пригодных exact composition
для формирования — **0/50**. Нельзя приравнивать восстановленный draft к фактическому вложению.
Два attachment JSON в legacy session physical lines 416 и 1443 содержат те же 17 …130 Ozon
parents и те же пары рук/Вишни (порядок sntins отличается). Это два snapshots одного pairing,
не независимые подтверждения physical relabeling. Sources f9408c12…json / 74383dc0…json;
удалённые Downloads не воссоздавались; восстановление — из сохранённого attachment content.
Полные identifiers/documents остаются только в private recovery.json.

Для 13 …147 Ozon полного hands/Amber pairing в разрешённых сохранённых источниках нет.
FF Amber/Cherry scans по действующему owner process не authoritative; ими missing issue не заменяется.
Для 20 WB сохранён distinct pool 20 hands scans, но точная parent assignment отсутствует.
У WB scan export explicit AI91/AI92 boundaries представлены пробелами: применяется строгое
распознавание полного формата, не произвольное обрезание КМ. Pool не распределялся и не отправлялся API.
Pool имеет 20 уникальных нормализованных КИ и не пересекается с 34 draft children.

CURRENT локальные queue pointers: Вишня 656, Амбра 413. Единственная issue manifest —
12 Cherry/12 Amber для …123; выдача для рассматриваемых 50 отсутствует в восстановленных
index/manifests. Это local evidence, не глобальное доказательство отсутствия любой external issue.
Колонка использовано в CSV пустая даже для фактически сформированных children: проверялись
выдано ФФ, issue manifest, current membership и history; одному пустому полю доверять нельзя.
Очередь читалась; issue/set-doc/dry-run commands не запускались, новые коды не назначались.

### CURRENT validation / classification

Frozen query scope: **50 parents + 34 draft children = 84 unique КИ**.
query_scope_sha256: `637274724e3e389c9d23599c14546ced936d95237b252b120ccfd10b324188dc`.
parent_scope_sha256: `79114fbaf26b77db8cc865e289368d044c3b8af1627ef9cd2203742718abc1f4`.
Пропущенных/лишних/повторных requested/returned cis нет, per-item errors отсутствуют.
cises/info и aggregated/list + product/info двух GTIN + history каждого из 84: **87 requests**,
каждый HTTP 200; history каждого содержит matching exact CIS. HTTP 200 дополнен проверкой содержания.
Истории доступны в пределах participant-visible movements; это не global pending request registry.

Все 50 parents APPLIED/EMPTY/LOCAL/SET, owner совпадает; child=[], parent отсутствует.
Все 34 проверенных children INTRODUCED/EMPTY/UNIT, owner совпадает; руки17 LOCAL, Вишня17 FOREIGN.
У 12 Вишен CURRENT parent другой — …123; у остальных 22 child текущий parent отсутствует.
aggregated/list даёт empty objects {}; сохранён ранее отмеченный shape deviation,
выводы о membership основаны на cisInfo.parent и matching history, не на empty aggregate response.
Обе карточки …130/…147 published, signed/mark/turn=true, markedProductsQuantityInSet=2.
Expected BOM подтверждён: …130 = …260 ×1 + …079 ×1; …147 = …260 ×1 + …062 ×1.
GTIN и expected child count совпадают для всех 17 восстановленных draft pairs.

| Classification | Count | Причина |
|---|---:|---|
| READY_FOR_SET_FORMATION | 0 | Нет доказанного пригодного exact child scope |
| ALREADY_FORMED | 0 | Ни у одного целевого parent нет зарегистрированных вложений |
| BLOCKED_CHILD_EVIDENCE | 38 | 5 unissued stale draft + 13 Ozon missing pairing + 20 WB missing pairing/issue |
| CONFLICT | 12 | Draft Вишни CURRENT входят в другие …123 наборы; history подтверждает prior formation |
| UNKNOWN | 0 | Проверенное parent state известно; missing composition классифицирован как evidence blocker |

Для 33 без точного pairing CURRENT child status/owner/membership **UNPROVEN**;
UNKNOWN=0 не означает, что все их необходимые вложения проверены. Для пяти unissued
Вишен …130 CURRENT status/ownership/membership допустимы, но queue/physical pairing не доказаны.

### Duplicate usage / previous operations

Внутри 17 восстановленных draft: 34 уникальных child, internal duplicate count=0.
**Глобальная безопасность reuse для всех 50 не доказана и не заявляется.**
Доказаны 12 внешних reuse conflicts: страницы Вишня 644–655 уже сформированы в …123.
CURRENT child.parent совпадает с exact …123 parent из ordered issue manifest/НАБОРЫ_123.
У каждого такого child matching history содержит docId `67225d44-c910-41ba-8599-e5011de42b91`
с тем же parent. Это live подтверждение старого использования, не только запись CSV.
Пять оставшихся stale draft Cherry pages656–660 свободны по CURRENT membership, но не имеют
целевой выдачи; свободная очередь не доказывает наклейку на нужную физическую единицу.
Сохранённые local set documents других batches не содержат повторов этих 34 draft children;
issue …123 и movement history выявляют конфликт, который такая local-document проверка пропускает.
У hands17 и free Cherry5 история содержит прежние business movements; никаких terminal
formation …123 для них не обнаружено. Full related document IDs сохранены приватно.

### Masked matrix всех 50

Каждая строка: expected child count=2, CURRENT parent APPLIED/LOCAL/SET/empty children.
H=руки …260; C=Вишня …079; A=Амбра …062. Приведены draft aliases, не executable allocation.
R1=current reuse …123; R2=unissued stale Cherry; R3=Ozon147 missing exact pair;
R4=WB hands pool unassigned + missing cream issue. Child status для найденных пар INTRODUCED/owner match.
Private classification.json содержит exact source lines, queue pages, current parent/history refs по каждому child.

| Parent alias | GTIN | Supply/city/ordinal | Draft child aliases | Membership / provenance | Classification / reason |
|---|---|---|---|---|---|
| SET-010d0db8b637 | 04619689656130 | Ozon / Невиномысск / №1 | C: CHILD-a112e87db3b7; H: CHILD-9f34e990d678 | C → SET-a638e3a4697a (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-6ecb9c2a2a2e | 04619689656130 | Ozon / Невиномысск / №2 | C: CHILD-e2fbec3985c7; H: CHILD-d78967c8c5c5 | C → SET-a0eda4540cd9 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-468ab517b70d | 04619689656130 | Ozon / Невиномысск / №3 | C: CHILD-ca14485973d9; H: CHILD-35b8c9c5f090 | C page658 unissued / no parent; H no parent / owner draft | BLOCKED_CHILD_EVIDENCE / R2 |
| SET-926b33f57f40 | 04619689656130 | Ozon / Пермь / №1 | C: CHILD-0234ce264753; H: CHILD-4b193dc7f078 | C → SET-5e86d2df2aec (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-e71e4837130f | 04619689656130 | Ozon / Пермь / №2 | C: CHILD-008863b25bb3; H: CHILD-7e25f1ff2455 | C → SET-c258a1296c59 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-95ad57cd0d09 | 04619689656130 | Ozon / Пермь / №3 | C: CHILD-140acb843e23; H: CHILD-622292665f34 | C → SET-eeedfb3920b5 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-8dce40689267 | 04619689656130 | Ozon / Тюмень / №1 | C: CHILD-2e09493fd859; H: CHILD-3a7f6b724266 | C → SET-ce8b553bb112 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-c8d6fa64c985 | 04619689656130 | Ozon / Тюмень / №2 | C: CHILD-5b17f8e05e1b; H: CHILD-3daf855f3932 | C → SET-8d46510ada01 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-92b8a3e29818 | 04619689656130 | Ozon / Тюмень / №3 | C: CHILD-8a998e5e2552; H: CHILD-c20ad93a0603 | C page659 unissued / no parent; H no parent / owner draft | BLOCKED_CHILD_EVIDENCE / R2 |
| SET-3f54194c1a40 | 04619689656130 | Ozon / Тюмень / №4 | C: CHILD-d7f55b549b08; H: CHILD-efbebdbafe8f | C page660 unissued / no parent; H no parent / owner draft | BLOCKED_CHILD_EVIDENCE / R2 |
| SET-2987e4084780 | 04619689656130 | Ozon / Тюмень / №5 | C: CHILD-325e6deb43dc; H: CHILD-53146d27cde4 | C → SET-4497fc8cba84 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-283323443599 | 04619689656130 | Ozon / Уфа / №1 | C: CHILD-afee95862e02; H: CHILD-85cb262b0a59 | C → SET-40c73f1683a2 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-802d00ccbb57 | 04619689656130 | Ozon / Уфа / №2 | C: CHILD-b0aa391f1fbd; H: CHILD-624416d0347a | C → SET-690d40750c09 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-bc7091ec7965 | 04619689656130 | Ozon / Уфа / №3 | C: CHILD-45ef9ac8b184; H: CHILD-7ae221cc74ec | C → SET-68d211e56692 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-b2414c6655c2 | 04619689656130 | Ozon / Уфа / №4 | C: CHILD-b709798d195e; H: CHILD-1ac3ebb5db2d | C page656 unissued / no parent; H no parent / owner draft | BLOCKED_CHILD_EVIDENCE / R2 |
| SET-104858fd67ea | 04619689656130 | Ozon / Уфа / №5 | C: CHILD-f033814cf69f; H: CHILD-47468e6d7c7d | C → SET-3702008bc330 (…123); H no parent / owner draft | CONFLICT / R1 |
| SET-17e27f7c0cac | 04619689656130 | Ozon / Уфа / №6 | C: CHILD-27413bc19798; H: CHILD-762784675b87 | C page657 unissued / no parent; H no parent / owner draft | BLOCKED_CHILD_EVIDENCE / R2 |
| SET-361b9728f552 | 04619689656130 | WB / Щеглово / №1 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-82c2535c50c2 | 04619689656130 | WB / Щеглово / №2 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-d39f1a962d27 | 04619689656130 | WB / Щеглово / №3 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-31ae89c3355d | 04619689656130 | WB / Щеглово / №4 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-bd43d17064ef | 04619689656130 | WB / Щеглово / №5 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-ce378cfcc3c9 | 04619689656130 | WB / Щеглово / №6 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-962a31627995 | 04619689656130 | WB / Щеглово / №7 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-1dfdfb3a2850 | 04619689656130 | WB / Щеглово / №8 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-8686e50deba7 | 04619689656130 | WB / Щеглово / №9 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-0faa757f1e3d | 04619689656130 | WB / Щеглово / №10 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-20a11be565bc | 04619689656147 | Ozon / Невиномысск / №1 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-ea57c8c9eb68 | 04619689656147 | Ozon / Невиномысск / №2 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-2b54c28efb03 | 04619689656147 | Ozon / Невиномысск / №3 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-07ad1f300296 | 04619689656147 | Ozon / Невиномысск / №4 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-44507317b6af | 04619689656147 | Ozon / Невиномысск / №5 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-a26f6ba41b2d | 04619689656147 | Ozon / Пермь / №1 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-954ffed09d56 | 04619689656147 | Ozon / Пермь / №2 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-7999b757c69f | 04619689656147 | Ozon / Пермь / №3 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-9178bcb72599 | 04619689656147 | Ozon / Тюмень / №1 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-043bcc978f09 | 04619689656147 | Ozon / Тюмень / №2 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-c0962493ea94 | 04619689656147 | Ozon / Тюмень / №3 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-aeed28724ab7 | 04619689656147 | Ozon / Тюмень / №4 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-8b1f9168ba2e | 04619689656147 | Ozon / Тюмень / №5 | Exact pair не установлен | Hands identities/queue pairing UNPROVEN | BLOCKED_CHILD_EVIDENCE / R3 |
| SET-d33b05de6030 | 04619689656147 | WB / Щеглово / №1 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-c36e20836bda | 04619689656147 | WB / Щеглово / №2 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-288c85f85963 | 04619689656147 | WB / Щеглово / №3 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-480b79b298fb | 04619689656147 | WB / Щеглово / №4 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-a61ab7db7ada | 04619689656147 | WB / Щеглово / №5 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-b2445f81b5ef | 04619689656147 | WB / Щеглово / №6 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-9c00ed85dd98 | 04619689656147 | WB / Щеглово / №7 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-06aa9d7d4b9c | 04619689656147 | WB / Щеглово / №8 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-7074835c72f8 | 04619689656147 | WB / Щеглово / №9 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |
| SET-e67c8e9f3ede | 04619689656147 | WB / Щеглово / №10 | Exact pair не установлен | Hands pool20, assignment UNPROVEN | BLOCKED_CHILD_EVIDENCE / R4 |

### Исторические 6 — отдельная unresolved group

Новые queries не включали шесть excluded parents или их children; причины не исправлялись.
Их parent snapshot APPLIED от 01.10.2026 не выдаётся за новый readback 02.10.
SET-155986c1bdaa …093 Невинномысск №4 — УВЛ вместо АКНЕ, physical contents/re-scan;
SET-bb375fe14554 …222 Пермь №5 — invalid serum scan, re-scan/current child check;
SET-786dd317f72a, SET-0f10b04e2b98, SET-9efb78a888a7, SET-ed67d6ab99cc …185 Тюмень №1–4
— missing УВЛ tonic scans / exact pairing. Все требуют отдельного owner decision.
Published NK cards не снимают эти blockers. Exact private identities остаются в evidence раздела 9.

### Next business operation / preview / STOP

Целевые parents не сформированы. Нужный дальнейший этап — **set formation**,
`AGGREGATION_DOCUMENT`, `aggregationType=SET`, `unitSerialNumberType=PRODUCT_SET`,
после доказанного exact composition/issue/physical mapping и нового explicit approval.
Для проверенных INTRODUCED children и LOCAL parent ожидается ввод parent в оборот как
результат formation; это требует terminal document evidence и current per-parent readback после
разрешённого execution. Отдельный LP_INTRODUCE_GOODS не подготовлен и не подменяет formation.
Для 33 с missing pairing тип эмиссии/статус необходимых children пока не доказан.

**Immutable business preview не создан:** READY=0; canonical executable payload,
operation_id и payload_sha256 отсутствуют. Scope hash не является payload hash.
Business approval button/request неприменим без доказанного exact operation.
Новый OWNER ACK не используется как execution approval. Business submissions=0; NOT_EXECUTED.
Не использовать старый 74383dc0 draft; не заменять конфликтующие children свободными страницами молча.
Нужно owner решение о bounded новой выдаче/переклейке Вишни/Амбры по очереди и доказанный
pairing рук для Ozon147/WB. Это новый business/local-runtime scope, текущим ACK не разрешён.
Сначала reviewable plan с exact inputs и физическим процессом, затем отдельный ACK для выдачи/переклейки;
formation потребует отдельного immutable preview и approval одной attempt.

### Evidence / integrity / safety finding

Private evidence: `/Users/evgenelagin/.config/evetis-chz/child-check-20261002T072104-958af7ce`.
Directory0700 / JSON0600; 94 files: before/recovery/auth metadata, three API snapshots,
84 histories, access-trace, classification, scope-integrity и evidence-manifest.
classification.json SHA256: `f624f7fec80e06ede46b0d2aa17c667c9e1c5506495af48875d80b924ae0ca9d`.
До обновления этого report все baseline source hashes совпали, включая обе актуальные и
датированные CSV, pointer, issue manifests, private journal, supply sources, session и чужие четыре
ingestion changes. Остальные baseline files сохраняются после update; меняется только этот report.
Legacy journal содержит377 events до/после, SHA256 не изменён. Auth использовал существующий
valid token: 0 challenge signatures, 0 refresh; wrapper отключал legacy auth journal write.
No business endpoints, no SUZ/NK mutation/signing, no queue allocation/issue/reservation, no V2 use.
Git branch/HEAD сохраняются, index не менялся, commit/push/PR/deploy отсутствуют.
Initial offline recovery остановился на нерелевантных synthetic/manual code formats и затем
на explicit space-separated WB scan boundaries; исправления затронули только temporary helper.
До network launch frozen recovery прошёл format/uniqueness/provenance checks.
Safety finding из раздела9 (12 full legacy КИ в прошлом tool output) остаётся зафиксированным;
не объявлен закрытым и не повторён. Новый output — whitelist metadata/aliases, exception text
и identifier-key dumps исключены; полные identifiers не переносятся в Git/context.
**STOP BEFORE BUSINESS WRITE.**

## 11. OWNER BUSINESS LOGIC — KIZ POOL ALLOCATION / SET FORMATION

Authority: explicit OWNER clarification `b2e4543e-4bde-4e3c-91b9-89feb2439702`,
принято 02.10.2026. Это действующая allocation model EVETIS для данной задачи.
Разделы9–10 остаются историческими audit snapshots, а не требованиями к новому allocation.
Ни этот текст, ни исторические ACK не разрешают отправить business document.

### Принятая владельцем allocation model

Вишня GTIN …079 и Amber Vanilla GTIN …062 имеют последовательные master pools,
организованные по видимым страницам PDF. Количество исторически выпущенных КИ больше
физического остатка товара; наличие страницы не доказывает пригодность КИ.
Источник назначения: **master pool + ordered cursor + CURRENT CHZ validation + local usage history**.
Для каждого пула существует один общий cursor для единичек, child наборов, WB и Ozon.
Отдельные очереди по purpose/marketplace создавать нельзя.

Следующий кандидат проверяется по CURRENT status, ownership, GTIN, membership, eligibility
и local issued/reserved/used/UNKNOWN history. При непригодности фиксируются page, alias,
reason и evidence; поиск продолжается по возрастанию страниц. Неоднозначный кандидат
quarantine/UNKNOWN и не используется. Historical pointer — starting evidence.
КИ, legitimate использованный для любой purpose, повторно свободным не становится.

Единичная выдача крема требует выбрать и зарезервировать пригодные КИ, затем передать
PDF/этикетки ФФ для физического нанесения. Назначение крема как child набора использует
тот же общий пул, но в принятом owner процессе не требует физической переклейки child
или восстановления old Chinese scan → новая этикетка → parent. Child назначается при
подготовке новой aggregation matrix. Это различие не отменяет проверок CURRENT/local use.

Для рук GTIN …260 существуют отдельные КИ и lifecycle emission → требуемый introduction
→ подтверждённый пригодный child. Руки как единичка передаются ФФ для нанесения;
руки как child уже собранного набора могут deterministic назначаться из предназначенного
для задачи пригодного пула без старого physical hand-to-parent pairing.
При дефиците 50 пригодных КИ рук нельзя молча выпускать дополнительные.

Parent SET имеет собственный КИ, относящийся к физическому собранному набору и нанесённый
ФФ. Новый allocation назначает required children существующим exact parents по BOM:
…130 = руки …260 + Вишня …079; …147 = руки …260 + Amber …062.
Отсутствие исторической final matrix само по себе не означает ошибку комплектации.
Вывод 30.09 «50 из 56 готовы» означал наличие материалов/pools для последующего формирования.

Документ `67225d44-c910-41ba-8599-e5011de42b91` — legitimate consumption других
12 parent SET …123: Вишня644–655, Amber401–412. Эти children исключены из нового allocation.
Старые …130 drafts с ними obsolete; они не превращают новые …130 parents в conflict.
Расформирование …123, возврат consumed codes или автоматическое повторное использование запрещены.

### Deterministic matrix и отдельное execution approval

Scope фиксирован: 17 Ozon …130, 13 Ozon …147, 10 WB …130, 10 WB …147, всего50;
шесть исторических …093/…222/четыре …185 остаются unresolved/quarantine и не включаются.
Полный scope50 нельзя уменьшать при blocker или отправлять частично.
Каждый parent и каждый child используется ровно один раз; GTIN/BOM проверяется CURRENT.
Obsolete draft mappings не определяют новую matrix.

Предлагаемый порядок: parents Ozon130 → Ozon147 → WB130 → WB147;
внутри Ozon Невиномысск → Пермь → Тюмень → Уфа, затем исходный PDF ordinal;
hands исходный Ozon PDF1–30 → WB saved grid в числовом порядке cells1–20;
creams возрастающие CURRENT пригодные страницы одного общего пула, sntins [hands, cream].
Порядок показывается в preview и замораживается с exact operation identity/matrix.

Только после CURRENT validation всех 50parents +50hands +27Cherry +23Amber создаётся
immutable AGGREGATION_DOCUMENT preview: PROD, exact type, scope/groups, aliases/matrix,
selected/skipped pages с reasons, hands source, before/after frontier, timestamps,
BOM/duplicate/membership checks, operation ID, canonical payload SHA-256 и expected effect.
Business identity, canonical payload, transport serialization и actual signed/submitted bytes
не смешиваются; отдельные hashes применимы только к действительно созданным representations.
Content fingerprint не заменяет operation identity. Full КИ и business payload остаются private.

Текущий ACK разрешает reconstruction/readback/preview, но не execution.
После полного preview — STOP и отдельный exact approval «Сформировать 50 наборов EVETIS в PROD»,
связанный с operation ID, payload hash, environment и exact matrix.
Business operation, owner approval и execution attempt — отдельные сущности.
Будущее approval допускает одну mutating attempt; перед ней повторно проверяются immutable
binding, selected codes и отсутствие нового local conflict. При timeout/reset/crash/ambiguous
response/нет terminal evidence — UNKNOWN, без повторной отправки; только reconciliation.
После отправки нужны terminal document result и exact CURRENT membership всех parents/children;
HTTP acceptance недостаточно. Aggregation и introduction имеют отдельные owner approvals.

Lifecycle examined/skipped/proposed/reserved/submitting/unknown/consumed разделяется.
Просмотр/проверка/proposal не двигают pointer и не резервируют КИ. Consumed фиксируется после
доказанного terminal business result; UNKNOWN остаётся заблокированным. Будущий SAFE V2 pointer
является projection ledger. До cutover legacy queue/pointers не меняются без отдельного ACK.

Regulatory parameters происхождения, выпуска, РД, production/expiry dates и introduction
не выводятся из этой owner clarification. Используются подтверждённое EVETIS evidence,
применимая official documentation или отдельный owner input; при расхождении STOP.
Никаких новых regulatory значений в этом этапе не назначено.

### Результат разрешённого reconstruction 02.10.2026

Первый auth attempt до подтверждения владельцем подключения флешки. Этот failure snapshot
сохранён; актуальный результат возобновлённой проверки и preview находится в разделе12.

**BLOCKED_AUTH_CMS_NO_CURRENT_CANDIDATE_READBACK. Business result NOT_EXECUTED.**

| Проверка | Доказано локально | CURRENT в этом запуске |
|---|---|---|
| Вишня | CSV/manifests: последняя выдача/использование655; starting candidate656; 100кандидатов656–755 без local holds | Safe frontier / eligibility UNPROVEN |
| Amber | CSV/manifests: последняя выдача/использование412; starting candidate413; 100кандидатов413–512 без local holds | Safe frontier / eligibility UNPROVEN |
| Руки | 30 exact unique из embedded исходного Ozon PDF +20 из WB saved grid; суммарно50 без пересечения и local incompatible document refs | Пригодность50 UNPROVEN |
| История submission | 8journal create POST/201 соответствуют8set events; все8имеют исторический tool-result CHECKED_OK | CURRENT doc status не запрашивался |
| UNKNOWN/reservations | В исследованных journal/session нет known unmatched submission; candidate windows не имеют local issued/used/doc refs | Не заявляется глобальное отсутствие неизвестных pending requests |
| Матрица / batch | Scope50 и canonical/source order подготовлены; шесть excluded отсутствуют | Validated matrix0/50; batch не сокращён |
| Пропуски | Historical APPLIED Вишня662 и Amber415 требуют fresh validation | CURRENT skips не определены |
| После consumption | Pointer не менялся, reservation не выполнялась | Proposed after frontier не определён до CURRENT selection |
| Preview / approval | Executable payload не сформирован; canonical payload hash отсутствует | READY preview и actionable approval не созданы |

Все200 candidate rows exact совпадают с единственной видимой КИ на cropped PDF page.
Нельзя использовать uncropped text extraction: hidden neighboring label text может дать неверный КИ.
Проверена уникальность обоих master indices по5036КИ и hand pool50.
Поиск starting candidates656/413 не означает, что эти страницы уже AVAILABLE.

Auth boundary: GET True API auth/key вернул HTTP200. Затем legacy `sign(auth challenge)`
завершился SystemExit из проверки nonzero csptest return code либо missing signature output.
Один запуск подписи, ноль завершённых signatures по helper contract, ноль simpleSignIn,
token refresh, CURRENT candidate reads и business submissions. Повторная подпись не выполнялась.
Exact CSP error code не сохранён: чувствительные stdout/stderr/exception text не выводились.
Причиной нельзя без evidence объявлять PIN, сертификат или отключённый токен.
Необходима работоспособность существующего auth-only signer перед продолжением readback.

Private reconstruction/evidence: `~/.config/evetis-chz/allocation-preview-20261002T103156-7daa3f04`.
Owner-facing masked report: `CHZ-POOL-20261002T103156-7daa3f04/Allocation_reconstruction.md`
в task output directory. Это reconstruction report, не executable business preview.
Проверены154source fingerprints; legacy queue/index/pointer/manifests/journal/session,
старые private evidence и чужие ingestion changes неизменны. Git HEAD/branch/index/worktree
inventory не менялись, staged diff пуст. V2 не использован, SUZ/NK mutations отсутствуют.
Единственный repository write этого этапа — authorised дополнение данного existing report.
Никаких commit/push/PR/deploy, issue/reservation/consumption или business writes.

**STOP BEFORE BUSINESS WRITE.**

## 12. CURRENT pool allocation / immutable preview 50 — 02.10.2026

Это preview snapshot до execution approval. Последующий trusted approval и подтверждённый
business result записаны в разделе13; original private preview остаётся неизменным.

После trusted owner сообщения «флешка с подписью CryptoPro установлена» продолжена
уже разрешённая auth-only/read-only проверка. Создан отдельный новый private evidence directory;
failure evidence раздела11 и все прежние evidence сохранены без изменения contents/permissions.
Действующая owner allocation model — раздел11; её нормы не менялись.

**PREVIEW_READY_AWAITING_OWNER_APPROVAL. Matrix50/50. Business result NOT_EXECUTED.**

CURRENT PROD observation: **02.10.2026 10:53:16–10:54:04 Europe/Moscow**
(07:53:16–07:54:04 UTC). Auth challenge подписан успешно; True API token обновлён один раз
через существующий mechanism. Business signing, NK signing/mutations и SUZ access отсутствуют.
Все112allowlisted requests HTTP200: auth2, cises/info1, product/info1,
known document info8 и selected child histories100.

### Frontier / pools / skips / exact scope

| Пул | CURRENT начало пригодного поиска | Selected | Skip | Proposed frontier после terminal consumption |
|---|---:|---|---|---:|
| Вишня …079 | 656 | 656–661, 663–683; 27КИ | 662, CHILD-9e7994b4f91c, CURRENT APPLIED | 684 |
| Amber …062 | 413 | 413–414, 416–436; 23КИ | 415, CHILD-ec893ce497a1, CURRENT APPLIED | 437 |
| Руки …260 | original Ozon PDF30 → WB grid20 | все50unique КИ пригодны CURRENT | 0 | Отдельный master cursor кремов не создаётся |

Ровно два CURRENT skips: данный состав использует INTRODUCED children; APPLIED662/415
не подходят вместе с введёнными руками и не включены. Это skipped, не consumed codes.
В bounded info query проверены160КИ:50parents+50hands+32Cherry+28Amber.
До набора нужного количества последовательный поиск examined28Cherry/24Amber;
четыре дополнительных CURRENT проверенных кандидата каждого пула остались examined,
не proposed/reserved/consumed. Caps: максимум100кандидатов/пул; дальнейшие окна не понадобились.
Legacy pointers по-прежнему656/413; значения684/437 не записывались.
Consumed children документа …123 отсутствуют; obsolete старые …130 drafts не используются.

Полный business scope: **17Ozon…130 +13Ozon…147 +10WB…130 +10WB…147 =50**.
50existing exact parent SET, 50hands, 27Cherry, 23Amber; children100unique.
Шесть historical exceptions остаются excluded/unresolved и отсутствуют в payload.

### CURRENT eligibility / BOM / prior-use evidence

Все50parents: APPLIED/EMPTY/LOCAL/SET, expected GTIN, owner matches, child=[], parent отсутствует.
Все100children: INTRODUCED/EMPTY/UNIT, expected GTIN, owner matches,
parent отсутствует, child=[]; руки LOCAL, кремы FOREIGN. CURRENT markWithdraw=false
и expirationDate в будущем для всех150selected codes; productGroup=chemistry.
CURRENT cards…130/…147 опубликованы, подписаны, mark/turn flags=true, exact BOM2
руки260+Вишня079 / руки260+Amber062. Никакие regulatory values не изменялись/не назначались.

Дубликаты, current membership conflicts и local incompatible usage refs —0.
Все100selected child histories прочитаны HTTP200, code joins/coverage exact;
предыдущие parent/child relations отсутствуют. Все8known legacy business documents
теперь имеют CURRENT CHECKED_OK. В audited journal/session нет known incomplete submissions;
это не доказательство глобального отсутствия неизвестных pending requests вне сохранённой истории.
Перед будущей отправкой обязательно повторить CURRENT/local checks: этот snapshot имеет дату.

### Immutable payload / identity / reviewable approval

Порядок из раздела11 применён без изменения: четыре группы parents, city/PDF ordinal;
Ozon source hands PDF1–30 затем WB source grid1–20; creams по возрастающим пригодным
страницам общего пула; children array [hands, cream]. Это новая deterministic matrix,
а не утверждение historical physical pairing. Сформирован один preview document на все50.

- Environment: **PROD**; type **AGGREGATION_DOCUMENT**, aggregationType=SET,
  unitSerialNumberType=PRODUCT_SET.
- Operation ID: `chz-set-formation-50-6fb86f35cbc84cd08eadce941d6a2189`.
- Canonical payload SHA-256: `f7926944a0bc26e6f7b3e4b23d100f7e97352434c44c9fa8ae35afbb63eb959b`.
- Exact matrix SHA-256: `005d6e344e70a15542c9047a139f8a1f2cc18e62f5a5e27908dc37a150c7dc84`.
- Business identity и proposed transport payload имеют отдельные hashes в private approval binding.
  Actual signed/submitted bytes отсутствуют, их hashes null; execution attempt и approval отсутствуют.

Expected effect будущей approved operation: зарегистрировать exact100children в exact50PRODUCT_SET
parents; затем доказать terminal processing и exact CURRENT memberships всех50 без partial result.
Возможный системный переход parent статуса при formation устанавливается readback;
отдельный introduction document/approval не входит в данную операцию.
Pending owner approval относится только к exact operation/hash/matrix/PROD и одной mutating attempt.
Pointer/legacy ledger write не разрешается автоматически aggregation approval.

Private exact evidence/payload:
`~/.config/evetis-chz/allocation-preview-20261002T105258-33af49f6`.
Files `aggregation-payload.canonical.json`, `aggregation-payload.transport.json`, `approval-binding.json`
созданы O_EXCL; exact codes не помещаются в Git/report/chat.
Owner-facing masked matrix50 и проверка:
`CHZ-PREVIEW-20261002T105258-33af49f6/Immutable_preview.md`, `Preview_verification.json`
в task outputs. Independent offline validation заново построила expected ordered payload
из source plan и CURRENT eligibility, сверила150selected codes, uniqueness, groups, skips,
исключения, histories, expiry/withdrawal и все distinct representation hashes: **PASS**.

### Integrity / STOP

154source fingerprints включают current/datedindices, queue/pointer/manifests, supply/session,
legacy journal, old evidence и чужие ingestion changes.153legacy/source files остаются неизменными;
из repository разрешённо меняется только этот existing report. Git branch/HEAD/index/worktree
inventory сохраняются, staged diff пуст; V2 не использован.
Business submissions/signatures0; legacy queue/pointer/journal/reservation writes0;
commit/push/PR/merge/deploy отсутствуют. Только auth token cache refresh разрешён текущим ACK.

Auto approval review отклонила массовую установку прав0400 для предыдущего нового evidence,
сочтя её изменением existing evidence. Действие не выполнено; продолжен безопасный вариант
без chmod и изменения existing contents/permissions. Private directories0700/files0600;
O_EXCL и hash manifests фиксируют reviewable contents. OS read-only immutability не заявляется.

**STOP BEFORE BUSINESS WRITE. Exact owner execution approval ещё не получен.**

## 13. Exact approved execution 50 — CURRENT SUCCEEDED 02.10.2026

Trusted OWNER reply на отдельный bound approval:
«Сформировать 50 наборов EVETIS в PROD — одна attempt».
Operation `chz-set-formation-50-6fb86f35cbc84cd08eadce941d6a2189`, canonical payload
`f7926944a0bc26e6f7b3e4b23d100f7e97352434c44c9fa8ae35afbb63eb959b`, exact matrix
`005d6e344e70a15542c9047a139f8a1f2cc18e62f5a5e27908dc37a150c7dc84`.
Approval, business operation и execution attempt записаны отдельными private records.

Перед signing/submission повторно проверены exact hashes, CURRENT150selected КИ,
cards/BOM2, histories100 и local usage/UNKNOWN state: PASS. После signing ещё раз
проверены CURRENT150КИ и local sources. Approval-consumed marker и submission intent
O_EXCL/fsync сохранены до mutating request. Выполнены **одна detached business signature
и один POST AGGREGATION_DOCUMENT**; никаких automatic retries.

Submission observation **02.10.2026 11:26:05 MSK**, HTTP201.
Фактический response был plain UTF-8 UUID и не разобрался как JSON; operation первоначально
fail-closed UNKNOWN. Только readback/reconciliation, без повторного POST, восстановили doc ID:
`5a3974c8-89db-4500-941f-63f7315569e5`. SHA256 UTF-8 UUID в точности совпал с фактическим
HTTP201 response SHA256 `c13bac88910c450a2a0414777c2110708e829ff1443cf9a626c3d54b780dd651`.
Это связывает найденный документ именно с approved attempt, а не с предполагаемой другой операцией.

CURRENT document readback **11:30:09 MSK**: type AGGREGATION_DOCUMENT,
status **CHECKED_OK**, errors/docErrors отсутствуют. CHZ document date
`2026-10-02T08:26:10.112Z`. CURRENT cises/info и histories100:

- Все50parents INTRODUCED/EMPTY/LOCAL/SET, owner/GTIN совпадают.
- У каждого parent ровно два exact approved children; все50relations совпадают с matrix.
- Все100children INTRODUCED/EMPTY/UNIT; каждый child принадлежит именно approved parent.
- Histories всех100children содержат этот doc ID и правильный parent.
- Partial result/duplicates/membership mismatches0; excluded six не затронуты.

**Business operation SUCCEEDED.** Private consumption commit создан только после
terminal document evidence и доказанного exact membership. Independent offline verifier
повторно сверил approved matrix, decoded signed business payload, CMS hash, actual wire hash,
actual response identity hash и все membership/history joins: **PASS**.
Signed input SHA256=`51d99e9cd3ca947ed6a384b886961e4edafb7ffc88600c37b9c578aa04b86e67`;
actual submitted envelope SHA256=`f175c9eb70e0d5979369bed08b19ad6b0158ff40c58eec6edf7b0d79bd2a3c90`.
Business/canonical/transport/signature/wire representations не смешаны.

Groups неизменны:17Ozon…130,13Ozon…147,10WB…130,10WB…147.
Подтверждённо consumed этой operation:50hands, Вишня656–661/663–683, Amber413–414/416–436.
Страницы662/415 не использованы. Effective next search после confirmed consumption:
**Вишня684 / Amber437**. Historical legacy pointers остаются656/413 и теперь не отражают
новый consumption. Следующая reconstruction обязана учитывать CURRENT memberships и этот
private business-commit; это не разрешение автоматически менять legacy pointers/выдавать codes.

Отдельный introduction document не создавался/не подписывался/не отправлялся:
INTRODUCED50 наблюдается как результат approved formation. Utilisation/emission/NK operations,
legacy queue/index/pointer/journal writes и V2 access отсутствуют.153legacy source files и
approved private preview неизменны; branch/HEAD/index/worktree inventory сохранены, staged diff пуст.
Commit/push/PR/merge/deployment отсутствуют. Consumed approval не разрешает новую mutating attempt.

Exact private execution evidence:
`~/.config/evetis-chz/execution-chz-set-formation-50-6fb86f35cbc84cd08eadce941d6a2189`.
New owner-facing masked result/matrix50:
`CHZ-RESULT-6fb86f35-20261002/Execution_result.md` в task outputs.
Full codes, signature/wire bytes, headers и credentials не помещены в Git/public result.
Private commit является evidence business consumption, не V2 migration/production queue rewrite.

**STOP. Approved operation завершена; дополнительные business operations не разрешены.**
