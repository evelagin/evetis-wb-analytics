# CHANGELOG

## 1.13.2 — R2.4A.2: разнообразие ответов на оценку без текста (2026-10-09)

Аудит 19 живых черновиков на оценку без слов (09.10): тексты формально уникальны, но 18 из 19 начинались
с «<Имя>, благодарим за высокую оценку <товар> EVETIS», около 89 % заканчивались «Нам приятно, что Вы
выбрали именно его/её». Причины: предписывающая инструкция промпта, покрытие аспекта требовало
одновременно «спасибо» + «оценк» + «приятн/рад» + название товара, и единственный детерминированный запасной текст.

- **Покрытие `rating_only_thanks`**: благодарность или радость + оценка (`оценк|звезд|пятёрк`) + одно тёплое
  слово (`приятн|рад|выбор|желаем|EVETIS`). Название товара больше не обязательно. Голое «Спасибо за
  высокую оценку!» по-прежнему помечается как слабое.
- **Шесть запасных структур** без утверждений о товаре; выбор — `sha256(communication_id)`, без случайности:
  повтор того же отзыва даёт тот же текст. Все проходят v31 и live-политику для проверенных товаров;
  непроверенный товар, как и раньше, уходит на ручную проверку.
- **Промпт 3.1E**: инструкция переписана (1–3 предложения, товар по желанию, без механического «благодарим
  за высокую оценку» и «выбрали именно его», без аромата/результата/повторной покупки), в запрос
  добавлен стабильный `rating_only_style_hint` (6 вариантов начала и порядка), только для оценки без текста.
- Синтетика 24 отзыва: 22 уникальных текста, 6 структур, крупнейшая — 25 %. Низкая оценка без слов,
  отзывы с текстом и публикация не менялись. Записей в WB нет.

## 1.13.1 — P0: оценка без текста попадает в модерацию только после прямого чтения WB (2026-10-09)

Инцидент 09.10: после включения R2.4A.1 (окно 168 ч) в Telegram по 5 в час приходили карточки исторических
оценок без текста (03–08.10). Расследование только чтением: 18 карточек = 18 разных отзывов (дублей нет);
у 13 ожидающих нет ответа продавца ни в одной выдаче WB — лента отвеченных, прямой GET, архив отзывов,
публичная карточка (WB сразу архивирует оценки без текста, поэтому в кабинете они выглядят обработанными);
на 5 ответил оператор, WB принял запись и обратное чтение совпало. Догон завершился в 17:00Z.

- Элемент ленты отвеченных — только кандидат. Перед claim: локальная проверка (отзыв уже есть → пропуск
  без чтения WB) и **прямое чтение отзыва по id** (тот же GET, что у проверенного публикатора): карточка
  создаётся, только если это тот же отзыв, он по-прежнему без текста и **без текста ответа продавца**.
  Ответ есть → не берём; ошибка, чужой id или нет поля ответа → не берём (повтор в следующем прогоне).
- Счётчики `rating_only`: `known`, `answered_on_wb`, `not_rating_only`, `read_errors`.

## 1.13.0 — R2.4A.1: полный приём отзывов WB, закрытие устаревших карточек, /status (2026-10-09)

Живой аудит 48 ч (09.10): расписание и доставка исправны, но (1) WB помечает оценку без текста
`isAnswered=true` без ответа продавца, и такие отзывы не попадают в EVETIS: в исходном окне аудита их было 5
(ранее ошибочно указано 6 — шестой был обычным неотвеченным отзывом с текстом, обработанным штатно); к
финальной живой проверке в скользящем окне 48 ч их стало 6 (появилась новая оценка); за 7 дней — 17, и все
17 — чистые оценки без текста, плюсов, минусов и тегов (оценки с текстом/тегами/плюсами в отвеченной ленте
без ответа не встречаются); (2) 18 карточек «ожидает» уже были отвечены в кабинете WB.

- **`WB_RATING_ONLY_INGEST_ENABLED`** (по умолчанию `false`): кроме ленты неотвеченных, прогон читает
  ограниченное окно отвеченной ленты (`WB_RATING_ONLY_LOOKBACK_HOURS`=48, `WB_RATING_ONLY_MAX_PER_POLL`=20) и
  берёт ТОЛЬКО оценки без текста, плюсов, минусов и тегов, у которых на WB нет текста ответа продавца
  (флаг `isAnswered` не используется как признак ответа). Дальше — обычный конвейер (claim по id отзыва →
  3.1E → проверка до карточки → Telegram → проверенный публикатор).
- **`WB_RECONCILE_EXTERNAL_ANSWERS_ENABLED`** (по умолчанию `false`): за прогон до `WB_RECONCILE_MAX_PER_POLL`=10
  карточек в статусах pending_approval / policy_blocked / policy_check_failed / publish_failed читаются на WB
  (только GET). Есть ответ продавца, отличный от нашего → `answered_externally`; совпадающий с нашим и
  публичный → `published` (как в проверенном публикаторе). Переход — только если статус и версия не
  менялись; карточка в Telegram закрывается без кнопок. Нет ответа (включая `isAnswered=true` без текста) или
  ошибка чтения — без изменений. Записей в WB нет.
- **`/status`** для всех авторизованных модераторов: последний опрос, отзывы и вопросы (получено, новые
  карточки, ошибки), последняя карточка, следующий опрос, ожидающие решения, ревизия; предупреждения при
  ошибках или давнем опросе. Данные — одна запись `wb_poll_health/current`, обновляемая в конце каждого
  прогона (без чтения журналов). Ежечасных сообщений нет.

## 1.12.0 — R2.4A: доступ команды через Telegram и идентичность модератора (2026-10-09)

- **`TELEGRAM_DYNAMIC_ACCESS_ENABLED`** (по умолчанию `false`; без флага — поведение R2.3 и ни одного
  обращения к новым коллекциям). Один источник прав `app/services/team_access.py`: bootstrap из окружения
  (`TELEGRAM_ALLOWED_USER_IDS` ∪ `V31_OWNER_OVERRIDE_USER_IDS`, всегда OWNER, без Firestore, не отзываются)
  ∪ ACTIVE-участники Firestore настроенного чата модерации. Модерация (в т. ч. «Опубликовать как есть» и
  двухшаговое решение R2.3) — для OWNER и OPERATOR; управление командой — только OWNER.
- Команды `/apply`, `/me`, `/help`, `/team`, `/requests`; карточка запроса с «✅ Добавить оператора» /
  «❌ Отклонить»; отзыв с подтверждением. Переходы — одной транзакцией с записью аудита
  (`telegram_team_audit`), повтор нажатия не меняет состояние. Сбой Firestore — отказ для всех, кроме
  bootstrap. Работает в режиме приватности бота.
- **Идентичность модератора:** события (`telegram_user_id`, `payload_json.actor` с ролью, источником и
  действием) и `publish_trace.actor`; новые события `edit_started` и `override_requested`.
- Рунбук `TELEGRAM_TEAM_ACCESS.md`.

## 1.11.0 — R2.3: окончательное решение владельца на карточках R2.2 (2026-10-09)

v3.1E предлагает ответ, проверяет утверждения и предупреждает; содержательный BLOCK больше не окончательный
запрет для явно подтверждённого владельцем текста. Только карточки R2.2 (`operator_mode=v31_only`).

- **Включение независимо от V2/R2.1:** `V31_OWNER_OVERRIDE_ENABLED=true` **и** явный список
  `V31_OWNER_OVERRIDE_USER_IDS` (пусто = никто). `V31_ENFORCE_LIVE_PUBLICATION_POLICY` не нужен. Старый
  override для карточек R2 не изменён.
- **Проверка до карточки (preflight).** Перед отправкой карточки (и после правки, «Другой вариант»,
  альтернативы, перевода карточки) действующая политика проверяет активный ответ; результат хранится в
  `publication_preflight`: SHA-256 текста, generation, режим, версия политики, снапшот, вердикт, правила,
  фрагменты, отпечаток, RED и время. Находка видна на карточке ДО действия — без сюрприза после нажатия.
- **Карточка.** PASS: «✅ Опубликовать / ✏️ Изменить / 🔄 Другой вариант / ⏭ Пропустить» — одно нажатие.
  Обычная находка (BLOCK): «⚠️ Система не рекомендует публикацию без проверки: <понятное описание>» и
  «✅ Опубликовать как есть» (только владелец; видимое предупреждение — первое подтверждение, нажатие —
  окончательное), «✨ Безопасная альтернатива», ✏️, 🔄, ⏭. RED (серьёзная безопасность, закрытые значения
  и иные правила высокого риска — V-RESTRICTED, V-CONFLICT, V-SAFETY, V-SAFETY-ROUTE, V-MEDICAL — и
  недоступность политики): «⛔ Высокий риск…» и двухшаговое «⚠️ Решение владельца» (замечания + точный
  текст → подтверждение). Если политика изменилась после показа карточки, нажатие обновляет карточку.
- **«✨ Безопасная альтернатива»** — ответ 3.1E, проходящий текущую политику: сохранённый черновик 3.1E,
  если он отличается и проходит, иначе новый черновик 3.1E; без неё — ничего не меняется. Никогда не V2.
- **Привязка подтверждения:** Telegram user ID владельца, коммуникация, generation, SHA-256 точного текста,
  контекст обращения, срок действия, отпечаток политики (версия, снапшот, вердикт, нарушения). Изменение
  любого из них или смена правил между нажатиями — повторное подтверждение. Подтверждение потребляется
  атомарно вместе с арендой публикации.
- **Журнал:** в `publish_trace.policy` остаётся фактический `BLOCK`/`ERROR`, рядом `owner_override`
  (кто, когда, хэш, версия); `PASS` не подставляется.
- **Безопасность:** на серьёзном (и любом) случае безопасности решение владельца доступно только для
  текста, написанного оператором; машинный текст и V2 через этот путь не публикуются никогда.
- **Технические гарантии не меняются:** allowlist, лимит WB (длинный текст не предлагается), нет
  автопубликации, аренда, одна запись, двойное нажатие, stale-check, чтение до записи, обратное чтение.
- **Старые карточки:** `pipeline.migration_plan` / `migrate_to_v31_only` переводят ожидающую карточку
  R2/R2.1 в поток R2.2 под арендой перегенерации, новой карточкой со старой снятой; опубликованные,
  публикуемые, неопределённые, внешние и пропущенные записи исключены. Маршрут не подключён; план —
  `scripts/r23_pending_cards.py` (только чтение); перевод — только по ACK владельца.

## 1.10.0 — R2.2: только 3.1E в работе оператора; честный ответ о поступлении (2026-10-08)

Первая живая карточка R2.1 (вопрос c0506bca…, «когда появится крем 438775437?»): 3.1E отправил вопрос
человеку (`SERVICE_INSTRUCTION_NOT_VERIFIED`), ведущим стал V2, `LIVE_V2` его заблокировал (`V-CLAIM`),
оператор дважды перегенерировал V2.

- **`V31_ONLY_OPERATOR_ENABLED`** (по умолчанию `false`; включает и R2.1). Новые карточки
  (`operator_mode=v31_only`): нормальный ответ — только 3.1E или текст оператора. Генератор V2 и его
  промпт для новых коммуникаций **не вызываются** (сбой V2 не может остановить 3.1E), теневое сравнение V2
  и валидаторы V2 над ручным текстом на этих карточках отключены, `LIVE_V2` не вызывается. Код V2 не удалён:
  LEGACY_FALLBACK / ROLLBACK ONLY для отката на R2.1 и старых карточек; `sv2`/`u2` на карточках R2.2
  отклоняются, публикация без ответа 3.1E — `no_v31_answer`.
- **Без «лотереи V2».** Технический сбой 3.1E: «⚠️ Не удалось подготовить ответ 3.1E» и кнопки
  «🔄 Повторить» / «✏️ Написать вручную» / «⏭ Пропустить». Повтор перегенерирует только 3.1E; без ответа
  блокировка снимается, ничего не меняется. Проверка человеком (юридическое, подлинность, нераспознанный
  вопрос, возврат): «⛔ Нужна проверка человеком: <причина>», кнопки «✏️ Написать вручную» / «⏭».
- **Политика всегда v3.1E:** любой ответ на карточке R2.2 — `v31`; ответ оператора на серьёзный случай —
  `v31_human_safety`. `LIVE_V2` на карточках R2.2 не используется.
- **SAFE_INFORMATION_GAP** (`app/response_quality/safe_gap.py`): вопрос покупателя только о поступлении
  товара → «Здравствуйте! Точную дату следующего поступления <крема/набора/…> пока подтвердить не можем.
  Рекомендуем следить за наличием в карточке товара на Wildberries. Спасибо за интерес к EVETIS!» Без даты
  и обещаний. Только сущность «вопрос», только если в сообщении нет других просьб (возврат, состав,
  «подойдёт ли», цена, доставка и т. п.). Неизвестные количество/частота/стойкость уже отвечались
  существующими формулировками UNKNOWN_FACT.
- Пилот R1 при включённом флаге не запускается. Старые карточки не мигрируются. Автопубликации нет.

## 1.9.0 — R2.1: 3.1E — основной черновик оператора; умеренный дискомфорт от аромата (2026-10-08)

Первый живой кейс R2 (52d55f80…, «отдушка… тошнотворная, голова кружится»): 3.1E ушёл в проверку человеком
без черновика, черновик V2 заблокирован правилом `LIVE_V2` — оператору пришлось писать ответ вручную.

- **`V31_PRIMARY_OPERATOR_ENABLED`** (по умолчанию `false`; при `false` поведение R2 не меняется). Готовый
  (READY) ответ 3.1E становится активным ответом карточки («✨ Рекомендуемый ответ 3.1E»), кнопки обычные:
  «✅ Опубликовать», «✏️ Изменить», «🔄 Перегенерировать», «⏭ Пропустить». V2 — только вторичная кнопка
  «🗂 Старый вариант V2» (отдельное сообщение; «↩️ Использовать вариант V2» переключает ответ явно).
  Сбой 3.1E → карточка с резервным ответом V2 и пометкой причины. «Перегенерировать» обновляет 3.1E.
  Ответ в карточке не обрезается: ужимаются поля покупателя; без видимого ответа нет кнопки публикации.
- **Политика по происхождению ответа.** `_publish` выбирает правило по истории версий: последний машинный
  источник `v31e` → политика v3.1E, иначе `LIVE_V2`; ручная правка наследует источник (правка 3.1E остаётся
  на 3.1E). Явный выбор V2 (`v2_fallback`) → `LIVE_V2`. Режим пишется в `publish_trace.publication_mode`.
  Проверенный публикатор не менялся (проверка политики → одна запись → обратное чтение).
- **Безопасность: умеренный и серьёзный классы.** `app/v3/safety.moderate_discomfort`: тошнота,
  головокружение или головная боль, которые покупатель связывает с ароматом, без маркеров серьёзности
  (рвота, обморок, отёк, дыхание, глаза, проглатывание, ожог/пузыри, ребёнок, беременность, смешанные
  кожные события) → «⚠️ Требует внимания оператора» и осторожный детерминированный ответ
  (`app/response_quality/moderate.py`): плюс покупателя, признание дискомфорта, «отложить этот аромат, если
  повторится», благодарность; без врача, без «прекратите всё», без свойств товара. Политика v3.1E не
  блокирует такой ответ по `V-SAFETY-ROUTE`; остальные правила действуют. Серьёзный класс не ослаблен:
  ответа нет, «✅ Опубликовать» и «🔄» нет, публикация машинного текста отклоняется на сервере
  (`human_review_required`), вариант V2 принять нельзя; оператор пишет свой ответ. Умеренный класс
  включается только вместе с флагом.
- **Ответ оператора на серьёзный случай** публикуется в контексте `v31_human_safety`
  (`validate_human_safety_publication`, gate `V31_HUMAN_SAFETY`): политика v3.1E, в которой проверка
  человеком снимает только маршрутный запрет `V-SAFETY-ROUTE`. Неподтверждённые факты и свойства,
  ограниченные значения, конфликты, сервисные предпосылки, опасные указания, длина и идентификация товара
  проверяются как обычно. Контекст доступен только карточке R2.1 с отметкой SERIOUS_SAFETY, когда
  активный ответ написан оператором через «✏️ Изменить»; `LIVE_V2` для таких ответов не используется.
  Карточки R2 (флаг выключен) сохраняют поведение R2.
- **Качество:** `POSITIVE_CUSTOMER_SIGNAL_IGNORED` (смешанный отзыв, плюс покупателя потерян),
  `MODERATE_SAFETY_OVER_ESCALATION`, `MODERATE_SAFETY_BLANKET_STOP` — диагностика, не блокировка.
- Пилот R1 при включённом флаге не запускается. Автопубликации нет.

## 1.8.0 — R2: черновик 3.1E в карточке Telegram и кнопка «Опубликовать 3.1E» (2026-10-07)

Решение владельца после первых реальных кейсов: черновики V2 блокируются правилами публикации примерно
в 95% случаев, а черновики 3.1E проходят (25 из 26 на реальных коммуникациях).

- `V31_OPERATOR_DRAFT_ENABLED` (по умолчанию `false`): для каждой новой коммуникации готовится черновик
  3.1E (один вызов модели) и показывается в карточке под черновиком V2 («✨ Вариант 3.1E»), с кнопкой
  «✨ Опубликовать 3.1E». Где 3.1E требует проверки человеком, кнопки нет и в карточке стоит пометка.
  Бюджет времени опроса `V31_OPERATOR_DRAFT_BUDGET_SECONDS` (100 с): позже карточка уходит без 3.1E,
  а «Перегенерировать» доготавливает его. Сбой 3.1E никогда не задерживает карточку V2.
- «Опубликовать 3.1E» сначала проверяет, открыт ли шлюз публикации, затем атомарно принимает сохранённый
  текст 3.1E как следующую версию ответа (только из статусов, допускающих правку, и только при совпадении
  версии с карточкой; из `publishing`/`publish_unknown` — никогда), затем запускает **неизменённый**
  проверенный публикатор: правило → одна запись в WB → обратное чтение → «опубликовано» только после
  совпадения. Обе кнопки публикации привязаны к версии карточки: нажатие на устаревшей — отклоняется.
- **Правило выбирается действием** (ревью владельца перед слиянием): «✅ Опубликовать» и ручная правка
  проверяются production-совместимым `LIVE_V2`, как сейчас; только «✨ Опубликовать 3.1E» — политикой
  v3.1E. `V31_ENFORCE_LIVE_PUBLICATION_POLICY` в первом выпуске R2 остаётся `false`. Режим пишется в
  `publish_trace.publication_mode` (`live_v2` / `v31`).
- **Предварительная проверка до принятия.** «✨ Опубликовать 3.1E» проверяет точный сохранённый текст
  текущей политикой v3.1E до принятия: при BLOCK/ошибке ответ, версия, статус и WB не меняются
  (`v31_preflight_blocked` / `v31_preflight_failed`). Публикатор перепроверяет политику перед записью.
- **Видимость.** Кнопка «✨ Опубликовать 3.1E» есть только если весь точный текст 3.1E виден под своей
  меткой в этом же сообщении; иначе — «✨ Показать 3.1E полностью» (`s31:<doc>:<gen>`): отдельное
  сообщение с полным текстом и единственной кнопкой публикации, привязанной к версии карточки.
- Пилот R1 при включённом R2 не запускается (защита в `app/v3/pilot.py`). Автопубликации нет, owner
  override выключен. Рунбук — `R2_OPERATOR_ASSIST.md`.

## 1.7.1 — Теги покупателя WB и тёплый ответ на оценку без текста (2026-10-07)

По первому реальному кейсу пилота R1 (отзыв 5★ без текста, с тегами «Хорошо пахнет», «Удобно пользоваться»).

- **Теги WB (`bables`) больше не теряются.** `Review.bables` из API WB → документ Firestore (`bables`) →
  карточка Telegram («Теги покупателя: …», только если они есть) → запрос V2 (`Теги покупателя`, с
  пометкой, что WB не указывает плюс/минус) → v3.1E (`customer_text`: теги — слова покупателя для
  аспектов, атрибуции и классификации). В API WB у тегов нет знака: полярность берётся только из смысла
  фразы; нейтральные теги («Цена», «Качество») не считаются ни похвалой, ни жалобой. В BigQuery новое
  поле не передаётся (явное сопоставление колонок).
- **Ответ на оценку без слов.** 4–5★ без текста (или только с нейтральными тегами): обязательный аспект
  `rating_only_thanks` — благодарность за оценку, подтверждённое название товара и одна тёплая фраза без
  свойств и состава; детерминированный запасной вариант называет товар («…что Вы выбрали наш крем для
  рук»). 1–3★ без текста: сожаление и просьба рассказать, что не понравилось. Сухое «благодарим за
  высокую оценку!» оценщик качества помечает как пропуск аспекта.
- Новые формулировки тегов в аспектах: «хорошо пахнет», «удобно пользоваться», «хорошо увлажняет».
- Живой гейт публикации V2 (`validate_live_publication`, побайтная копия production) не менялся.
- После выкладки пилот R1 перезапускается новой активацией (см. `R1_SHADOW_PILOT.md`).

## 1.7.0 — Reviews & Q&A v3.1E: канонический движок и R1 zero-write shadow pilot (2026-10-06)

Одна каноническая линия нового движка ответов (Phase 3.1E, коммиты `8d4435f`, `2f439b8` поверх `main`),
заменяет runtime из PR #235/#236. Для покупателей по-прежнему работает **только v2**: `auto_publish=false`,
проверенный публикатор (P0, PR #234) не менялся, восстановление оператора и owner override выключены.

- **Слой 3.1E в shadow — только явное включение**: `V31_QUALITY_SHADOW_ENABLED` по умолчанию `false`
  (было `true`). Без переменной — ни одной оценки 3.1E и ни одного вызова LLM этого слоя;
  `V3_SHADOW_ENABLED` по-прежнему управляет только baseline v3.
- **Активация пилота** (`app/v3/pilot.py`): `V31_SHADOW_ACTIVATION_ID`, `V31_SHADOW_START_AT`,
  `V31_SHADOW_END_AT` (ISO-8601 со смещением; начало включительно, конец исключительно),
  `V31_SHADOW_PILOT_MAX_COMMUNICATIONS` (1…20). Любое некорректное значение или включённые
  `V31_OPERATOR_RECOVERY_ENABLED` / `V31_OWNER_OVERRIDE_ENABLED` — пилот выключен (fail-closed), v2 не затронут.
- **Только новые коммуникации**: и дата WB (`source_created_at`), и первое наблюдение сервиса (`first_seen_at`)
  не раньше начала окна; нет надёжной метки времени — пропуск `SHADOW_ELIGIBILITY_UNKNOWN_TIME`.
- **Бэкфилл удалён**: смена версии движка, промпта, политики или снапшота больше не переоценивает
  уже обработанные коммуникации слоем 3.1E.
- **Глобальный лимит и идемпотентность**: перед вызовом LLM — транзакционная заявка в Firestore
  `v31_shadow_pilot` (счётчик `A.<activation>` + заявка `C.<activation>.<sha1(id)>`). Повторный poll,
  перезапуск, параллельные инстансы и смена версий не дают второй оценки и не выводят пилот за лимит;
  21-я коммуникация — `PILOT_CAP_REACHED`. Не больше одной генерации на коммуникацию.
- **Журнал пилота** — только хеши, вердикты и идентификаторы; тексты остаются там же, где и раньше
  (`v3_shadow_runs`). Отчёт и оценка владельца: `scripts/r1_pilot_report.py`.
- **Живая публикация v2 изолирована от политики v3.1E**: путь публикации оператором проверяет текст
  production-совместимым гейтом `validate_live_publication` — `app/v3/verifier_live.py` побайтно равен
  `verifier.py` из main 3632bae (blob `52f6ba20`, закреплено тестом) плюс прежнее сопоставление
  ингредиентов. Новый верификатор (`validate_for_publication`) — только shadow. Перевод живой публикации
  на v3.1E — решение R2: `V31_ENFORCE_LIVE_PUBLICATION_POLICY=true`; с ним R1 не стартует. Owner override
  работает только вместе с этим флагом. Проверка на 240 реальных текстах v2: вердикты и наборы правил
  живого гейта совпадают с production 240/240.
- Исправлено: проверенная формулировка аромата в косвенном падеже («с древесно-удовым ароматом»)
  больше не блокируется в v3.1E; известный пробел: окончания вроде «со сладким ароматом» правилом не
  распознаются (задокументировано тестом, отдельная задача).
- Рунбук включения, kill switch и таймаута — `R1_SHADOW_PILOT.md`.

## 1.6.3 — v2: концентрация салициловой кислоты крема анти-акне убрана из контекста модели (ODR-07, 2026-09-29)

Hotfix по production-кейсу `879824da`: из `knowledge/products/acne_cream.md` (факты и текст) и из отката
`prompts/reviews/system_v1.txt` убрано значение концентрации, модели разрешено только «в составе есть салициловая кислота»;
регрессия `tests/engine/test_odr07_salicylic.py` проверяет собранный prompt крема и всех его наборов. v3, верификатор и
shadow не менялись; ниацинамид 5 %/6 % (T1, раскрытие по умолчанию) не затронут.

## 1.6.2 — v3 SHADOW: исправления по WP12 на реальных данных (PR #223, main `3e0466f`, 2026-09-29)

1.6.2 — код PR #223 (только `app/v3`, политика v3, тесты; вне v3 код не менялся): отрицание в сигналах упаковки,
код причины HUMAN_REVIEW, обращение по однобуквенному имени, тон при пустом отзыве, снимок знаний
`ks_v3_20260928T120346_4f4121d4`, регрессионный кейс `PROD-ce1a68fb`; образ `v3shadow-3e0466f`. v2 остаётся единственным
движком для покупателей, `auto_publish=false`, ручной верификатор только наблюдает.

## 1.6.1 — Аудируемый публичный вход: события auth_ok / mutation_* с trace запроса (D-19b)

Номер 1.6.1: 1.6.0 в `main` занят Reviews & Q&A v3 SHADOW (PR #219). При интеграции с `main` эта запись
встаёт над 1.6.0 v3, запись 1.5.3 сохраняется.

Развёртывание 1.6.1 — только образ (`gcloud run services update --image <digest>`), окружение не меняется.
`deploy/env.production.live.yaml` приведён к живому сервису: `V3_SHADOW_ENABLED` в production отсутствует
(v3 SHADOW выключен, код по умолчанию `false`); включение v3 — отдельное решение владельца.

Только инструментирование — поведение, маршруты, аутентификация и бизнес-логика не меняются.

- **Trace запроса**: middleware берёт trace из `X-Cloud-Trace-Context` / `traceparent`; каждая строка журнала
  получает `logging.googleapis.com/trace` = `projects/<GCP_PROJECT_ID>/traces/<id>` — тот же trace, что у
  журнала запроса Cloud Run (машинная связь, не временная близость).
- **События `audit_event`** (схема `wbc-audit/1`, закрытый набор полей, без свободного текста и секретов):
  `instrumentation_ready` (при старте экземпляра), `auth_ok` / `auth_denied` (секрет Telegram, allow-list
  Telegram, секрет Scheduler, admin-токен), `mutation_attempt` / `mutation_success` / `mutation_failure`
  (каждая запись в WB через `_write_once`, каждый не-`get*` вызов Telegram Bot API). Цель — только хеш
  (`target_ref`), `service`/`revision` — из `K_SERVICE`/`K_REVISION`.
- Эмиссия событий никогда не бросает исключений.
- Серверный `request_id` (uuid4) в каждом событии — trace клиентоуправляем; открытый allow-list (пустые списки)
  фиксируется как `result=open_no_allowlist`, не как проход; логгер `app.audit` всегда INFO.
- **`request_end`** в конце КАЖДОГО запроса (и при исключении): статус и число `mutation_attempt` — аудит доказывает,
  что ни одно событие изменения не потеряно; частичный allow-list — `result=ok_partial_allowlist`.
- **Закрытый словарь маршрутов** в событиях безопасности (`audit_route`): путь запроса клиентоуправляем
  (Starlette раскодирует `%xx`, в нём бывают формы секретов, unicode, «query» внутри пути), поэтому в
  `request_end.route` и в поле `route` любого события пишется только точный известный маршрут
  (`/health`, `/poll`, `/telegram-webhook`, `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`),
  категория `/admin` для admin-роутера или `UNKNOWN`. Сырой путь в события не попадает; маршрутизация,
  аутентификация и поведение WB/Telegram не меняются. Метка берётся из `scope["path"]` — той же строки, по
  которой маршрутизирует Starlette: `request.url.path` пересобирается из заголовка Host, режется на
  закодированных `?`/`#` (`/poll%3Fx=1` получил бы метку `/poll`) и на кривом Host бросает, подавляя `request_end`
  (Starlette 0.41.3 из закреплённых версий).

## 1.6.0 — Reviews & Q&A v3 in SHADOW mode (Phase 3, 2026-09-28)

Production behaviour unchanged: v2 still generates every card; v3 is observation only
(no WB write, no Telegram message, no v2 Firestore write). Enabled by `V3_SHADOW_ENABLED`.

- **Knowledge registry** (`knowledge_v3/registry/*.yaml` -> BigQuery `evetis_ref`, 10 new
  tables, additive; DDL/rollback in `sql/ref/v3_knowledge_registry*.sql`). T1 digitized for all
  11 singles (recipes checked to 100 %, label INCI, usage, PAO, provenance with file sha256);
  bundles and WB identity are READ from `REF_BUNDLE_COMPONENTS` / `REF_SKU_CHANNEL_MAP`.
- **Immutable snapshot** `app/v3/snapshots/<id>.json` built from BigQuery by
  `scripts/v3_registry.py build-snapshot` behind a knowledge build gate; selected by
  `snapshots/ACTIVE` or `V3_KNOWLEDGE_SNAPSHOT_ID`; hash-checked on load.
- **Engine** `app/v3`: deterministic product resolver; classifier (rules + optional LLM that can
  only add/raise); safety extractor (negation, temporal state, EM-01..06, R4 needs a marker);
  required-fact planner (KNOWN_ALLOWED / KNOWN_RESTRICTED / CONFLICT / UNKNOWN); controlled
  generator (structured evidence only, templates rendered without LLM); deterministic verifier
  (same code for AI drafts, v2 drafts and manual edits).
- **Shadow runner** at the end of `/poll` (time-boxed, fully isolated) + decision journal
  `evetis_communications.communication_v3_decisions`; ledger `v3_shadow_runs` (Firestore).
- Admin: `POST /admin/v3/shadow-run`, `GET /admin/v3/decision/{id}` (inspection, no actions).
- `V3_ENFORCE_MANUAL_EDIT_VERIFIER` (default **false**): owner-gated publish gate for manual text.
- `OpenAIClient.structured()` added (JSON schema); `generate_answer` untouched.
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
- Owner 28.09 / ODR-12: enzyme powder frequency «2–3 раза в неделю» removed (not in T1 SP-TS-8);
  powder «для лица и тела» -> «для лица» (ODR-13); «без кислот» removed (KC-17, Ascorbic Acid 0,2 %).
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
