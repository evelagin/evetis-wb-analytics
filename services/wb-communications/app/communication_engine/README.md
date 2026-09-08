# EVETIS Communication Engine

Движок генерации ответов на отзывы/вопросы **только Wildberries**. Ozon сюда
намеренно не подмешивается — под него будет отдельная система. Не «Prompt Builder», а
подсистема знаний бренда: промпт всегда **собирается автоматически** из отдельно
лежащих знаний, код не меняется при добавлении товара или сценария.

## Поток

```
ReviewInput
   │
   ▼
ReviewClassifier ─► Classification{ marketplace, product_id, rating,
   │                               sentiment, scenario[], language, urgency }
   ▼
ContextBuilder ──► PromptContext{ blocks[] }        (через KnowledgeRegistry)
   │
   ▼
PromptBuilder ───► PromptBundle{ system, user, prompt_version }
   │
   ▼   (LLM генерирует ответ)
   ▼
ValidationPipeline ─► ValidationResult{ ok, issues[] }
```

Классификаторы **детерминированные**: продукт — по артикулу, sentiment — по
рейтингу, scenario — по ключевым словам из front-matter кейсов, language — по доле
кириллицы, urgency — по правилам. Без LLM-вызовов, полностью тестируемо.

## Слои

- `constants.py`, `config.py` — все enum'ы, пути и пороги (никаких «магических строк»).
- `models/` — Pydantic v2: `ReviewInput`, `Classification`, `Product`,
  `KnowledgeDocument/Ref`, `PromptContext/Block/Bundle`, `ValidationResult`.
- `knowledge/registry.py` — `KnowledgeRegistry` + `KnowledgeSource` (ABC) +
  `MarkdownKnowledgeSource`. Единственная точка, знающая о хранилище.
- `classifier/` — marketplace / product / sentiment / review (scenario, language, urgency).
- `builder/` — `ContextBuilder` (декларативно резолвит документы) и `PromptBuilder`
  (только структура, никаких знаний внутри).
- `validators/` — tone, medical, length, duplicate, hallucination + pipeline.
- `engine.py` — `CommunicationEngine` (composition root, DI).
- `knowledge/` — база знаний EVETIS (brand / products / cases / marketplaces).

## Формат знаний

Каждый документ — markdown с YAML-заголовком:

```markdown
---
id: enzyme_powder
version: 1
tags: [enzyme, powder, cleansing]
articles: ["535580776"]
actives: [папаин, аллантоин, витамин с]
---
Тело документа…
```

- product: `articles`, `actives` → строят индекс артикулов и лексикон компонентов.
- case: `keywords`, `urgency`, `priority` → детект сценария и срочности.
- brand/forbidden: `forbidden_phrases`, `forbidden_openers`, `medical_forbidden` → валидаторы.

## Расширение без кода

- Новый товар — добавить `products/<id>.md` с `articles`/`actives`.
- Новый сценарий — добавить `cases/<id>.md` с `keywords`.
- Новый канал WB (вопросы/чат) — добавить `marketplaces/<id>.md` при необходимости.
- Сменить хранилище (Markdown → БД/CMS) — реализовать свой `KnowledgeSource`, отдать
  его в `KnowledgeRegistry`. `ContextBuilder` и остальное не меняются.

## Использование

```python
from app.communication_engine.engine import CommunicationEngine
from app.communication_engine.models.classification import ReviewInput

engine = CommunicationEngine.build()
review = ReviewInput(platform="wb", rating=2, supplier_article="305101361",
                     text="Появилось раздражение и жжение")
bundle = engine.build_prompt(review)          # bundle.system / bundle.user / bundle.prompt_version
# ... отправить в OpenAI, получить answer ...
result = engine.validate(answer, engine.build_context(review), recent_answers=())
if not result.ok:
    ...  # ошибки в result.errors — перегенерировать / отправить на ручную правку
```

## Интеграция (PR7)

`app/services/engine_prompt_service.py` (`EnginePromptService`) конвертирует
существующий `Review` в `ReviewInput` и отдаёт `PromptBundle`. Переключение с
`reviews_v1`: в `app/dependencies.py` заменить `PromptService` на
`EnginePromptService`, а в `run_poll` использовать `build_prompt(review)` (теперь
он возвращает и system, и user — системный промпт стал контекстным) и запускать
`validate()` после генерации. Пока не подключено, чтобы прод-контур оставался зелёным.

## Требования

Python 3.13 (совместимо с 3.11+), Pydantic v2, PyYAML. Полная типизация, docstrings,
SOLID, DI, без циклических импортов. Тесты — `tests/engine/`, покрытие ≥90%.
