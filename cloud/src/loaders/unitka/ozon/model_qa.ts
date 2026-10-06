/** Независимая сверка исходных фактов/наблюдений с планом или перечитанным листом.
 * Не вызывает composeMonth, buildGrid или генератор формул.
 */
import type { OzonSectionInput, ValueWrite } from './monthplan.js';
import type { CellValue, OzonFactRow } from './month.js';
import { MANAGEMENT_TAX_RESERVE_RATE } from './contract.js';

export interface OzonModelIssue { code: string; cell: string; expected: unknown; actual: unknown }
export type OzonCellReader = (row: number, col: number) => CellValue | undefined;

/** Независимая сверка исходного posting evidence, без helper базы planner. */
function sourceOperationalBasis(f: OzonFactRow): { basis?: number; issues: OzonModelIssue[] } {
  const issues: OzonModelIssue[] = [], cell = `${f.d}|${f.offer_id}`;
  const qty = f.expected_realized_qty ?? Math.max(0, f.gross_qty - f.cancelled_qty);
  const issue = (code: string, expected: unknown, actual: unknown) => issues.push({ code, cell, expected, actual });
  if (f.operational_basis_version !== undefined && qty !== Math.max(0, f.gross_qty - f.cancelled_qty)) {
    issue('OPERATIONAL_EXPECTED_QTY_MISMATCH', Math.max(0, f.gross_qty - f.cancelled_qty), qty);
  }
  if (qty <= 0) return { issues };
  if (f.operational_basis_version === undefined) {
    const revenue = f.provisional_revenue_rub ?? f.revenue ?? 0;
    if (revenue > 0) {
      if ((f.buyout_revenue_unproven_qty ?? 0) > 0) {
        issue('OPERATIONAL_POPULATION_EVIDENCE_MISSING', 'mutually exclusive unit evidence', revenue);
        return { issues };
      }
      return { basis: revenue, issues };
    }
    const p = f.order_reference_price;
    return { basis: typeof p === 'number' && Number.isFinite(p) && p > 0
      && f.order_reference_qty === qty && f.order_reference_covered_qty === qty ? p * qty : undefined, issues };
  }
  if (f.operational_basis_version !== 1) {
    issue('OPERATIONAL_BASIS_VERSION', 1, f.operational_basis_version); return { issues };
  }
  let units: unknown;
  try { units = JSON.parse(f.operational_basis_units_json ?? 'null'); } catch { units = null; }
  if (!Array.isArray(units)) {
    issue('OPERATIONAL_POPULATION_EVIDENCE_MISSING', 'posting/SKU evidence array', units); return { issues };
  }
  let actualQty = 0, provisionalQty = 0, referenceCovered = 0, actualBasis = 0, referenceBasis = 0, conflictQty = 0;
  const seen = new Set<string>();
  // TO_JSON_STRING кодирует дробный BigQuery NUMERIC десятичной строкой.
  // Принимаем только явное числовое представление, не boolean/null/пустоту.
  const amount = (v: unknown, positive = false): number | undefined => {
    const n = typeof v === 'number' ? v : typeof v === 'string'
      && /^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(v) ? Number(v) : undefined;
    return n !== undefined && Number.isFinite(n) && (positive ? n > 0 : n >= 0) ? n : undefined;
  };
  for (const raw of units) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      issue('OPERATIONAL_UNIT_EVIDENCE_INVALID', 'posting evidence object', raw); continue;
    }
    const u = raw as Record<string, unknown>, q = u.quantity;
    const key = `${u.posting_number}|${u.marketplace_sku}`;
    if (typeof u.posting_number !== 'string' || !u.posting_number || typeof u.marketplace_sku !== 'string'
      || !u.marketplace_sku || typeof u.documented_buyout_present !== 'boolean'
      || !['delivered', 'delivering', 'awaiting_deliver', 'awaiting_packaging'].includes(String(u.status))
      || typeof q !== 'number' || !Number.isInteger(q) || q <= 0) {
      issue('OPERATIONAL_UNIT_EVIDENCE_INVALID', 'valid identity/status/positive quantity', key); continue;
    }
    if (seen.has(key)) issue('OPERATIONAL_POPULATION_OVERLAP', 'one unit population per posting/SKU', key);
    seen.add(key);
    const documented = u.status === 'delivered' && u.documented_buyout_present;
    // Финансы старше статуса: seller-base у недоставленной единицы — тоже факт (не выкуп по документу).
    const finance = !documented && u.finance_unit_rub !== null
      && (u.status === 'delivered' || !u.documented_buyout_present);
    if (finance && u.status !== 'delivered') conflictQty += q;
    const source = documented ? 'DOCUMENTED_BUYOUT' : finance ? 'ACTUAL_FINANCE' : 'REFERENCE';
    if (u.basis_source !== source) issue('OPERATIONAL_EVIDENCE_PRECEDENCE', source, u.basis_source);
    if (documented || finance) {
      actualQty += q;
      const value = documented ? u.documented_buyout_unit_rub : u.finance_unit_rub;
      const parsed = amount(value);
      if (parsed === undefined) issue('OPERATIONAL_ACTUAL_BASIS_INVALID', 'finite non-negative stronger evidence', value);
      else actualBasis += parsed * q;
    } else {
      provisionalQty += q;
      const parsed = amount(u.reference_unit_rub, true);
      if (parsed === undefined) issue('OPERATIONAL_REFERENCE_INCOMPLETE', 'positive reference for every remaining unit', key);
      else { referenceCovered += q; referenceBasis += parsed * q; }
    }
  }
  const match = (code: string, expected: number, got: unknown) => {
    const count = code.includes('QTY') || code.includes('COVERAGE');
    if (typeof got !== 'number' || !Number.isFinite(got) || (count && !Number.isInteger(got))
      || Math.abs(expected - got) > (count ? 0 : 1e-6)) issue(code, expected, got);
  };
  match('OPERATIONAL_EXPECTED_QTY_MISMATCH', qty, actualQty + provisionalQty);
  match('OPERATIONAL_COVERED_QTY_MISMATCH', qty, actualQty + referenceCovered);
  match('OPERATIONAL_EXPECTED_QTY_MISMATCH', qty, f.operational_expected_qty);
  match('OPERATIONAL_ACTUAL_QTY_MISMATCH', actualQty, f.operational_actual_qty);
  match('OPERATIONAL_PROVISIONAL_QTY_MISMATCH', provisionalQty, f.operational_provisional_qty);
  match('OPERATIONAL_REFERENCE_COVERAGE_MISMATCH', referenceCovered, f.operational_reference_covered_qty);
  match('OPERATIONAL_ACTUAL_BASIS_MISMATCH', actualBasis, f.operational_actual_basis_rub);
  match('OPERATIONAL_REFERENCE_BASIS_MISMATCH', referenceBasis, f.operational_reference_basis_rub);
  match('OPERATIONAL_ECONOMIC_BASIS_MISMATCH', actualBasis + referenceBasis, f.operational_basis_rub);
  // Независимая проверка конфликта цикла по первичным единицам (не по состоянию вью):
  // недоставленная единица с seller-base начислением обязана принести ФАКТИЧЕСКУЮ комиссию.
  // Ноль здесь — ровно сигнатура дефекта 20.08 / 930334396 (+655,81 ₽ к прибыли).
  if (conflictQty > 0) {
    match('LIFECYCLE_CONFLICT_QTY_MISMATCH', conflictQty, f.lifecycle_conflict_qty);
    const cc = f.lifecycle_conflict_commission_rub;
    if (typeof cc !== 'number' || !Number.isFinite(cc) || cc <= 0) {
      issue('LIFECYCLE_CONFLICT_COMMISSION_MISSING', 'positive finance commission for non-delivered unit with seller base', cc ?? null);
    }
    if (f.economics_completeness === 'ACTUAL') issue('LIFECYCLE_CONFLICT_MARKED_ACTUAL', 'PROVISIONAL', f.economics_completeness);
  }
  if ((f.commission_unaccounted_qty ?? 0) !== 0 && f.commission_state !== 'UNKNOWN') {
    issue('COMMISSION_UNACCOUNTED_NOT_UNKNOWN', 'UNKNOWN', f.commission_state);
  }
  return { basis: issues.length === 0 ? actualBasis + referenceBasis : undefined, issues };
}

export function ozonSourceCompletenessIssues(facts: readonly OzonFactRow[]): OzonModelIssue[] {
  return facts.flatMap((f) => {
    const issues = sourceOperationalBasis(f).issues;
    const unproven = (f.buyout_revenue_unproven_qty ?? 0) > 0;
    if (unproven && f.economics_completeness === 'ACTUAL') issues.push({ code: 'ACTUAL_WITH_UNPROVEN_REVENUE',
      cell: `${f.d}|${f.offer_id}`, expected: 'PROVISIONAL_PARTIAL', actual: f.economics_completeness });
    if (unproven && f.commission_state === 'NOT_APPLICABLE') issues.push({ code: 'UNPROVEN_BUYOUT_COMMISSION_NOT_APPLICABLE',
      cell: `${f.d}|${f.offer_id}`, expected: 'ESTIMATED_OR_UNKNOWN', actual: f.commission_state });
    if (f.economics_completeness === 'ACTUAL') {
      // Независимый запрет false ACTUAL: полноценное покрытие оценками не равно факту.
      const provisional = (f.operational_provisional_qty ?? 0) > 0 || (f.in_transit_qty ?? 0) > 0;
      const estimated = f.commission_state === 'ESTIMATED' || f.logistics_state === 'ESTIMATED'
        || Math.abs(f.commission_estimated_rub ?? 0) > 0 || Math.abs(f.logistics_estimated_rub ?? 0) > 0;
      if (provisional || estimated) issues.push({ code: 'ACTUAL_WITH_PROVISIONAL_COMPONENTS',
        cell: `${f.d}|${f.offer_id}`, expected: 'PROVISIONAL_COMPLETE_OR_PARTIAL', actual: f.economics_completeness });
      const missing = (f.cogs_missing_qty ?? 0) > 0 || (f.provisional_cogs_missing_qty ?? 0) > 0
        || (f.commission_missing_qty ?? 0) > 0 || f.commission_state === 'UNKNOWN' || f.logistics_state === 'UNKNOWN';
      if (missing) issues.push({ code: 'ACTUAL_WITH_MISSING_MATERIAL_EVIDENCE',
        cell: `${f.d}|${f.offer_id}`, expected: 'PROVISIONAL_COMPLETE_OR_PARTIAL', actual: f.economics_completeness });
    }
    return issues;
  });
}

/** Разбирает только координаты опубликованных диапазонов, без расчётной логики. */
export function ozonWrittenReader(writes: readonly ValueWrite[]): OzonCellReader {
  const cells = new Map<string, CellValue>();
  for (const w of writes) {
    const m = /!([A-Z]+)(\d+):[A-Z]+\d+$/.exec(w.range);
    if (!m) throw new Error(`диапазон QA: ${w.range}`);
    const col = [...m[1]!].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
    const row = Number(m[2]);
    w.values.forEach((r, i) => r.forEach((v, j) => cells.set(`${row + i}:${col + j}`, v)));
  }
  return (r, c) => cells.get(`${r}:${c}`);
}

export function verifyOzonModel(a: {
  sections: readonly OzonSectionInput[]; lcd: string | null; read: OzonCellReader; mode: 'PLAN' | 'READBACK';
  effectiveRead?: OzonCellReader; effectiveThrough?: string;
}): OzonModelIssue[] {
  const issues: OzonModelIssue[] = [];
  const empty = (v: unknown) => v === undefined || v === null || v === '';
  const check = (code: string, row: number, col: number, want: number | string | undefined) => {
    const got = a.read(row, col);
    const ok = empty(want) ? empty(got) : typeof want === 'number' && typeof got === 'number'
      ? Math.abs(want - got) <= 1e-6 : want === got;
    if (!ok) issues.push({ code, cell: `${row}:${col}`, expected: want ?? '', actual: got ?? '' });
  };
  for (const sec of a.sections) {
    issues.push(...ozonSourceCompletenessIssues(sec.facts));
    const facts = new Map(sec.facts.map((f) => [`${f.d}|${f.offer_id}`, f]));
    for (let day = sec.fromDay ?? 1; day <= sec.spec.days; day++) {
      const date = `${sec.spec.key}-${String(day).padStart(2, '0')}`;
      if (a.lcd !== null && date > a.lcd) continue;
      const row = sec.spec.firstRow + day - 1;
      for (const offer of sec.spec.blocks) {
        const base = sec.spec.anchor[offer]!, key = `${date}|${offer}`, f = facts.get(key);
        const qty = f?.expected_realized_qty ?? Math.max(0, (f?.gross_qty ?? 0) - (f?.cancelled_qty ?? 0));
        if (qty > 0 && f) {
          const basis = sourceOperationalBasis(f).basis;
          const price = basis !== undefined ? basis / qty : undefined;
          check('ORDER_PRICE_OMITTED_OR_CHANGED', row, base + 14, price);
          const commissionKnown = f.commission_state === 'ACTUAL' || f.commission_state === 'ESTIMATED'
            || (f.commission_state === undefined && (f.provisional_revenue_rub ?? f.revenue ?? 0) > 0)
            || ((f.buyout_revenue_unproven_qty ?? 0) === 0 && (f.commission_not_applicable_qty ?? 0) > 0
              && (f.commission ?? 0) === 0);
          const rate = basis !== undefined && basis > 0 && commissionKnown
            ? Math.round(((f.commission ?? 0) + (f.acquiring ?? 0)) / basis * 1e8) / 1e8
            : (f.buyout_revenue_unproven_qty ?? 0) === 0 && (f.commission_not_applicable_qty ?? 0) > 0
              && (f.commission ?? 0) === 0 ? 0 : undefined;
          check('COMMISSION_OMITTED_OR_CHANGED', row, base + 17, rate);
          if (f.logistics !== null && f.logistics !== undefined) check('KNOWN_LOGISTICS_OMITTED', row, base + 19, f.logistics / qty);
          const cg = f.provisional_cogs_rub ?? f.cogs_amt;
          if (cg !== null && cg !== undefined) {
            const formula = a.read(row, base + 23);
            const m = typeof formula === 'string' ? /-([0-9]+(?:[.,][0-9]+)?)\)\)$/.exec(formula) : null;
            const term = m ? Number(m[1]!.replace(',', '.')) : undefined;
            if (term === undefined || Math.abs(term - cg / qty) > 1e-6) issues.push({
              code: 'KNOWN_COGS_OMITTED', cell: `${row}:${base + 23}`, expected: cg / qty, actual: formula,
            });
          }
          if (a.effectiveRead && a.effectiveThrough && date <= a.effectiveThrough && price !== undefined) {
            // Денежная QA идёт непосредственно от фактов, не от totals/cells composeMonth.
            const round = (v: number, n: number) => Math.round(v * 10 ** n) / 10 ** n;
            const displayPrice = round(price, 6), acq = f.acquiring ?? 0;
            const afterCommission = displayPrice - displayPrice * (rate ?? 0);
            // Штатная формула DF пуста при нулевой цене после комиссии.
            const unit = afterCommission === 0 ? undefined : afterCommission - round((f.logistics ?? 0) / qty, 6)
              - displayPrice * MANAGEMENT_TAX_RESERVE_RATE - round((cg ?? 0) / qty, 6);
            const external = a.effectiveRead(row, base + 12);
            const ads = f.impr === null || f.impr === undefined ? 0 : round(f.ads_spend ?? 0, 6);
            const direct = round((f.other_direct ?? 0) + (f.promo ?? 0) + (basis !== undefined && basis > 0 ? 0 : acq), 6);
            const total = qty * (unit ?? 0) - ads - round(f.storage ?? 0, 6) - direct
              + (typeof external === 'number' ? external : 0);
            for (const [off, expected] of [[23, unit], [10, total]] as const) {
              const actual = a.effectiveRead(row, base + off);
              const ok = expected === undefined ? empty(actual)
                : typeof actual === 'number' && Math.abs(actual - expected) <= 1e-6;
              if (!ok) issues.push({
                code: 'SOURCE_ECONOMICS_MISMATCH', cell: `${row}:${base + off}`, expected, actual,
              });
            }
          }
        }
        for (const [off, code, want] of [
          [1, 'BLOGGERS_OVERWRITTEN', sec.sheetInputs?.bloggers?.[key]],
          [12, 'EXTERNAL_ADS_OVERWRITTEN', sec.sheetInputs?.externalAds?.[key]],
        ] as const) {
          if (a.mode === 'PLAN') {
            // Никакое дневное значение этих колонок не должно входить в автоматическую запись.
            if (a.read(row, base + off) !== undefined) issues.push({ code, cell: `${row}:${base + off}`,
              expected: 'EXCLUDED_FROM_WRITE', actual: a.read(row, base + off) });
          } else if (off === 1) check(code, row, base + off, want as number | undefined);
          // CU — output: существующая формула может пересчитаться от изменённых inputs.
          // Защита от его перезаписи проверяется по фактическому write set выше.
        }
        if (sec.cart?.[key] !== undefined) check('CART_OBSERVATION_LOST', row, base + 5, sec.cart[key]);
      }
    }
  }
  return issues;
}
