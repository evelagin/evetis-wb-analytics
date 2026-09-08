# Миграция `bundle_components` → внутренние `product_id`

Раньше компоненты наборов хранились внешними числовыми nmId, что снова связывало
внутреннюю структуру с неоднозначными внешними идентификаторами. Теперь компоненты
ссылаются на внутренние `product_id`. Соответствие nmId → product_id:

| nmId | product_id |
|------|------------|
| 438775617 | acne_cream |
| 305101361 | acne_serum |
| 535581675 | acne_toner |
| 535580776 | enzyme_powder |
| 252442517 | hand_cream |
| 438775437 | moisturizing_cream |
| 305101272 | moisturizing_serum |
| 535581674 | moisturizing_toner |
| 593111985 / 593111986 | body_cream (обе ароматические версии = один product_id) |

## Мигрированные наборы (было → стало)

| Набор | Было (nmId) | Стало (product_id) |
|-------|-------------|--------------------|
| set_acne_2step_tc | 535581675, 438775617 | acne_toner, acne_cream |
| set_acne_2step_ts | 535581675, 305101361 | acne_toner, acne_serum |
| set_acne_3step | 535581675, 305101361, 438775617 | acne_toner, acne_serum, acne_cream |
| set_acne_4step | 535580776, 535581675, 305101361, 438775617 | enzyme_powder, acne_toner, acne_serum, acne_cream |
| set_acne_serum_cream | 305101361, 438775617 | acne_serum, acne_cream |
| set_body_duo | 593111986, 593111985 | body_cream¹ |
| set_hand_body_amber | 252442517, 593111985 | hand_cream, body_cream |
| set_hand_body_cherry | 252442517, 593111986 | hand_cream, body_cream |
| set_moist_2step_tc | 535581674, 438775437 | moisturizing_toner, moisturizing_cream |
| set_moist_2step_ts | 535581674, 305101272 | moisturizing_toner, moisturizing_serum |
| set_moist_3step | 535581674, 305101272, 438775437 | moisturizing_toner, moisturizing_serum, moisturizing_cream |
| set_moist_serum_cream | 305101272, 438775437 | moisturizing_serum, moisturizing_cream |

¹ Обе позиции набора (Вишня + Амбра) — это один `product_id` `body_cream`
(документ покрывает оба аромата), поэтому компонент указан один раз: схема
запрещает дубли `product_id` в `bundle_components`.

Схема (`registry.validate_schema()`) теперь проверяет, что каждый компонент
существует как товар, не повторяется и набор не ссылается сам на себя.
