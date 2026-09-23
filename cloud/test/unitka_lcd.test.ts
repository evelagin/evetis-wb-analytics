/**
 * AUTO-LCD: смежность, режимы, атомарный коммит и регрессия инцидента 23.09.2026.
 *
 * Главный инвариант, который здесь доказывается: готовность источников НЕ закрывает день.
 * День закрывает только успешно завершённый цикл записи. Именно этого не хватало 23.09,
 * когда LCD передвинули вручную, а запись не состоялась.
 */
import { describe, it, expect } from 'vitest';
import {
  parseLcdMode, contiguousCandidate, decideCandidate, decideCommit, firstFailedStage,
  coveredByDailyRuns, coveredByRunWindows, addDaysIso, LCD_STAGES,
  commitLcd, revertLcd, type LcdCell, type StageReport, type LcdStage,
} from '../src/loaders/unitka/lcd.js';

const ALL_PASS: StageReport = {
  SOURCE_READINESS: 'PASS', STRUCTURE_PREPARE: 'PASS', UNITKA_WRITE: 'PASS',
  POST_WRITE_READBACK: 'PASS', INTEGRITY_CHECK: 'PASS',
};
const withFail = (s: LcdStage): StageReport => ({ ...ALL_PASS, [s]: 'FAIL' });
/** Покрыты все дни из списка. */
const coveredSet = (days: readonly string[]) => (d: string): boolean => days.includes(d);

describe('режим LCD читается явно, без эвристик', () => {
  it('пусто = AUTO (production-умолчание); AUTO/MANUAL в любом регистре', () => {
    expect(parseLcdMode('')).toEqual({ mode: 'AUTO' });
    expect(parseLcdMode(null)).toEqual({ mode: 'AUTO' });
    expect(parseLcdMode(' auto ')).toEqual({ mode: 'AUTO' });
    expect(parseLcdMode('Manual')).toEqual({ mode: 'MANUAL' });
  });

  it('непонятное слово — отказ, а НЕ молчаливый откат к AUTO', () => {
    for (const bad of ['ON', 'true', '1', 'АВТО', 'MANUAL!']) {
      expect(parseLcdMode(bad), bad).toMatchObject({ code: 'LCD_MODE_INVALID' });
    }
  });
});

describe('смежность: дырку не перепрыгиваем (§6)', () => {
  const d1 = '2026-09-22';

  it('D-2 READY, D-1 MISSING, D READY ⇒ кандидат ≤ D-2 — обязательный негативный тест', () => {
    const r = contiguousCandidate({
      committed: '2026-09-19', d1Msk: d1,
      covered: coveredSet(['2026-09-20', '2026-09-22']),   // 21.09 — дырка
    });
    expect(r.candidate).toBe('2026-09-20');
    expect(r.gapAt).toBe('2026-09-21');
    expect(r.advancedDays).toBe(1);
    expect(r.blockedDays).toBe(2);           // 21 и 22 остались за дыркой
  });

  it('сплошная готовность — доходим до D-1 и не дальше', () => {
    const r = contiguousCandidate({
      committed: '2026-09-19', d1Msk: d1,
      covered: coveredSet(['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23']),
    });
    expect(r.candidate).toBe('2026-09-22');
    expect(r.gapAt).toBeNull();
  });

  it('следующий день не покрыт — стоим на месте, не откатываемся', () => {
    const r = contiguousCandidate({ committed: '2026-09-22', d1Msk: d1, covered: () => false });
    expect(r.candidate).toBe('2026-09-22');
    expect(r.advancedDays).toBe(0);
  });

  it('МОНОТОННОСТЬ ПО ПОСТРОЕНИЮ: даже если не покрыто вообще ничего, LCD не уезжает назад', () => {
    for (const committed of ['2026-04-17', '2026-09-01', '2026-09-22']) {
      const r = contiguousCandidate({ committed, d1Msk: d1, covered: () => false });
      expect(r.candidate).toBe(committed);
      expect(r.advancedDays).toBe(0);
    }
  });

  it('история журнала короче данных: старые сутки без прогонов НЕ тянут LCD назад', () => {
    // журнал знает только 20–22.09, а LCD уже на 19.09 — обход идёт вперёд и это не мешает
    const r = contiguousCandidate({
      committed: '2026-09-19', d1Msk: d1, covered: coveredSet(['2026-09-20', '2026-09-21', '2026-09-22']),
    });
    expect(r.candidate).toBe('2026-09-22');
  });
});

describe('покрытие суток — это ПРОГОН, а не бизнес-строки', () => {
  it('WB: день закрыт, когда завершены ВСЕ гейтящие загрузчики', () => {
    const covered = coveredByDailyRuns(
      [{ loader: 'funnel', period: '2026-09-20' }, { loader: 'mart', period: '2026-09-20' },
       { loader: 'funnel', period: '2026-09-21' }],                    // у 21.09 нет mart
      ['funnel', 'mart']);
    expect(covered('2026-09-20')).toBe(true);
    expect(covered('2026-09-21')).toBe(false);
    expect(covered('2026-09-19')).toBe(false);
  });

  it('сутки без продаж — законный ноль: прогон есть, значит день покрыт', () => {
    const covered = coveredByDailyRuns(
      [{ loader: 'funnel', period: '2026-09-20' }, { loader: 'mart', period: '2026-09-20' }], ['funnel', 'mart']);
    expect(covered('2026-09-20'), 'отсутствие заказов не делает день дыркой').toBe(true);
  });

  it('Ozon: покрытие — окно source_from..source_to успешного прогона', () => {
    const covered = coveredByRunWindows(
      [{ entity: 'finance_accrual', from: '2026-08-24', to: '2026-09-22' },
       { entity: 'fbo_postings', from: '2026-08-24', to: '2026-09-22' },
       { entity: 'ads_sku_daily', from: '2026-09-15', to: '2026-09-22' },
       { entity: 'prices', from: '2026-09-22', to: '2026-09-22' }],
      ['finance_accrual', 'fbo_postings', 'ads_sku_daily', 'prices']);
    expect(covered('2026-09-22')).toBe(true);
    expect(covered('2026-09-21'), 'prices не покрывает 21.09').toBe(false);
  });
});

describe('MANUAL переопределяет ВЫБОР кандидата, но не барьеры записи (§8)', () => {
  const base = { platform: 'OZON' as const, committed: '2026-09-20', d1Msk: '2026-09-22', covered: () => true };

  it('AUTO не смотрит на MANUAL_LCD вовсе', () => {
    const d = decideCandidate({ ...base, mode: 'AUTO', manualLcd: '2026-01-01' });
    expect(d.candidate).toBe('2026-09-22');
    expect(d.code).toBeNull();
  });

  it('MANUAL берёт дату владельца даже там, где AUTO бы не пошёл', () => {
    const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: '2026-09-21', covered: () => false });
    expect(d.candidate).toBe('2026-09-21');
    expect(d.code).toBe('MANUAL_OVERRIDE_ACTIVE');
  });

  it('невалидная MANUAL_LCD отвергается — LCD не двигается', () => {
    for (const bad of ['', '21.09.2026', '2026-13-01', '2027-02-29', '2026-02-31', '2026-04-31', null, 46287]) {
      const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: bad });
      expect(d.code, String(bad)).toBe('MANUAL_LCD_INVALID');
      expect(d.candidate).toBe(base.committed);
      expect(d.willAdvance).toBe(false);
    }
  });

  it('MANUAL позже D-1 МСК отвергается: сегодняшний день закрытым не бывает', () => {
    const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: '2026-09-23' });
    expect(d.code).toBe('MANUAL_LCD_INVALID');
    expect(d.candidate).toBe(base.committed);
  });

  it('откат назад в MANUAL ОТВЕРГАЕТСЯ: он стёр бы опубликованные сутки', () => {
    // Раньше откат разрешался «громко». Пересмотрено (Gate 10 §4): у Ozon сутки после LCD
    // пишутся пустыми — откат стёр бы данные; у WB упал бы FUTURE_LEAKAGE. Историю чинит
    // окно перезаписи, а не откат LCD.
    const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: '2026-09-18' });
    expect(d.code).toBe('MANUAL_LCD_REGRESSION');
    expect(d.candidate).toBe(base.committed);
    expect(d.willAdvance).toBe(false);
  });

  it('MANUAL позже готовности источников отвергается: override не создаёт данные', () => {
    const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: '2026-09-22', sourceCeiling: '2026-09-21' });
    expect(d.code).toBe('MANUAL_LCD_INVALID');
    expect(d.candidate).toBe(base.committed);
  });

  it('MANUAL НЕ отменяет барьеры: провал целостности — LCD на месте', () => {
    const d = decideCandidate({ ...base, mode: 'MANUAL', manualLcd: '2026-09-22' });
    const c = decideCommit(base.committed, d.candidate, d.willAdvance, withFail('INTEGRITY_CHECK'));
    expect(c.commit).toBe(false);
    expect(c.lcdAfter).toBe(base.committed);
    expect(c.code).toBe('INTEGRITY_FAILED');
  });

  it('возврат в AUTO продолжает цикл от закоммиченного состояния', () => {
    const afterManual = '2026-09-21';
    const d = decideCandidate({ ...base, committed: afterManual, mode: 'AUTO', covered: () => true });
    expect(d.candidate).toBe('2026-09-22');
    expect(d.willAdvance).toBe(true);
  });
});

describe('атомарный коммит: провал ЛЮБОЙ стадии оставляет LCD на месте (§9)', () => {
  it.each(LCD_STAGES.map((s) => [s] as const))('провал %s ⇒ LCD_AFTER = LCD_BEFORE', (stage) => {
    const c = decideCommit('2026-09-21', '2026-09-22', true, withFail(stage));
    expect(c.commit).toBe(false);
    expect(c.lcdAfter).toBe('2026-09-21');
    expect(c.failedStage).toBe(stage);
  });

  it('все стадии пройдены ⇒ коммит ровно в кандидата', () => {
    const c = decideCommit('2026-09-21', '2026-09-22', true, ALL_PASS);
    expect(c).toMatchObject({ commit: true, lcdAfter: '2026-09-22', code: 'LCD_COMMITTED' });
  });

  it('двигаться некуда — это НЕ ошибка, а штатный LCD_NOT_ADVANCED', () => {
    const c = decideCommit('2026-09-22', '2026-09-22', false, ALL_PASS);
    expect(c).toMatchObject({ commit: false, lcdAfter: '2026-09-22', code: 'LCD_NOT_ADVANCED', failedStage: null });
  });

  it('SKIPPED провалом не считается: стадия могла быть не нужна', () => {
    const stages: StageReport = { ...ALL_PASS, STRUCTURE_PREPARE: 'SKIPPED' };
    expect(firstFailedStage(stages)).toBeNull();
    expect(decideCommit('2026-09-21', '2026-09-22', true, stages).commit).toBe(true);
  });

  it('повторный идентичный прогон не двигает LCD второй раз (§37)', () => {
    const first = decideCommit('2026-09-21', '2026-09-22', true, ALL_PASS);
    expect(first.lcdAfter).toBe('2026-09-22');
    // второй прогон стартует уже с закоммиченного состояния
    const d = decideCandidate({ platform: 'OZON', mode: 'AUTO', committed: first.lcdAfter,
      d1Msk: '2026-09-22', covered: () => true });
    const second = decideCommit(first.lcdAfter, d.candidate, d.willAdvance, ALL_PASS);
    expect(second.commit, 'LCD_DUPLICATE_ADVANCE').toBe(false);
    expect(second.lcdAfter).toBe('2026-09-22');
  });
});

describe('РЕГРЕССИЯ ИНЦИДЕНТА 23.09.2026 (§30)', () => {
  // 22.09 источники Ozon были готовы; расписание сработало; Job не был вызван (нет run.invoker);
  // записи не произошло; LCD при этом передвинули вручную — день «закрыт», но строка пустая.
  const committed = '2026-09-21';
  const d1 = '2026-09-22';
  const sourcesReady = () => true;

  it('источники ГОТОВЫ, но запись не состоялась ⇒ LCD НЕ двигается', () => {
    const d = decideCandidate({ platform: 'OZON', mode: 'AUTO', committed, d1Msk: d1, covered: sourcesReady });
    expect(d.candidate, 'кандидат вычислен: источники действительно готовы').toBe('2026-09-22');
    expect(d.willAdvance).toBe(true);

    const c = decideCommit(committed, d.candidate, d.willAdvance, withFail('UNITKA_WRITE'));
    expect(c.commit).toBe(false);
    expect(c.lcdAfter, 'LCD_ADVANCE=NO').toBe(committed);
    expect(c.code).toBe('WRITE_FAILED');
  });

  it('готовность источников САМА ПО СЕБЕ не закрывает день', () => {
    const d = decideCandidate({ platform: 'OZON', mode: 'AUTO', committed, d1Msk: d1, covered: sourcesReady });
    const onlyReadiness: StageReport = {
      SOURCE_READINESS: 'PASS', STRUCTURE_PREPARE: 'PASS', UNITKA_WRITE: 'FAIL',
      POST_WRITE_READBACK: 'SKIPPED', INTEGRITY_CHECK: 'SKIPPED',
    };
    expect(decideCommit(committed, d.candidate, d.willAdvance, onlyReadiness).lcdAfter).toBe(committed);
  });

  it('readback разошёлся ⇒ LCD НЕ двигается, хотя запись «прошла»', () => {
    const c = decideCommit(committed, '2026-09-22', true, withFail('POST_WRITE_READBACK'));
    expect(c.lcdAfter).toBe(committed);
    expect(c.code).toBe('READBACK_FAILED');
  });

  it('тот же кандидат, но цикл успешен ⇒ LCD_ADVANCE=YES', () => {
    const d = decideCandidate({ platform: 'OZON', mode: 'AUTO', committed, d1Msk: d1, covered: sourcesReady });
    const c = decideCommit(committed, d.candidate, d.willAdvance, ALL_PASS);
    expect(c).toMatchObject({ commit: true, lcdAfter: '2026-09-22', code: 'LCD_COMMITTED' });
  });

  it('изоляция платформ: падение Ozon не трогает LCD WB', () => {
    const wbBefore = '2026-09-22';
    const wb = decideCommit(wbBefore, '2026-09-22', false, ALL_PASS);          // WB уже закрыт
    const oz = decideCommit('2026-09-21', '2026-09-22', true, withFail('UNITKA_WRITE'));
    expect(wb.lcdAfter).toBe('2026-09-22');
    expect(oz.lcdAfter).toBe('2026-09-21');
    expect(wb.lcdAfter).not.toBe(oz.lcdAfter);   // допустимое штатное состояние D и D-1
  });
});

describe('арифметика дат не течёт через границы месяца и года', () => {
  it.each([
    ['2026-09-30', 1, '2026-10-01'], ['2026-12-31', 1, '2027-01-01'],
    ['2028-02-28', 1, '2028-02-29'], ['2027-02-28', 1, '2027-03-01'],
    ['2026-03-01', -1, '2026-02-28'],
  ])('%s %+d = %s', (from, n, want) => expect(addDaysIso(from, n as number)).toBe(want));
});

describe('потолок канонической готовности (WB: V_UNITKA_LAST_CLOSED_DATE)', () => {
  const base = { platform: 'WB' as const, mode: 'AUTO' as const, committed: '2026-09-20', d1Msk: '2026-09-22' };

  it('смежность может только ПРИДЕРЖАТЬ LCD относительно прежнего контракта', () => {
    const d = decideCandidate({ ...base, covered: () => true, sourceCeiling: '2026-09-21' });
    expect(d.candidate).toBe('2026-09-21');
  });

  it('дырка в журнале прогонов придерживает, даже если канон говорит «готово»', () => {
    const d = decideCandidate({ ...base, covered: (x) => x !== '2026-09-22', sourceCeiling: '2026-09-22' });
    expect(d.candidate).toBe('2026-09-21');
    expect(d.gapAt).toBe('2026-09-22');
  });

  it('потолок ниже закоммиченного НЕ тянет LCD назад', () => {
    const d = decideCandidate({ ...base, covered: () => true, sourceCeiling: '2026-09-15' });
    expect(d.candidate).toBe('2026-09-20');
    expect(d.code).toBe('LCD_NOT_ADVANCED');
  });
});

/** Ячейка LCD в памяти, с инъекцией сбоев — модель поведения API, а не его подмена. */
class MemCell implements LcdCell {
  writes: string[] = [];
  constructor(public value: string | null, private readonly o: {
    failWrite?: 'throw' | 'throw-after-apply' | 'silent-noop';
    onBeforeWrite?: () => void;
  } = {}) {}
  async read(): Promise<string | null> { return this.value; }
  async write(iso: string): Promise<void> {
    this.o.onBeforeWrite?.();
    this.writes.push(iso);
    if (this.o.failWrite === 'throw') throw new Error('Sheets API 503');
    if (this.o.failWrite === 'silent-noop') return;
    this.value = iso;
    if (this.o.failWrite === 'throw-after-apply') throw new Error('timeout после применения');
  }
}

describe('протокол коммита: compare-before-commit', () => {
  it('успех: ровно одна запись, перечитывание подтверждает кандидата', async () => {
    const c = new MemCell('2026-09-21');
    const r = await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_COMMITTED');
    expect(c.writes).toEqual(['2026-09-22']);
    expect(c.value).toBe('2026-09-22');
  });

  it('LCD_COMMIT_CONFLICT: книга изменилась после планирования — чужое значение НЕ перетирается', async () => {
    const c = new MemCell('2026-09-19');            // кто-то сдвинул, пока мы писали факты
    const r = await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_COMMIT_CONFLICT');
    expect(c.writes).toEqual([]);
    expect(c.value).toBe('2026-09-19');
  });

  it('LCD_WRITE_FAILED: запись упала — LCD остаётся прежним', async () => {
    const c = new MemCell('2026-09-21', { failWrite: 'throw' });
    const r = await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_WRITE_FAILED');
    expect(c.value).toBe('2026-09-21');
  });

  it('«ошибка» после фактического применения — исход решает ПЕРЕЧИТЫВАНИЕ: закоммичено', async () => {
    const c = new MemCell('2026-09-21', { failWrite: 'throw-after-apply' });
    const r = await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_COMMITTED');
    expect(r.message).toMatch(/перечитывание подтвердило/);
  });

  it('API ответил успехом, но значение не легло — LCD_WRITE_FAILED, а не COMMITTED', async () => {
    const c = new MemCell('2026-09-21', { failWrite: 'silent-noop' });
    const r = await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_WRITE_FAILED');
  });

  it('кандидат равен закоммиченному — ни одной записи', async () => {
    const c = new MemCell('2026-09-22');
    const r = await commitLcd(c, { expectedCommitted: '2026-09-22', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_NOT_ADVANCED');
    expect(c.writes).toEqual([]);
  });

  it('повтор после LCD_WRITE_FAILED коммитит РОВНО один раз', async () => {
    const c = new MemCell('2026-09-21', { failWrite: 'throw' });
    await commitLcd(c, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    const retry = new MemCell(c.value);            // следующий прогон: сбой прошёл
    const r = await commitLcd(retry, { expectedCommitted: '2026-09-21', candidate: '2026-09-22' });
    expect(r.code).toBe('LCD_COMMITTED');
    const again = await commitLcd(retry, { expectedCommitted: '2026-09-22', candidate: '2026-09-22' });
    expect(again.code, 'LCD_DUPLICATE_ADVANCE').toBe('LCD_NOT_ADVANCED');
    expect(retry.writes).toEqual(['2026-09-22']);
  });
});

describe('компенсирующий откат собственного коммита', () => {
  it('проверка после коммита не прошла — книга возвращается к состоянию до цикла', async () => {
    const c = new MemCell('2026-09-22');
    const r = await revertLcd(c, { committed: '2026-09-21', from: '2026-09-22' });
    expect(r.code).toBe('REVERTED');
    expect(c.value).toBe('2026-09-21');
  });

  it('если в книге уже НЕ наш кандидат — не откатываем чужое', async () => {
    const c = new MemCell('2026-09-25');
    const r = await revertLcd(c, { committed: '2026-09-21', from: '2026-09-22' });
    expect(r.code).toBe('REVERT_CONFLICT');
    expect(c.value).toBe('2026-09-25');
    expect(c.writes).toEqual([]);
  });
});
