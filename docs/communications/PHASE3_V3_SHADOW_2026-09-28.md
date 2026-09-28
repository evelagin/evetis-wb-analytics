# Reviews & Q&A v3 — Phase 3: Structured Knowledge, Policy Engine, Verifier, Shadow Mode

Дата: 2026-09-28. Сервис: `services/wb-communications` (1.6.0). Статус: SHADOW (v3 не влияет на ответы покупателям).

## 1. Что работает

```
сообщение WB → резолвер продукта (nmId → SKU/набор) → классификатор (правила + LLM, LLM только добавляет)
→ извлечение безопасности (отрицание, время, EM-01..06) → планировщик нужных фактов
→ разрешение факта: KNOWN_ALLOWED / KNOWN_RESTRICTED / CONFLICT / UNKNOWN → стратегия
→ генерация (шаблоны — без LLM; остальное — LLM только по разрешённым фактам)
→ детерминированный верификатор → журнал решения (BigQuery)
```

Стратегии: FACT_ANSWER · SERVICE · SAFETY_TEMPLATE · ACKNOWLEDGEMENT · CLARIFICATION_REQUIRED · UNKNOWN_FACT · HUMAN_REVIEW; итог BLOCK — если верификатор заблокировал черновик.

v2 остаётся production-генератором. v3 запускается в конце `/poll` (после всей работы v2, в пределах бюджета времени), на реальных обращениях из Firestore, пишет только в свои хранилища.

## 2. Где что лежит

| Что | Где |
|---|---|
| Реестр (авторская точка правды) | `services/wb-communications/knowledge_v3/registry/*.yaml` |
| Политика runtime (шаблоны, маркеры, лексиконы) | `knowledge_v3/policy/policy_v3.yaml` |
| Реестр в BigQuery | `evetis_ref.REF_KNOWLEDGE_SOURCE`, `REF_PRODUCT_KNOWLEDGE_PROFILE`, `REF_PRODUCT_IDENTIFIER`, `REF_PRODUCT_FACT`, `REF_USAGE`, `REF_PRODUCT_INGREDIENT`, `REF_INGREDIENT`, `REF_CLAIM`, `REF_KNOWLEDGE_CONFLICT`, `REF_OWNER_DECISION`, журнал `KNOWLEDGE_SNAPSHOT` |
| Читаются, не пишутся | `evetis_ref.REF_SKU_CHANNEL_MAP` (WB), `REF_PRODUCT_MASTER`, `REF_BUNDLE_COMPONENTS` |
| Снимок (неизменяемый) | `app/v3/snapshots/<snapshot_id>.json` + `.report.json`; активный — `snapshots/ACTIVE` |
| Журнал решений | `evetis_communications.communication_v3_decisions` (партиция по `created_at`) |
| Метрики | `V_V3_SHADOW_LATEST`, `V_V3_SHADOW_METRICS`, `V_V3_V2_FAILURE_CLASSES` (`sql/communications/v3_shadow_views.sql`) |
| Реестр обработанных | Firestore `v3_shadow_runs` (v2-документы не изменяются) |
| Золотой корпус | `tests/v3/golden_corpus.yaml` |
| Разметка исторического корпуса | `knowledge_v3/eval/historical_labels.yaml` (без текстов покупателей) |

## 3. Отклонения от задания (осознанные)

1. **`REF_PRODUCT_KNOWLEDGE_PROFILE`** — дополнительная таблица для заголовка продукта (тип, клиентское название, `spec_association_status`, `customer_fact_generation`). В перечне задания таких полей нет.
2. **Идентичность и BOM не дублируются.** `REF_PRODUCT_IDENTIFIER` хранит только типы, которых нет в `REF_SKU_CHANNEL_MAP`: EAN этикетки и код ТУ. nmId, vendorCode и наборы берутся из существующих авторитетных таблиц при сборке снимка.
3. **`REF_BUNDLE_COMPONENTS`** используется как есть (существующая таблица), новая не создаётся.
4. **Политика хранится в YAML репозитория и встраивается в снимок**, а не в таблицу BigQuery. Решения владельца лежат в `REF_OWNER_DECISION` и ссылаются на шаблоны политики.
5. **Стратегия ACKNOWLEDGEMENT** добавлена для отзывов без вопроса (похвала и опыт). В ответе нет фактов о продукте.
6. **Верификатор полностью детерминированный** (стемы, совместная встречаемость в предложении, числа с привязкой к единице). LLM-судьи нет: это слой, который можно добавить позже как WARNING.

## 4. Безопасные умолчания, добавленные в этой фазе

- **ODR-20 (новое, ждёт владельца).** В SP-TS-6 феноксиэтанол — 1,50 % (проверено визуально). До решения значение не раскрывается, по аналогии с ODR-07; присутствие ингредиента раскрывается.
- **Состав при недоступном INCI этикетки** (тоники и пудра: поле пустое в ТУ) → HUMAN_REVIEW. Вопросы о наличии или концентрации ингредиента отвечаются по рецептуре T1a.
- **Регуляторные вопросы** → HUMAN_REVIEW: оцифровки T1c нет, номера в OCR выписок искажены.
- **Способ применения крема УВЛ** = UNKNOWN: в ТУ поле пустое, из крема АКНЕ не переносится.
- **SP-TS-2** в самом документе суммируется в 100,3 % (все 27 строк проверены визуально). Стоит флаг `RECIPE_SUM_MISMATCH`, это предупреждение, а не ошибка.

## 5. Runbook

```bash
cd services/wb-communications
python scripts/v3_registry.py validate                  # офлайн-гейт seed
python scripts/v3_registry.py load-bq                   # полная перезагрузка v3-таблиц evetis_ref
python scripts/v3_registry.py build-snapshot --activate # снимок из BigQuery + гейт + журнал
python scripts/v3_registry.py verify-snapshot           # хэш, гейт, совпадение с seed
python scripts/v3_shadow_report.py                      # отчёт shadow (только чтение)
```

Порядок деплоя:
1. Миграция таблицы решений `ensure_v3_decisions_schema()`.
2. Представления из `sql/communications/v3_shadow_views.sql`.
3. `python3 deploy/preflight_env.py --file deploy/env.production.live.yaml --accept-changes`. Единственная ожидаемая дельта — `V3_SHADOW_ENABLED`.
4. Деплой по digest с `--update-env-vars V3_SHADOW_ENABLED=true`.

**Откат v3:** `--update-env-vars V3_SHADOW_ENABLED=false` (v2 не затронут) или откат ревизии. Снимок откатывается через `V3_KNOWLEDGE_SNAPSHOT_ID=<старый id>`. Таблицы — `sql/ref/v3_knowledge_registry_rollback.sql`; runtime BigQuery не читает.

## 6. Известные ограничения

- Верификатор ловит смысл через лексиконы и стемы. Перефразировки вне лексиконов возможны: их ловит проверка чисел, ингредиентов и зон применения, но не всегда.
- Стоимость LLM рассчитывается по токенам и справочной цене из политики, это оценка.
- Промпт v1 (`prompts/reviews/system_v1.txt`) содержит устаревшие формулировки. Он достижим только при явном откате конфигурации (`COMMUNICATION_ENGINE_V2_PRIMARY=false`) и оставлен как исторический материал.
