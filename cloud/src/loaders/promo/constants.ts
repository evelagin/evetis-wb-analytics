/**
 * Константы наблюдателя акций WB (PR-PROMO-1). READ-ONLY.
 *
 * Здесь и нигде больше зафиксирован контракт полей ответа WB и — главное —
 * ЗАКРЫТЫЙ список путей, которые наблюдателю разрешено вызывать.
 *
 * 🔴 ГРАНИЦА ЗАПИСИ. Токен категории «Цены и скидки» даёт И чтение, И запись,
 * если в JWT не выставлен бит 30 (read-only). У EVETIS бит выставлен (проверено
 * 2026-09-22, срок до 2027-03-08), но полагаться только на это нельзя: секрет в
 * runtime — это возможность, а не только намерение. Поэтому граница дублируется
 * в коде: ALLOWED_PATHS — единственный источник путей запроса.
 *
 * Список ЗАПРЕЩЁННЫХ путей намеренно живёт не здесь, а в test/promo_security.test.ts:
 * тест сканирует исходный текст всех модулей каталога promo/ на эти строки, и если
 * держать их в самом каталоге, тест ловил бы собственное определение. Наблюдатель
 * не должен содержать мутирующий путь даже в виде константы.
 */

/** Хост категории «Календарь акций». Другие категории WB живут на других хостах. */
export const WB_PROMO_HOST = 'https://dp-calendar-api.wildberries.ru';

/** Список акций. READ. */
export const WB_PROMO_LIST_PATH = '/api/v1/calendar/promotions';
/** Детали акций (условия, счётчики, лестница бустинга). READ. */
export const WB_PROMO_DETAILS_PATH = '/api/v1/calendar/promotions/details';
/** Состав акции по номенклатурам. READ. Неприменим к автоакциям — см. index.ts. */
export const WB_PROMO_NOMENCLATURES_PATH = '/api/v1/calendar/promotions/nomenclatures';

/**
 * ЗАКРЫТЫЙ список разрешённых путей. Любой запрос собирается только через него;
 * путь вне списка роняет сборку запроса ДО выхода в сеть.
 */
export const ALLOWED_PATHS: readonly string[] = [
  WB_PROMO_LIST_PATH,
  WB_PROMO_DETAILS_PATH,
  WB_PROMO_NOMENCLATURES_PATH,
];

/** Горизонт календаря назад от момента наблюдения, суток. */
export const WB_PROMO_WINDOW_BACK_DAYS = 7;
/** Горизонт календаря вперёд, суток. WB публикует примерно на месяц вперёд. */
export const WB_PROMO_WINDOW_FORWARD_DAYS = 90;

/** Максимум акций на страницу по контракту WB (limit 1..1000). */
export const WB_PROMO_PAGE_LIMIT = 1000;
/** Предохранитель от бесконечной пагинации при аномальном ответе. */
export const WB_PROMO_MAX_PAGES = 20;
/** Максимум promotionIDs в одном запросе /details по контракту WB. */
export const WB_PROMO_DETAILS_BATCH = 100;
/** Максимум номенклатур на страницу (limit 1..1000). */
export const WB_PROMO_NOMENCLATURE_LIMIT = 1000;
/** Предохранитель пагинации номенклатур. */
export const WB_PROMO_NOMENCLATURE_MAX_PAGES = 50;

/**
 * Пауза между запросами, мс. Лимит категории «Календарь акций» —
 * 10 запросов / 6 с, интервал 600 мс, всплеск 5, НА АККАУНТ ПРОДАВЦА и на все
 * методы категории сразу. Держим 700 мс: снимок укладывается в 2–3 запроса,
 * экономить здесь нечего, а 429 стоит дороже.
 */
export const WB_PROMO_REQUEST_SPACING_MS = 700;

/** Поля элемента data.promotions[] списка, известные контракту. */
export const KNOWN_LIST_FIELDS: readonly string[] = [
  'id',
  'name',
  'startDateTime',
  'endDateTime',
  'type',
];

/** Поля элемента data.promotions[] деталей, известные контракту. */
export const KNOWN_DETAILS_FIELDS: readonly string[] = [
  'id',
  'name',
  'description',
  'advantages',
  'startDateTime',
  'endDateTime',
  'inPromoActionLeftovers',
  'inPromoActionTotal',
  'notInPromoActionLeftovers',
  'notInPromoActionTotal',
  'participationPercentage',
  'type',
  'exceptionProductsCount',
  'ranging',
];

/** Поля элемента ranging[], известные контракту. */
export const KNOWN_RANGING_FIELDS: readonly string[] = [
  'condition',
  'participationRate',
  'boost',
];

/** Поля элемента nomenclatures[], известные контракту. */
export const KNOWN_NOMENCLATURE_FIELDS: readonly string[] = [
  'id',
  'inAction',
  'price',
  'currencyCode',
  'planPrice',
  'discount',
  'planDiscount',
];

/**
 * Поля, без которых наблюдение бессмысленно. Их отсутствие — отказ прогона,
 * а НЕ подстановка значения по умолчанию.
 */
export const REQUIRED_LIST_FIELDS: readonly string[] = ['id', 'type'];

export type SchemaStatus = 'OK' | 'DRIFT_NEW_FIELDS' | 'DRIFT_MISSING_FIELDS';

/**
 * Почему у акции нет строк уровня SKU.
 *
 * SKIPPED_AUTO_PROMOTION — автоакция; метод /nomenclatures к ней неприменим по
 *   официальной документации WB. Запрос не делается вовсе. Это ИЗВЕСТНОЕ
 *   ОГРАНИЧЕНИЕ КОНТРАКТА, а не отказ пайплайна.
 * UNSUPPORTED_422     — запрос сделан (акция не автоакция) и площадка ответила 422.
 *   Тоже ограничение способности, а не ошибка: фиксируем и идём дальше.
 * FETCHED             — состав получен.
 * EMPTY               — валидный ответ с пустым массивом.
 * HTTP_ERROR          — иной сбой; роняет прогон.
 */
export type NomenclatureStatus =
  | 'SKIPPED_AUTO_PROMOTION'
  | 'UNSUPPORTED_422'
  | 'FETCHED'
  | 'EMPTY'
  | 'HTTP_ERROR';

/** Значение поля type, при котором состав по SKU недоступен. */
export const WB_AUTO_PROMOTION_TYPE = 'auto';
