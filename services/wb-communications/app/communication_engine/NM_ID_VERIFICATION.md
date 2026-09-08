# WB identifier mapping — set_acne_3step (resolved)

Этот движок — **только Wildberries**. Здесь хранятся исключительно WB-идентификаторы.

## set_acne_3step — итоговые идентификаторы
Владелец подтвердил соответствие для набора «3 шага, акне»:

| Поле | Значение | Где хранится |
|------|----------|--------------|
| WB nmId | `910584041` | `wb_nm_ids` |
| barcode (GTIN) | `2049910829446` | `barcodes` |
| supplier_article (vendorCode) | не подтверждён | `supplier_articles: []` (не дублируем nmId) |
| Ozon SKU | относится к **Ozon** | **не хранится** в этом WB-движке |

Ранее в `wb_imt_ids` лежало значение, которое оказалось **Ozon SKU**, а не WB
imtID. Оно полностью удалено из WB knowledge base: под Ozon будет отдельная
система со своими товарами и идентификаторами — сюда Ozon-поля не подмешиваются.

## Первичный источник (WB API), не URL и не SellMonitor
Собственный сборщик `_Реклама_API/wb_collect.py` дергает **WB content API**
`POST https://content-api.wildberries.ru/content/v2/get/cards/list` и читает поле
`c.get("nmID")`. Его сохранённый вывод — `_Реклама_API/Отчёты/data_2026-07-10.json`
— подтверждает nmID по каждой рекламной кампании EVETIS. Подтверждённые в payload
nmID: 305101272, 305101361, 438775617, 535580776, 535581674, 535581675, 593111985,
868597351, 910584041 (см. `RAW_WB_NMIDS_2026-07-10.json`).

## Раздельное хранение идентификаторов
Поля не смешиваются:
- `supplier_articles` — артикул(ы) продавца (vendorCode);
- `wb_nm_ids` — WB nmID;
- `wb_imt_ids` — WB imtID (родительская карточка);
- `barcodes` — WB штрихкод / GTIN;
- `id_status: verified|unverified`.

`id_status` у товаров остаётся `unverified` до полной сверки дампом `cards/list`
с полями `nmID`+`imtID`+`vendorCode`+`barcode`. Резолв отзывов надёжен уже сейчас:
WB-фидбек несёт `nmId`, совпадающий с `wb_nm_ids`; добавлен резолв по `barcode`.

## Порядок резолва товара
`supplier_article → nm_id → barcode → alias/product_name`. Не найдено — товар
`unresolved`, элемент уходит в ручную модерацию.
