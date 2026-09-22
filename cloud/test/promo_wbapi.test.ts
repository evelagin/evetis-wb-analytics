import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  buildUrl,
  fetchPromotionDetails,
  fetchPromotionNomenclatures,
  fetchPromotions,
} from '../src/loaders/promo/wbApi.js';
import { WB_PROMO_HOST, WB_PROMO_LIST_PATH } from '../src/loaders/promo/constants.js';

const OPTS = { timeoutMs: 1000, maxRetries: 0, spacingMs: 0 };
const TOKEN = 'test-token-not-a-secret';

function mockFetch(responses: Array<{ status: number; body: string }>): void {
  let i = 0;
  vi.stubGlobal('fetch', async () => {
    const r = responses[Math.min(i++, responses.length - 1)]!;
    return { ok: r.status >= 200 && r.status < 300, status: r.status, text: async () => r.body } as Response;
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('buildUrl — закрытый список путей', () => {
  it('разрешённый путь собирается', () => {
    expect(buildUrl(WB_PROMO_HOST, WB_PROMO_LIST_PATH, 'a=1')).toBe(`${WB_PROMO_HOST}${WB_PROMO_LIST_PATH}?a=1`);
  });

  it('мутирующий путь роняет сборку ДО выхода в сеть', () => {
    expect(() => buildUrl(WB_PROMO_HOST, '/api/v1/calendar/promotions/' + 'upload', '')).toThrowError(
      /не входит в разрешённый список/,
    );
  });

  it('любой посторонний путь отвергается', () => {
    expect(() => buildUrl(WB_PROMO_HOST, '/api/v2/' + 'upload/task', '')).toThrow();
    expect(() => buildUrl(WB_PROMO_HOST, '/whatever', '')).toThrow();
  });
});

describe('fetchPromotions', () => {
  it('разбирает живой ответ и возвращает акции', async () => {
    mockFetch([
      {
        status: 200,
        body: JSON.stringify({
          data: { promotions: [{ id: 2940, name: 'X', type: 'auto', startDateTime: 'a', endDateTime: 'b' }] },
        }),
      },
    ]);
    const r = await fetchPromotions(WB_PROMO_HOST, TOKEN, '2026-09-01T00:00:00Z', '2026-12-01T00:00:00Z', OPTS);
    expect(r.promotions).toHaveLength(1);
    expect(r.httpStatus).toBe(200);
    expect(r.pages).toBe(1);
  });

  it('пустой валидный ответ — не ошибка: акции целиком дело площадки', async () => {
    mockFetch([{ status: 200, body: JSON.stringify({ data: { promotions: [] } }) }]);
    const r = await fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS);
    expect(r.promotions).toHaveLength(0);
  });

  it('401/403 классифицируется как отказ авторизации', async () => {
    mockFetch([{ status: 401, body: 'unauthorized' }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_AUTH',
    });
  });

  it('400 классифицируется как некорректный запрос — это наш дефект, не площадки', async () => {
    mockFetch([{ status: 400, body: '{"errorText":"Invalid query params"}' }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_BAD_REQUEST',
    });
  });

  it('429 после исчерпания ретраев — отдельный класс ошибки', async () => {
    mockFetch([{ status: 429, body: 'too many' }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_RATE_LIMIT',
    });
  });

  it('5xx — операционный сбой источника', async () => {
    mockFetch([{ status: 503, body: 'oops' }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_HTTP',
    });
  });

  it('невалидный JSON — отдельный класс, а не «пустой ответ»', async () => {
    mockFetch([{ status: 200, body: '<html>не json</html>' }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_BAD_JSON',
    });
  });

  it('пропажа data.promotions[] — дрейф контракта, а не пустой календарь', async () => {
    mockFetch([{ status: 200, body: JSON.stringify({ data: {} }) }]);
    await expect(fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_SHAPE',
    });
  });

  it('токен не попадает в текст ошибки', async () => {
    mockFetch([{ status: 500, body: 'boom' }]);
    try {
      await fetchPromotions(WB_PROMO_HOST, TOKEN, 'a', 'b', OPTS);
      expect.unreachable();
    } catch (e) {
      expect(String((e as Error).message)).not.toContain(TOKEN);
    }
  });
});

describe('fetchPromotionDetails', () => {
  it('пустой список id не делает ни одного запроса', async () => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    const r = await fetchPromotionDetails(WB_PROMO_HOST, TOKEN, [], OPTS);
    expect(spy).not.toHaveBeenCalled();
    expect(r.details).toHaveLength(0);
  });

  it('пустой массив деталей — штатное состояние завершённых акций', async () => {
    mockFetch([{ status: 200, body: JSON.stringify({ data: { promotions: [] } }) }]);
    const r = await fetchPromotionDetails(WB_PROMO_HOST, TOKEN, [2728], OPTS);
    expect(r.details).toHaveLength(0);
    expect(r.batches).toBe(1);
  });
});

describe('fetchPromotionNomenclatures — известное ограничение контракта WB', () => {
  it('HTTP 422 возвращается как UNSUPPORTED_422 и НЕ роняет прогон', async () => {
    mockFetch([{ status: 422, body: '{"errorText":"Unprocessable entity"}' }]);
    const out = await fetchPromotionNomenclatures(WB_PROMO_HOST, TOKEN, 2940, true, OPTS);
    expect(out.kind).toBe('UNSUPPORTED_422');
  });

  it('валидный пустой ответ отличается от 422', async () => {
    mockFetch([{ status: 200, body: JSON.stringify({ data: { nomenclatures: [] } }) }]);
    const out = await fetchPromotionNomenclatures(WB_PROMO_HOST, TOKEN, 2728, true, OPTS);
    expect(out.kind).toBe('EMPTY');
  });

  it('полученный состав возвращается как FETCHED', async () => {
    mockFetch([
      {
        status: 200,
        body: JSON.stringify({ data: { nomenclatures: [{ id: 1, inAction: true, price: 100, planPrice: 80 }] } }),
      },
    ]);
    const out = await fetchPromotionNomenclatures(WB_PROMO_HOST, TOKEN, 2728, true, OPTS);
    expect(out.kind).toBe('FETCHED');
    if (out.kind === 'FETCHED') expect(out.items).toHaveLength(1);
  });

  it('401 на составе — по-прежнему отказ авторизации, а не ограничение способности', async () => {
    mockFetch([{ status: 403, body: 'forbidden' }]);
    await expect(fetchPromotionNomenclatures(WB_PROMO_HOST, TOKEN, 2728, true, OPTS)).rejects.toMatchObject({
      code: 'WB_PROMO_AUTH',
    });
  });
});
