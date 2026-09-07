/**
 * Константы наблюдателя цен WB (PR-1). READ-ONLY.
 *
 * Здесь и нигде больше зафиксирован контракт полей ответа WB. Любое расхождение
 * фактического ответа с этими списками попадает в WB_PRICES_OBSERVATIONS.schema_status
 * и не растворяется молча: цена — коммерчески чувствительная величина, и «новое поле,
 * которого мы не заметили» здесь дороже, чем упавший прогон.
 */

/** Хост категории «Цены и скидки». Другие категории WB живут на других хостах. */
export const WB_PRICES_HOST = 'https://discounts-prices-api.wildberries.ru';

/** Единственный вызываемый путь. READ. Мутирующие пути этого хоста в коде отсутствуют. */
export const WB_PRICES_PATH = '/api/v2/list/goods/filter';

export const WB_PRICES_SOURCE_ENDPOINT = `GET ${WB_PRICES_PATH}`;

/**
 * Максимум элементов на страницу по контракту WB (limit ≤ 1000).
 * Ассортимент EVETIS — десятки SKU, поэтому снимок укладывается в одну страницу;
 * пагинация тем не менее реализована, чтобы рост ассортимента не обрезал снимок молча.
 */
export const WB_PRICES_PAGE_LIMIT = 1000;

/** Предохранитель от бесконечной пагинации при аномальном ответе. */
export const WB_PRICES_MAX_PAGES = 20;

/** Поля элемента listGoods[], известные нашему контракту. */
export const KNOWN_ITEM_FIELDS: readonly string[] = [
  'nmID',
  'vendorCode',
  'sizes',
  'currencyIsoCode4217',
  'discount',
  'clubDiscount',
  'editableSizePrice',
  'wholesaleDiscountThreshold',
  'isBadTurnover',
];

/** Поля элемента sizes[], известные нашему контракту. */
export const KNOWN_SIZE_FIELDS: readonly string[] = [
  'sizeID',
  'price',
  'discountedPrice',
  'clubDiscountedPrice',
  'techSizeName',
];

/**
 * Поля, без которых наблюдение бессмысленно. Их отсутствие — отказ прогона,
 * а НЕ подстановка нуля: 0 — валидная цена, и спутать её с «нет данных» нельзя.
 */
export const REQUIRED_ITEM_FIELDS: readonly string[] = ['nmID', 'sizes'];
export const REQUIRED_SIZE_FIELDS: readonly string[] = ['price'];

export type SchemaStatus = 'OK' | 'DRIFT_NEW_FIELDS' | 'DRIFT_MISSING_FIELDS';
