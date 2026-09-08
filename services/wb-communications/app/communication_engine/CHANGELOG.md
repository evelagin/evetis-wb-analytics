# Communication Engine — CHANGELOG

## v2.5 — WB-only cleanup: barcode, internal bundle ids, Ozon SKU purged

Local: **141/141 tests**, engine builds fail-fast, `validate_knowledge()` clean.
Продовый контур `reviews_v1` не тронут.

1. **Ошибочный не-WB SKU полностью удалён** из WB knowledge base (front-matter,
   `RAW_WB_NMIDS_2026-07-10.json`, `NM_ID_VERIFICATION.md`, CHANGELOG). Значение
   оказалось SKU другой площадки, а не WB imtID, — здесь оно не хранится. Убраны
   все формулировки «likely / candidate imtID».
2. **`set_acne_3step` — чистые идентификаторы.** `wb_nm_ids: ["910584041"]`,
   `wb_imt_ids: []`, `barcodes: ["2049910829446"]`, `supplier_articles: []`
   (vendorCode не подтверждён — nmId в supplier_articles НЕ дублируем).
3. **barcode — первоклассное WB-поле.** `MetaKey.BARCODES`, `Product.barcodes`,
   `registry.barcode_index()` / `product_id_for_barcode()`, `ReviewInput.barcode`,
   `Review.barcode`. Порядок резолва: `supplier_article → nm_id → barcode → alias`.
4. **`bundle_components` — внутренние `product_id`, не внешние числа.** Все 12 наборов
   переведены с числовых nmId на `product_id` (список миграции — в `MIGRATION_bundle_components.md`).
   `ContextBuilder`/`allowed_actives` резолвят компоненты напрямую по `product_id`.
5. **MarketplaceClassifier больше не превращает чужую площадку в WB.** Неизвестный/
   не-WB `platform` → `marketplace=None`, `marketplace_resolution_status=UNRESOLVED`,
   `needs_manual_moderation=True`. Допустимы только `wb`/`wildberries`; пустой
   `platform` тоже unsupported (WB гарантирует только upstream-адаптер, нормализуя
   его в `"wb"`). Добавлен `classify_strict()` → `UnsupportedMarketplace`.
6. **Схема строже.** Уникальность `barcode`; `bundle_components` обязаны
   ссылаться на существующие `product_id`, без дублей и без ссылки набора на себя.

## v2.4 — Wildberries-only (Ozon removed)

По решению владельца движок ведёт **только Wildberries**. Ozon больше не
подмешивается: под Ozon будет отдельная система (свои товары, свои отзывы), это
не управляется «с одного пульта».

1. **`Marketplace` enum — только `WB`.** Значение `OZON` убрано.
2. **`MARKETPLACE_ALIASES`** — убраны `ozon`/`оzon`. (В v2.5 поведение для
   неизвестной площадки ужесточено: не WB по умолчанию, а unsupported.)
3. **`config.marketplace_docs`** — убран маппинг `Marketplace.OZON: "ozon"`.
4. **Удалён** `knowledge/marketplaces/ozon.md`.
5. Docstring `ReviewInput`, README, VERIFIED_SOURCE_DATA, CHANGELOG — переписаны
   на WB-only. Тесты классификатора (`platform="ozon"` → теперь ожидают WB).
6. Продовый контур `reviews_v1` не тронут (легаси-промпт оставлен как есть).

## v2.3 — id-separation + entity-type guard (pre-shadow)

Local: **126/126 tests, engine coverage 98%**, engine builds fail-fast. Prod untouched.

1. **Identifiers separated, not mixed.** New per-product fields:
   `supplier_articles`, `wb_nm_ids`, `wb_imt_ids`, `id_status: verified|unverified`.
   `id_status: unverified` on all products until the full WB id-reconciliation.
2. **Raw WB payload check (not URL/SellMonitor).** From your own pipeline output
   `_Реклама_API/Отчёты/data_2026-07-10.json` (WB content/adv API, field `nmID`/`nms`
   read by `wb_collect.py`): the numbers ARE the WB nmID. Saved raw extract:
   `RAW_WB_NMIDS_2026-07-10.json`; analysis: `NM_ID_VERIFICATION.md`. (The disputed
   non-WB value was later confirmed to be an Ozon SKU and removed entirely in v2.5.)
   Full `verified` status pending a `cards/list` dump with `nmID`+`imtID`+`vendorCode`.
3. **`type_for_entity` no longer defaults to review.** Unknown WB entity type raises
   `UnsupportedEntityType` (controlled) → caller routes to manual moderation. Test:
   `type_for_entity("unknown_type")` raises (does not return REVIEW).

## v2.2 — pre-PR7 technical round (audit items 1–10)

Local: **125/125 tests, engine coverage 98%**, engine builds with fail-fast schema
validation active. Production pipeline still untouched.

1. **nm_id verified, not invented.** Pulled authoritative WB data via SellMonitor
   (brand EVETIS 2704704, merchant ИП Елагина 4094555 = your account): the numbers
   in `articles`/`nm_ids` ARE the real WB nmId (URL id); for the 3-step acne set the
   WB nmId is 910584041. Table + note: `NM_ID_VERIFICATION.md`. Also surfaced 3
   brand-hijack cards (not our SKU) — excluded. (The disputed non-WB number was later
   confirmed to be an Ozon SKU and purged in v2.5.)
2. **Adapter passes communication_type.** `EnginePromptService.build_prompt/build_context/
   to_input` accept `communication_type`; `type_for_entity()` maps WB entity → type.
   Tests via the adapter (question).
3. **Fail-fast schema validation on startup.** `CommunicationEngine.build()` raises
   `KnowledgeError` if `validate_schema()` finds problems, unless
   `VALIDATE_KNOWLEDGE_ON_STARTUP=false`. Test with a duplicate-article base.
4. **ContextBuilder** catches only `KnowledgeError` (not bare `Exception`) — real
   parse/programming errors now propagate.
5. **YAML is the real source.** `verified_facts`/`verified_claims` are folded into the
   grounding body automatically; `prohibited_claims` become `ContextBlock.constraints`,
   rendered as «Нельзя заявлять …» in the system prompt — and deliberately kept OUT of
   the grounding text so the numeric validator never grounds a forbidden example number.
6. **Bundle hallucination = union of components.** `registry.allowed_actives()` returns
   the union of a bundle's components' actives; a set answer may name any component
   ingredient but a foreign known active (e.g. сквалан in an acne set) is flagged.
   (Note: an entirely non-lexicon word like «ретинол» is handled by `prohibited_claims`
   instructions + numeric checks, not the ingredient validator.)
7. **Split `verified_fields` → `verified_facts` (physical/concentration/regime) +
   `verified_claims` (approved effects) + `prohibited_claims`.** Presence of an
   ingredient per ТУ no longer implies an approved marketing effect.
8. Test: article absent + real nm_id → resolved via nm_id.
9. Test: unknown nm_id + ambiguous name → `needs_manual_moderation`.
10. Full pytest + this changelog. Numeric validator remains **lexical grounding**, not
    semantic — documented as a safety net, not absolute protection.

## v2.1 — corrective round (verified knowledge + architecture)

Knowledge rebuilt from the owner's **verified sources** (`ИТОГ_WB_<article>.md`,
раздел «Активы по ТУ» + `EVETIS_WB_живой_дамп_описаний_и_составов`), not the old
marketing prompt. Production pipeline untouched. Local: **119/119 tests, engine
coverage 99%, `validate_knowledge()` clean**.

### Data corrections (removed unverified / wrong facts)
- **enzyme_powder**: было «75 г / флакон 120 мл, для лица». Стало **75 г, для лица
  И ТЕЛА, pH 8,0–8,5 (verified по ТУ)**; убраны непроверенные проценты ПАВ
  (33/30/20) и «папаин 0,2%»; добавлены **B12 и масло макадамии**. (Аудитор с
  «120–125 г» ошибся — 75 г подтверждён.)
- **acne_serum**: убран **pH 6,94** (нет в ТУ), «альфа-глюкан 1%», «каприлиловая
  кислота». Оставлены verified: ниацинамид 5% / чайное дерево 5% / гликолевая 2% /
  салициловая 1% / LHA 1% / цинк PCA 0,2%.
- **moisturizing_toner**: убраны «Bifida 10% / зелёный чай 10% / инулин 2%»;
  зафиксировано **гиалуроновая 0,2% (не 2%)**, pH 5,0–5,5.
- **moisturizing_cream**: убраны трейс-проценты (сквалан 2% и т.п.); оставлены
  verified **алоэ 40%, цетиловый спирт 2%**; «без спирта» запрещено.
- **acne_cream**: убраны «масло ши 4,5% / виноград 4,5% / чайное дерево 0,75% /
  трегалоза 0,75%»; оставлены verified **ниацинамид 6% / салициловая 2,25%**.
- **acne_toner**: убраны все непроверенные проценты; pH 5,5–6,5; «цинк» запрещён.
- **hand_cream**: **полностью переписан** — реальный состав (масло ши, миндаль,
  рис, ниацинамид, мочевина 0,1%, дамасская роза), аромат **Oud & Wood**, 300 мл,
  «для рук И ТЕЛА». Прежний состав (касторовое, «ниацинамид 1%») был неверен.
- **moisturizing_serum**: убран «гамамелис 2%» (в ТУ — хвощ 3%) и выдуманный pH;
  оставлены verified вит С 8% / гиалурон 1,3% / вит Е 5% / жожоба 5% / центелла 2% /
  МСМ 1%.
- **body_cream**: убран «глицерин 10%»; «масло розы» → «масло розы ругоза»;
  зафиксировано **6 типов церамидов**; ароматы 985=Amber Vanilla, 986=Вишня.

Все числовые примеры «лжи» (напр. «ниацинамид 10%», «мочевина 10%») вынесены в
`prohibited_claims` (front-matter) и **удалены из тела документов**, чтобы
numeric-валидатор не «заземлял» их случайно.

### New YAML schema (per product)
`source_status: verified|draft|unverified`, `verified_fields: [...]`,
`prohibited_claims: [...]`, `volume_ml`/`weight_g`, `ph`, `aroma`, `country`,
`declaration`, `age`, `nm_ids`, `aliases`; для наборов — `is_bundle: true`,
`bundle_components: [articles]`. Все 21 документ — `source_status: verified`.

### Architecture (пункты аудита 1–11)
1. **communication_type** (`review|question|chat`) в `ReviewInput`/`Classification`;
   отдельный user-шаблон на каждый тип (`PromptBuilder`).
2. **Наборы как сущности** — 12 bundle-документов с `bundle_components`;
   `ContextBuilder` разворачивает состав каждого компонента.
3. **Неизвестный товар → manual moderation**: `product_resolution_status=unresolved`,
   `Classification.needs_manual_moderation`.
4. **Сценарии с отрицанием**: `ScenarioDetector` игнорирует ключ при отрицании
   до/после («аллергии не было»).
5. **NumericGroundingValidator**: любой числовой продуктовый факт (%, pH, объём/вес,
   срок, частота) обязан присутствовать в выданных знаниях; иначе — ERROR.
6. **Schema-валидация** (`registry.validate_schema()` / `engine.validate_knowledge()`):
   уникальность articles/nm_id, обязательные поля, корректный `source_status`.
7. **Приоритет резолва товара**: article → nm_id → alias/product_name.
8. **Нейтральное обращение**: дефолт «Покупательница» убран; без имени — не обращаться.
9. см. п.5 (numeric hallucination).
10. см. п.6 (schema validation at startup/CI).
11. Production pipeline не изменён. См. таблицу и тесты ниже.

### Product ↔ article table
9 единичных товаров + 12 наборов (21 документ). Полная таблица — в
`evetis_verified_product_data.md` и через `registry.products()`.

### Tests added (tests/engine/test_corrective.py)
unresolved product; bundle resolve + component-blocks; bundle не флагает компонентный
ингредиент; question/chat типы; numeric hallucination (invented % / pH — flagged,
verified — ok); отрицание сценария; нейтральное обращение; schema-валидация;
все продукты verified. Всего по движку — 54 теста, покрытие 99%.

### Осталось (следующий раунд)
- Живая сверка через SellMonitor/BigQuery при желании.
- PR7 — подключение движка в pipeline (после ревью базы).
