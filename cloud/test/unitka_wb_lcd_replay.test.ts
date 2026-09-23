/**
 * РЕПЛЕЙ НА ИСТОРИИ PRODUCTION. Новый алгоритм кандидата WB прогоняется по РЕАЛЬНОМУ журналу
 * прогонов (wb_raw.LOADER_RUNS: funnel и mart, prod, COMPLETE) и сравнивается с LCD, который
 * Engine ФАКТИЧЕСКИ закоммитил каждым боевым прогоном (wb_ops.UNITKA_ENGINE_RUNS).
 * Фикстура снята 24.09.2026: `fixtures/wb_lcd_history_2026-09.json`.
 *
 * Зачем. Новый барьер смежности не должен придерживать WB там, где прежний контракт закрывал
 * день ВЕРНО. Именно этот реплей поймал ошибку первой версии: точный матч «прогон на день»
 * объявил бы дырку 02–04.09 (прогонов mart за эти дни нет — их пересобрал прогон 05.09).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { decideCandidate, coveredByLoaderRuns, coveredByDailyRuns, addDaysIso } from '../src/loaders/unitka/lcd.js';
import { WB_COVERAGE_RULES } from '../src/loaders/unitka/wb_lifecycle.js';
import { WB_FUNNEL_COVERAGE_DAYS } from '../src/loaders/unitka/bq.js';

interface Run { loader_name: string; logical_period: string; started_at: string }
interface Eng { started_at: string; lcd: string; error_code: string | null }
const H: { runs: Run[]; engine: Eng[] } = JSON.parse(readFileSync(
  join(fileURLToPath(new URL('.', import.meta.url)), 'fixtures', 'wb_lcd_history_2026-09.json'), 'utf8'));

/** Что знал журнал к моменту прогона Engine. */
const knownAt = (t: string) => H.runs.filter((r) => r.started_at < t).map((r) => ({ loader: r.loader_name, period: r.logical_period }));
const d1Of = (t: string) => {
  const msk = new Date(Date.parse(t) + 3 * 3600_000).toISOString().slice(0, 10);
  return addDaysIso(msk, -1);
};

describe('реплей: новый кандидат WB против фактически закоммиченного LCD', () => {
  const okRuns = H.engine.filter((e) => e.error_code === null);

  it('фикстура содержит реальную историю', () => {
    expect(H.runs.length).toBeGreaterThanOrEqual(50);
    expect(okRuns.length).toBeGreaterThanOrEqual(20);
  });

  it('на КАЖДОМ боевом прогоне новый алгоритм даёт ровно тот LCD, что был закоммичен', () => {
    const diffs: string[] = [];
    for (let i = 1; i < okRuns.length; i++) {
      const prev = okRuns[i - 1]!, run = okRuns[i]!;
      const d = decideCandidate({
        platform: 'WB', mode: 'AUTO', committed: prev.lcd, d1Msk: d1Of(run.started_at),
        covered: coveredByLoaderRuns(knownAt(run.started_at), WB_COVERAGE_RULES),
        sourceCeiling: run.lcd,               // канонический потолок = то, что Engine вычислил тогда
      });
      if (d.candidate !== run.lcd) diffs.push(`${run.started_at}: закоммичено ${run.lcd}, новый кандидат ${d.candidate} (${d.notes.join('; ')})`);
    }
    expect(diffs, diffs.join('\n')).toEqual([]);
  });

  it('первая версия (точный матч) на той же истории ДАЛА БЫ ложные дырки — поэтому она заменена', () => {
    // Прогонов mart за 02–04.09 нет; первая пересборка после сбоя — 06.09 в 16:14 UTC (логический 05.09).
    const afterRebuild = '2026-09-07T07:00:00Z';
    const exact = coveredByDailyRuns(knownAt(afterRebuild), ['mart']);
    expect(['2026-09-02', '2026-09-03', '2026-09-04'].every((d) => !exact(d)), 'точный матч: ложная дырка').toBe(true);
    const cumulative = coveredByLoaderRuns(knownAt(afterRebuild), { mart: { kind: 'CUMULATIVE' } });
    expect(['2026-09-02', '2026-09-03', '2026-09-04'].every((d) => cumulative(d)), 'пересборка покрыла').toBe(true);
  });

  it('…а ДО пересборки те же сутки честно не покрыты: барьер прав, данных ещё нет', () => {
    const beforeRebuild = '2026-09-06T07:00:00Z';        // пересборка 05.09 случится только в 16:14
    const cumulative = coveredByLoaderRuns(knownAt(beforeRebuild), { mart: { kind: 'CUMULATIVE' } });
    expect(cumulative('2026-09-02')).toBe(false);
  });
});

describe('семантика покрытия по загрузчикам', () => {
  const runs = [{ loader: 'mart', period: '2026-09-10' }, { loader: 'funnel', period: '2026-09-10' }];

  it('mart — полная пересборка: прогон за 10.09 покрывает и 01.09', () => {
    const c = coveredByLoaderRuns(runs, { mart: { kind: 'CUMULATIVE' } });
    expect(c('2026-09-01')).toBe(true);
    expect(c('2026-09-10')).toBe(true);
    expect(c('2026-09-11')).toBe(false);
  });

  it('funnel — окно 7 суток: прогон за 10.09 покрывает 04..10.09 и не глубже', () => {
    const c = coveredByLoaderRuns(runs, { funnel: { kind: 'WINDOW', days: 7 } });
    expect(c('2026-09-04')).toBe(true);
    expect(c('2026-09-10')).toBe(true);
    expect(c('2026-09-03')).toBe(false);
    expect(c('2026-09-11')).toBe(false);
  });

  it('день закрыт, только когда покрыт ОБОИМИ гейтящими загрузчиками', () => {
    const c = coveredByLoaderRuns([{ loader: 'mart', period: '2026-09-12' }], WB_COVERAGE_RULES);
    expect(c('2026-09-12'), 'воронки нет вовсе').toBe(false);
  });

  it('D-2 READY, D-1 MISSING, D READY ⇒ кандидат ≤ D-2 и на РЕАЛЬНЫХ правилах покрытия', () => {
    const r = [
      { loader: 'mart', period: '2026-09-20' }, { loader: 'funnel', period: '2026-09-20' },
      // 21.09: ни mart, ни funnel не отработали; 22.09 funnel есть, mart есть
      { loader: 'mart', period: '2026-09-22' }, { loader: 'funnel', period: '2026-09-22' },
    ];
    // funnel за 22.09 покрывает окно 16..22 — дырку 21.09 он ЗАКРЫВАЕТ (так и задумано окном);
    // поэтому дырку моделируем там, где окно её не спасает: mart отсутствует после 20.09
    const noMart = r.filter((x) => !(x.loader === 'mart' && x.period === '2026-09-22'));
    const d = decideCandidate({ platform: 'WB', mode: 'AUTO', committed: '2026-09-19', d1Msk: '2026-09-22',
      covered: coveredByLoaderRuns(noMart, WB_COVERAGE_RULES), sourceCeiling: '2026-09-22' });
    expect(d.candidate).toBe('2026-09-20');
    expect(d.gapAt).toBe('2026-09-21');
  });

  it('окно воронки — это FUNNEL_LOOKBACK_DAYS задания wb-funnel-prod (контракт с Terraform)', () => {
    const tf = readFileSync(join(fileURLToPath(new URL('.', import.meta.url)), '..', '..', 'infra', 'terraform', 'wb_funnel_loader.tf'), 'utf8');
    const m = /FUNNEL_LOOKBACK_DAYS\s*=\s*"(\d+)"/.exec(tf);
    expect(m, 'FUNNEL_LOOKBACK_DAYS в wb_funnel_loader.tf не найден').not.toBeNull();
    expect(Number(m![1])).toBe(WB_FUNNEL_COVERAGE_DAYS);
  });
});
