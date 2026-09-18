/**
 * UNITKA ENGINE v1 — QA-гейт (§7 брифа). Чистый модуль.
 *
 * Оценивает снимок листа против плана/ожидания:
 *   BQ → SHEETS MISMATCH, FORMULA ERRORS, FUTURE LEAKAGE, SUMMARY RECONCILIATION,
 *   LAST_CLOSED_DATE consistent. (BLOCKS, DUP, INVARIANT проверяются в plan.ts до записи.)
 *
 * В PROD вызывается ПОСЛЕ записи (reconciliation). В SHADOW — на текущем листе:
 * mismatch тогда = «что изменил бы Engine», остальные проверки — здоровье Master как есть.
 */
import {
  OFFSET, FUTURE_ALLOWED_OFFSETS, SUMMARY_TO_OFFSET, SUMMARY, BOOK_ANCHORS,
  type CellValue, colA1, isEmpty, asNumber, factEqual, rateEqual, isFormulaError, isoToSerial,
} from './model.js';
import { BLOCK_WIDTH, dayRowOf, isReservedSlot, slotOfColumn } from './calendar.js';
import { cellAt, currentValue, formulaAt, isFormula, formatContract, type Plan, type Snapshot } from './plan.js';

export interface QaCheck {
  name: string;
  pass: boolean;
  count: number;
  sample: string[];
}

export interface QaResult {
  pass: boolean;
  checks: QaCheck[];
}

function check(name: string, bad: string[]): QaCheck {
  return { name, pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 10) };
}

export interface EvaluateOpts {
  /**
   * SHADOW: книга ещё не знает новый LCD, поэтому сводка сверяется только по дням,
   * закрытым В КНИГЕ (зеркало WB736); mismatch и LCD — это «что изменил бы Engine», не дефект.
   */
  shadow?: boolean;
}

export function evaluate(snap: Snapshot, plan: Plan, opts: EvaluateOpts = {}): QaResult {
  const checks: QaCheck[] = [];
  const L = plan.layout;
  const dayRow = (i: number): number => dayRowOf(L, i);
  let closedDays = plan.closedDays;
  if (opts.shadow) {
    const mirror = asNumber(snap.mirrorLcd);
    const bookClosed = Number.isFinite(mirror) ? mirror - isoToSerial(plan.monthStart) + 1 : 0;
    closedDays = Math.max(0, Math.min(plan.closedDays, bookClosed));
  }

  // BQ → SHEETS MISMATCH = 0 — весь контракт expected, не только записанные ячейки.
  const mism: string[] = [];
  for (const e of plan.expected) {
    const got: CellValue = currentValue(snap, e);
    const ok = e.kind === 'fact' ? factEqual(got, e.want) : rateEqual(got, e.want as number);
    if (!ok) mism.push(`${e.namedRange ?? colA1(e.col) + e.row} ${e.key ?? ''} лист[${String(got)}] BQ[${e.want === null ? '' : e.want}]`);
  }
  checks.push(check('BQ_SHEETS_MISMATCH', mism));

  // FORMULA ERRORS = 0 — строки секции (заголовок..MTD), колонки сводки и всех слотов до последнего
  // блока; зарезервированный слот (VQ..WN: унаследованные расчёты и якоря книги) секции не принадлежит.
  const errs: string[] = [];
  for (let r = L.topRow; r <= L.mtdRow; r++) {
    for (let c = 1; c <= L.lastBlockColumn; c++) {
      const slot = slotOfColumn(c);
      if (slot !== null && isReservedSlot(slot)) continue;
      const v = cellAt(snap, r, c);
      if (isFormulaError(v)) errs.push(`${colA1(c)}${r} ${String(v)}`);
    }
  }
  checks.push(check('FORMULA_ERRORS', errs));

  // FUTURE LEAKAGE = 0 — за датами > LCD все не-разрешённые смещения пусты (правило s8qa).
  // Исключение по решению владельца (E2, KEEP): формульная проекция ОСТАТКА в будущих днях —
  // visual planning layer; считается отдельно (STOCK_PROJECTION_FUTURE, informational).
  // Фактическое хранение/любой другой факт в будущем дне — утечка, формула это или нет.
  const leaks: string[] = [];
  const projection: string[] = [];
  for (const b of plan.blocks) {
    for (let i = plan.closedDays; i < L.daysInMonth; i++) {
      for (let o = 0; o < BLOCK_WIDTH; o++) {
        if (FUTURE_ALLOWED_OFFSETS.has(o) || o === OFFSET.bloggers) continue;
        const v = cellAt(snap, dayRow(i), b.start + o);
        if (isEmpty(v)) continue;
        if (o === OFFSET.stock && isFormula(formulaAt(snap, dayRow(i), b.start + o))) projection.push(`${colA1(b.start + o)}${dayRow(i)}=${String(v)}`);
        else leaks.push(`${colA1(b.start + o)}${dayRow(i)}=${String(v)}`);
      }
    }
  }
  checks.push(check('FUTURE_LEAKAGE', leaks));
  checks.push({ name: 'STOCK_PROJECTION_FUTURE', pass: true, count: projection.length, sample: projection.slice(0, 10) });

  // SUMMARY RECONCILIATION — закрытые дни: колонка сводки = Σ всех блоков секции (|Δ| ≤ 0.01).
  const rec: string[] = [];
  for (let i = 0; i < closedDays; i++) {
    for (const [sumCol, off, name] of SUMMARY_TO_OFFSET) {
      let total = 0;
      for (const b of plan.blocks) {
        const v = asNumber(cellAt(snap, dayRow(i), b.start + off));
        if (Number.isFinite(v)) total += v;
      }
      const s = asNumber(cellAt(snap, dayRow(i), sumCol));
      const sv = Number.isFinite(s) ? s : 0;
      if (Math.abs(sv - total) > 0.01) rec.push(`${name} ${colA1(sumCol)}${dayRow(i)} сводка[${sv}] Σблоков[${Math.round(total * 100) / 100}]`);
    }
  }
  // MTD: I{mtd} = Σ дневных I по закрытым дням (как в приёмке Stage 8.2; сентябрь — I767).
  {
    let sum = 0;
    for (let i = 0; i < closedDays; i++) {
      const v = asNumber(cellAt(snap, dayRow(i), SUMMARY.profit));
      if (Number.isFinite(v)) sum += v;
    }
    const mtd = asNumber(cellAt(snap, L.mtdRow, SUMMARY.profit));
    if (!Number.isFinite(mtd) || Math.abs(mtd - sum) > 0.01) rec.push(`MTD I${L.mtdRow}[${String(cellAt(snap, L.mtdRow, SUMMARY.profit))}] Σдней[${Math.round(sum * 100) / 100}]`);
  }
  checks.push(check('SUMMARY_RECONCILIATION', rec));

  // CLOSED_FORMAT_CONTRACT — закрытые дни в колонках Engine отформатированы как эталон (первый день секции).
  {
    const fc = formatContract(snap, plan.blocks, closedDays, plan.monthStart, plan.expected);
    checks.push(check('CLOSED_FORMAT_CONTRACT', fc.cells.map((c) => `${colA1(c.col)}${c.row} ${c.key} [${c.before.fg ? 'fg' : ''}${c.before.bg ? ' bg' : ''}${c.before.numberFormat ? ' nf' : ''}]`)));
  }

  // LAST_CLOSED_DATE consistent: имя = зеркало = вычисленное.
  const want = isoToSerial(plan.lcd);
  const lcdBad: string[] = [];
  if (asNumber(snap.namedLcd) !== want) lcdBad.push(`LAST_CLOSED_DATE=${String(snap.namedLcd)} ≠ ${want}`);
  if (asNumber(snap.mirrorLcd) !== want) lcdBad.push(`${colA1(BOOK_ANCHORS.COL)}${BOOK_ANCHORS.LCD_MIRROR_ROW}=${String(snap.mirrorLcd)} ≠ ${want}`);
  checks.push(check('LCD_CONSISTENT', lcdBad));

  // Дубли и инвариант — уже гарантированы планом; фиксируем как PASS для полного отчёта.
  // BLOCKS: число блоков — из секции (не константа 24); пустая секция и дубли уже отсеяны preflight.
  checks.push({ name: 'BLOCKS', pass: plan.blocks.length > 0 && new Set(plan.blocks.map((b) => b.nmId)).size === plan.blocks.length, count: plan.blocks.length, sample: [] });
  checks.push({ name: 'DIRECT_LOGISTICS_INVARIANT', pass: plan.invariant.pass, count: plan.invariant.shipments, sample: [`${plan.invariant.shipments} = ${plan.invariant.sales} + ${plan.invariant.refusals}`] });
  checks.push({ name: 'NO_DUPLICATE_DATE_NMID', pass: true, count: 0, sample: [] });

  return { pass: checks.every((c) => c.pass), checks };
}

/** Код ошибки для журнала/алерта по первой проваленной проверке. */
export function failureCode(qa: QaResult): string {
  const first = qa.checks.find((c) => !c.pass);
  if (!first) return 'QA_PASS';
  switch (first.name) {
    case 'BQ_SHEETS_MISMATCH': return 'BQ_MISMATCH';
    case 'FORMULA_ERRORS': return 'FORMULA_ERROR';
    case 'FUTURE_LEAKAGE': return 'FUTURE_LEAKAGE';
    case 'SUMMARY_RECONCILIATION': return 'SUMMARY_MISMATCH';
    case 'LCD_CONSISTENT': return 'LCD_INCONSISTENT';
    case 'CLOSED_FORMAT_CONTRACT': return 'FORMAT_CONTRACT';
    default: return first.name;
  }
}

/** Компактная строка для журнала qa_json. */
export function qaJson(qa: QaResult, extra: Record<string, unknown> = {}): string {
  return JSON.stringify({ pass: qa.pass, checks: qa.checks, ...extra });
}
