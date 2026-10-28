import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

/**
 * Инцидент 28.10.2026: runWbStocksSnapshot взял tryLock(1000), проиграл ежечасному
 * инкременту продаж за общий ScriptLock и пропустил суточный снимок без повтора.
 *
 * Исполняемый код apps-script/ingestion/WbStocksSnapshot.gs гоняется в node:vm;
 * ядро снимка, BigQuery, триггеры и Script Properties — заглушки.
 */

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', '..', 'apps-script', 'ingestion', 'WbStocksSnapshot.gs'), 'utf8');

type Obj = Record<string, unknown>;

interface Harness {
  ctx: Obj;
  logs: string[];
  triggers: { fn: string; afterMs: number | null }[];
  props: Map<string, string>;
  lockWaits: number[];
  coreCalls: number;
}

function load(opts: { lockOk: boolean; completeExists?: boolean | Error; coreStatus?: string; coreThrows?: boolean; today?: string }): Harness {
  const h: Harness = { ctx: {}, logs: [], triggers: [], props: new Map(), lockWaits: [], coreCalls: 0 };
  const today = opts.today ?? '2026-10-28';
  const sandbox: Obj = {
    console: { log: (s: string) => h.logs.push(String(s)), error: (s: string) => h.logs.push(String(s)) },
    Utilities: { formatDate: () => today },
    LockService: {
      getScriptLock: () => ({
        tryLock: (ms: number) => { h.lockWaits.push(ms); return opts.lockOk; },
        releaseLock: () => {},
      }),
    },
    SpreadsheetApp: { getActiveSpreadsheet: () => { throw new Error('лист журнала недоступен в тесте'); } },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k: string) => h.props.get(k) ?? null,
        setProperty: (k: string, v: string) => { h.props.set(k, v); },
      }),
    },
    ScriptApp: {
      getProjectTriggers: () => h.triggers.map((t) => ({ getHandlerFunction: () => t.fn, _t: t })),
      deleteTrigger: (tr: { _t: { fn: string } }) => { h.triggers.splice(h.triggers.indexOf(tr._t as never), 1); },
      newTrigger: (fn: string) => {
        const t = { fn, afterMs: null as number | null };
        const b = {
          timeBased: () => b,
          after: (ms: number) => { t.afterMs = ms; return b; },
          everyDays: () => b, atHour: () => b, nearMinute: () => b,
          create: () => { h.triggers.push(t); return t; },
        };
        return b;
      },
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  // Заглушки поверх объявлений файла: вызовы внутри runWbStocksSnapshot идут через глобалы контекста.
  sandbox.wbStocksBqCompleteSnapshotExists_ = () => {
    if (opts.completeExists instanceof Error) throw opts.completeExists;
    return opts.completeExists ?? false;
  };
  sandbox.wbStocksSnapshotCore_ = (r: Obj) => {
    h.coreCalls++;
    if (opts.coreThrows) throw new Error('T6: HTTP 500');
    r.status = opts.coreStatus ?? 'OK';
    r.snapshot_id = 'STOCK_SNAP_test';
  };
  sandbox.wbStocksBqManifestFinalize_ = () => {};
  h.ctx = sandbox;
  return h;
}

const run = (h: Harness) => vm.runInContext('runWbStocksSnapshot()', h.ctx) as Obj;
const state = (h: Harness) => JSON.parse(h.props.get('WB_STOCKS_CATCHUP_STATE') ?? '{}') as { date?: string; attempts?: number };

describe('WbStocksSnapshot: защита суточного снимка от общего ScriptLock', () => {
  it('ждёт замок дольше, чем одну секунду (часовые загрузчики держат его секунды)', () => {
    const h = load({ lockOk: true });
    run(h);
    expect(h.lockWaits).toHaveLength(1);
    expect(h.lockWaits[0]).toBeGreaterThanOrEqual(60_000);
    expect(h.lockWaits[0]).toBeLessThanOrEqual(240_000); // оставить время на сам снимок в лимите 6 мин
  });

  it('SKIPPED_LOCKED больше не теряет сутки: ставится одноразовый догон', () => {
    const h = load({ lockOk: false });
    const r = run(h);
    expect(r.status).toBe('SKIPPED_LOCKED');
    expect(h.coreCalls).toBe(0);
    expect(h.triggers).toEqual([{ fn: 'wbStocksCatchupTick', afterMs: 15 * 60 * 1000 }]);
    expect(state(h)).toEqual({ date: '2026-10-28', attempts: 1 });
    expect(h.logs.some((l) => l.startsWith('STOCKS: CATCHUP_SCHEDULED | 2026-10-28 | попытка 1/4'))).toBe(true);
  });

  it('ERROR ядра тоже ставит догон, а исключение не теряет manifest-финализацию', () => {
    const h = load({ lockOk: true, coreThrows: true });
    const r = run(h);
    expect(r.status).toBe('ERROR');
    expect(String(r.error_message)).toContain('T6: HTTP 500');
    expect(h.triggers.map((t) => t.fn)).toEqual(['wbStocksCatchupTick']);
  });

  it('за сутки с COMPLETE в manifest второй снимок не пишется и догон не ставится', () => {
    const h = load({ lockOk: true, completeExists: true });
    const r = run(h);
    expect(r.status).toBe('SKIPPED_ALREADY_COMPLETE');
    expect(h.coreCalls).toBe(0);
    expect(h.triggers).toHaveLength(0);
  });

  it('если проверку manifest выполнить нельзя — не пишем вслепую: ERROR и догон', () => {
    const h = load({ lockOk: true, completeExists: new Error('BigQuery недоступен') });
    const r = run(h);
    expect(r.status).toBe('ERROR');
    expect(h.coreCalls).toBe(0);
    expect(h.triggers.map((t) => t.fn)).toEqual(['wbStocksCatchupTick']);
  });

  it('догон ограничен: после 4 попыток за сутки — только CATCHUP_EXHAUSTED', () => {
    const h = load({ lockOk: false });
    for (let i = 0; i < 6; i++) run(h);
    expect(state(h)).toEqual({ date: '2026-10-28', attempts: 4 });
    expect(h.triggers).toHaveLength(1); // старый триггер догона заменяется, а не копится
    expect(h.logs.some((l) => l.startsWith('STOCKS: CATCHUP_EXHAUSTED | 2026-10-28'))).toBe(true);
  });

  it('счётчик попыток живёт в пределах суток МСК', () => {
    const h = load({ lockOk: false });
    h.props.set('WB_STOCKS_CATCHUP_STATE', JSON.stringify({ date: '2026-10-27', attempts: 4 }));
    run(h);
    expect(state(h)).toEqual({ date: '2026-10-28', attempts: 1 });
  });

  it('обработчик догона снимает свой триггер и повторяет снимок; дневной триггер не трогает', () => {
    const h = load({ lockOk: true });
    h.triggers.push({ fn: 'runWbStocksSnapshot', afterMs: null }, { fn: 'wbStocksCatchupTick', afterMs: 900000 });
    const r = vm.runInContext('wbStocksCatchupTick()', h.ctx) as Obj;
    expect(r.status).toBe('OK');
    expect(h.coreCalls).toBe(1);
    expect(h.triggers.map((t) => t.fn)).toEqual(['runWbStocksSnapshot']);
  });

  it('успешный снимок не ставит догон', () => {
    const h = load({ lockOk: true });
    const r = run(h);
    expect(r.status).toBe('OK');
    expect(h.triggers).toHaveLength(0);
  });
});
