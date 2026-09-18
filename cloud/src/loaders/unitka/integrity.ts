/**
 * UNITKA INTEGRITY GUARD V1 — правила целостности (Phase 1C1). Чистый модуль: без I/O.
 *
 * Отвечает на один вопрос: «можно ли доверять финансовой интерпретации этой строки?»
 * Ничего не чинит и ничего не подставляет: факт источника остаётся как есть, Guard только
 * классифицирует и объясняет.
 *
 *   DATA_ERROR ≠ падение Job'а. Техническое исполнение — qa_status (qa.ts), целостность данных —
 *   integrity_status (здесь). Engine может честно дать qa_status = PASS и integrity_status = DATA_ERROR.
 *
 *   OBSERVED PRICE ≠ FACTUAL ORDER PRICE. Наблюдённая цена продавца попадает ТОЛЬКО
 *   в diagnostic_value с явной меткой и не участвует ни в одном правиле как цена.
 *
 * Источники: wb_mart.V_UNITKA_INTEGRITY (факты SKU × день), wb_mart.V_UNITKA_COGS_CANONICAL
 * (канонический COGS из ФИЗИЧЕСКОЙ копии wb_mart.UNITKA_COGS_EFFECTIVE; evetis_ref Unitka не читает),
 * снимок листа (СПП, формулы AI, блоки). Контракт — docs/UNITKA_INTEGRITY_GUARD_V1.md.
 */
import {
  OFFSET, colA1, addDaysIso, isEmpty, asNumber,
  type Block, type CellValue,
} from './model.js';

/* ───────────────────────── типы ───────────────────────── */

export type IntegritySeverity = 'INFO' | 'EXPECTED_DELAY' | 'WARNING' | 'MANUAL_REQUIRED' | 'ERROR';
export type IntegrityStatus = 'PASS' | 'PASS_WITH_WARNINGS' | 'MANUAL_REQUIRED' | 'DATA_ERROR' | 'SYSTEM_ERROR';
export type IntegrityMode = 'off' | 'observe' | 'enforce';
export type EvaluationPhase = 'PRE_WRITE' | 'POST_WRITE';

export type IntegrityCode =
  | 'PRICE_MISSING_WITH_ORDERS'
  | 'PRICE_MISSING_NO_ORDERS'
  | 'COGS_ZERO_OR_MISSING'
  | 'COGS_SOURCE_MISMATCH'
  | 'SKU_WITHOUT_BLOCK'
  | 'SPP_MISSING'
  | 'STORAGE_MISSING'
  | 'ORDERS_SOURCE_DIVERGENCE'
  | 'COGS_SNAPSHOT_STALE'
  | 'COGS_SNAPSHOT_UNAVAILABLE';

export type PriceState = 'PRESENT' | 'MISSING_WITH_ACTIVITY' | 'MISSING_NO_ACTIVITY';
export type DivergenceClass = 'EXACT' | 'FUNNEL_GT_FACT' | 'FACT_GT_FUNNEL' | 'ONLY_FUNNEL' | 'ONLY_FACT' | 'NO_FUNNEL_ROW';

/** Строка wb_mart.V_UNITKA_INTEGRITY (типы после нормализации bq.ts). */
export interface IntegrityFactsRow {
  marketplace: 'WB';
  nmId: number;
  internalSku: string | null;
  productName: string | null;
  day: string;                 // YYYY-MM-DD
  lastClosedDate: string;
  ordersUnitka: number | null; // Q, как пишет Engine
  cancelsUnitka: number | null;// S, как пишет Engine
  ordersSource: string;
  factualOrderPrice: number | null; // NULL ≠ 0
  ordersFunnel: number | null;      // NULL = строки воронки нет
  factOrderRows: number | null;
  factOrderQty: number | null;
  /** OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE — только диагностика. */
  observedPriceDiagnostic: number | null;
  observedPriceAt: string | null;
  storageValue: number | null;
  storageDateCovered: boolean;
  priceState: PriceState;
  divergenceClass: DivergenceClass;
}

/** Строка wb_mart.V_UNITKA_COGS_CANONICAL. canonicalCogs NULL ⇔ интервалов не ровно один. */
export interface CogsCanonicalRow {
  nmId: number;
  internalSku: string | null;
  day: string;
  cogsIntervalCount: number;
  canonicalCogs: number | null;
}

/**
 * Состояние физической копии канонического COGS (wb_mart.UNITKA_COGS_EFFECTIVE):
 *   AVAILABLE   — копия свежая (≤ порога): её строки можно использовать для вердиктов COGS;
 *   STALE       — последняя успешная публикация старше порога: вердикты COGS НЕ выносятся;
 *   UNAVAILABLE — копию не прочитать (нет вью/таблицы, пусто, ошибка запроса): вердиктов нет.
 * STALE/UNAVAILABLE — проблема подсистемы публикации, а не доказанная ошибка бизнес-данных.
 */
export type CogsSnapshotState = 'AVAILABLE' | 'STALE' | 'UNAVAILABLE';

export interface CogsSnapshot {
  state: CogsSnapshotState;
  rows: readonly CogsCanonicalRow[];
  publishedAt: string | null;
  runId: string | null;
  ageHours: number | null;
  reason: string | null;
}

/** Порог «копия устарела». НЕ целевая свежесть: штатно копия обновляется раз в час (:50, 07–23 МСК). */
export const COGS_STALE_THRESHOLD_HOURS = 26;

/**
 * TIMESTAMP BigQuery — всегда UTC. Клиент Node отдаёт ISO с 'Z', а bq CLI / JSON-выгрузки — вид
 * «2026-09-18 14:18:03» без зоны, который Date.parse прочитал бы как МЕСТНОЕ время. Без зоны → UTC.
 */
export function parseBqTimestamp(s: string | null): number {
  if (s === null) return NaN;
  const t = s.trim();
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(t);
  return Date.parse(hasZone ? t : `${t.replace(' ', 'T')}Z`);
}

/**
 * Классификация копии. Пустой результат или NULL published_at — UNAVAILABLE (не «канона нет»):
 * «канона нет для SKU» допустимо утверждать ТОЛЬКО по свежей копии.
 */
export function classifyCogsSnapshot(
  read: { rows: readonly CogsCanonicalRow[]; publishedAt: string | null; runId: string | null } | { error: string },
  now: Date,
  staleHours = COGS_STALE_THRESHOLD_HOURS,
): CogsSnapshot {
  if ('error' in read) return { state: 'UNAVAILABLE', rows: [], publishedAt: null, runId: null, ageHours: null, reason: read.error };
  const ts = parseBqTimestamp(read.publishedAt);
  if (read.rows.length === 0 || !Number.isFinite(ts)) {
    return { state: 'UNAVAILABLE', rows: [], publishedAt: read.publishedAt, runId: read.runId, ageHours: null, reason: 'копия пуста или не опубликована' };
  }
  const ageHours = Math.round(((now.getTime() - ts) / 3_600_000) * 100) / 100;
  if (ageHours > staleHours) {
    return { state: 'STALE', rows: [], publishedAt: read.publishedAt, runId: read.runId, ageHours, reason: `последняя публикация ${ageHours} ч назад > ${staleHours} ч` };
  }
  return { state: 'AVAILABLE', rows: read.rows, publishedAt: read.publishedAt, runId: read.runId, ageHours, reason: null };
}

export interface IntegrityIssue {
  marketplace: 'WB';
  nmId: number | null;
  day: string | null;          // null — issue уровня SKU (COGS) или магазина
  field: string;
  code: IntegrityCode;
  severity: IntegritySeverity;
  /** Блокирует доверие к строке/покрытию (НЕ блокирует запись фактов). */
  blocking: boolean;
  financialInvalid: boolean;
  source: string;
  sourceValue: string | null;
  diagnosticValue: string | null;
  dependentFields: string[];
  message: string;
}

/** Метка диагностической цены — обязательный префикс diagnostic_value. */
export const OBSERVED_PRICE_LABEL = 'OBSERVED_PRICE_NOT_FACTUAL_ORDER_PRICE';

/** Колонки листа, которые становятся недостоверными при отсутствии цены (проверено на live-формулах, Phase 1A). */
export const PRICE_DEPENDENT_FIELDS = ['AC', 'AE', 'AH', 'AI', 'W', 'V', 'Y', 'Z', 'SUMMARY_I', 'SUMMARY_K', 'MTD_767'] as const;
export const COGS_DEPENDENT_FIELDS = ['AI', 'W', 'V', 'Y', 'SUMMARY_I', 'MTD_767'] as const;
export const SPP_DEPENDENT_FIELDS = ['AC', 'Z', 'SUMMARY_K', 'AC767', 'Z767', 'K767'] as const;
export const STORAGE_DEPENDENT_FIELDS = ['AG', 'W', 'V', 'SUMMARY_I', 'AG767'] as const;

/* ───────────────────────── статус ───────────────────────── */

const STATUS_RANK: Record<IntegrityStatus, number> = {
  PASS: 0, PASS_WITH_WARNINGS: 1, MANUAL_REQUIRED: 2, DATA_ERROR: 3, SYSTEM_ERROR: 4,
};

/**
 * Детерминированный приоритет: SYSTEM_ERROR > DATA_ERROR > MANUAL_REQUIRED > PASS_WITH_WARNINGS > PASS.
 * INFO и EXPECTED_DELAY статус не поднимают. SYSTEM_ERROR — только от сбоя исполнения, не от issue.
 */
export function aggregateStatus(issues: readonly Pick<IntegrityIssue, 'severity'>[], systemError = false): IntegrityStatus {
  if (systemError) return 'SYSTEM_ERROR';
  let s: IntegrityStatus = 'PASS';
  for (const i of issues) {
    const t: IntegrityStatus =
      i.severity === 'ERROR' ? 'DATA_ERROR'
        : i.severity === 'MANUAL_REQUIRED' ? 'MANUAL_REQUIRED'
          : i.severity === 'WARNING' ? 'PASS_WITH_WARNINGS'
            : 'PASS';
    if (STATUS_RANK[t] > STATUS_RANK[s]) s = t;
  }
  return s;
}

/* ───────────────────────── время (Europe/Moscow) ───────────────────────── */

/** Дата и минуты суток в Europe/Moscow. МСК без перехода на летнее время, но считаем через Intl. */
export function moscowParts(now: Date): { date: string; minutes: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(now);
  const g = (t: string): string => parts.find((p) => p.type === t)?.value ?? '00';
  const hour = g('hour') === '24' ? 0 : Number(g('hour'));
  return { date: `${g('year')}-${g('month')}-${g('day')}`, minutes: hour * 60 + Number(g('minute')) + Number(g('second')) / 60 };
}

/** "HH:MM" → минуты суток; null, если строка не соответствует контракту. */
export function parseHhMm(s: string): number | null {
  const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(s.trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

export const DEFAULT_STORAGE_DUE_MSK = '12:15';

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86_400_000);
}

/**
 * STORAGE_MISSING по времени МСК (Phase 1B §10):
 *   D−1 до срока (по умолчанию 12:15 МСК) → EXPECTED_DELAY; D−1 со срока → WARNING;
 *   D−2 → WARNING; старше D−2 → ERROR. Будущее/сегодня → null (не закрытый день).
 * Срок 12:15 = старт загрузчика 11:45 + 30 мин, строго ДО резервного окна 12:30.
 */
export function storageSeverity(day: string, now: Date, dueMinutes = parseHhMm(DEFAULT_STORAGE_DUE_MSK)!): IntegritySeverity | null {
  const { date: today, minutes } = moscowParts(now);
  const age = daysBetween(day, today);
  if (age <= 0) return null;
  if (age === 1) return minutes < dueMinutes ? 'EXPECTED_DELAY' : 'WARNING';
  if (age === 2) return 'WARNING';
  return 'ERROR';
}

/* ───────────────────────── парсер COGS в формуле AI ───────────────────────── */

/**
 * Разрешённые абсолютные ссылки на COGS в формуле AI. Единственная известная — $R$45
 * (блок 1, 252442517). Любая другая ссылка — UNRECOGNISED, а не угадывание.
 */
export const APPROVED_COGS_REFS: readonly string[] = ['R45'];

/** Допуск сверки COGS листа с каноном: полкопейки — максимум ошибки округления канона до 2 знаков. */
export const COGS_TOLERANCE_RUB = 0.005;

export type CogsTerm =
  | { kind: 'literal'; value: number; text: string }
  | { kind: 'ref'; ref: string; text: string }
  | { kind: 'unrecognised'; text: string };

/**
 * Контракт live-формулы AI (проверено Phase 1A на всех 24 блоках):
 *   =IF($<DATE><r>>LAST_CLOSED_DATE,"",<AE><r>-<AF><r>-<AH><r>-<COGS>)
 * Разделитель аргументов — «,» или «;» (локаль книги); в «;»-локали десятичный — «,» или «.».
 * Колонки/строки ОБЯЗАНЫ совпасть с геометрией блока; COGS — число или разрешённая
 * абсолютная ссылка. Всё прочее — UNRECOGNISED (fail-closed в issue).
 */
export function parseCogsTerm(formula: CellValue, block: Block, row: number): CogsTerm {
  const text = typeof formula === 'string' ? formula : String(formula ?? '');
  const f = text.replace(/\s+/g, '').toUpperCase();
  const L = (o: number): string => colA1(block.start + o);
  // Точное сопоставление с контрактом по частям: никаких «жадных» регулярных выражений по формуле.
  const head = `=IF($${L(OFFSET.date)}${row}>LAST_CLOSED_DATE`;
  if (!f.startsWith(head)) return { kind: 'unrecognised', text };
  const sep = f.charAt(head.length);
  if (sep !== ',' && sep !== ';') return { kind: 'unrecognised', text };
  const core = `${sep}""${sep}${L(OFFSET.priceMinusComm)}${row}-${L(OFFSET.logistics)}${row}-${L(OFFSET.tax)}${row}-`;
  if (!f.startsWith(core, head.length) || !f.endsWith(')')) return { kind: 'unrecognised', text };
  const term = f.slice(head.length + core.length, -1);
  const num = sep === ',' ? /^\d+(\.\d+)?$/ : /^\d+([.,]\d+)?$/;
  if (num.test(term)) return { kind: 'literal', value: Number(term.replace(',', '.')), text: term };
  const ref = /^\$([A-Z]{1,3})\$(\d+)$/.exec(term);
  if (ref && APPROVED_COGS_REFS.includes(`${ref[1]}${ref[2]}`)) return { kind: 'ref', ref: `${ref[1]}${ref[2]}`, text: term };
  return { kind: 'unrecognised', text };
}

/* ───────────────────────── правила ───────────────────────── */

export interface IntegrityInputs {
  facts: readonly IntegrityFactsRow[];
  /** Копия канонического COGS. Не AVAILABLE — COGS-вердиктов нет, вместо них issue о копии (WARNING). */
  cogs: CogsSnapshot;
  blocks: readonly Block[];
  lcd: string;
  monthStart: string;
  /** Строка листа первого дня секции месяца (Phase 2B: из MonthLayout, не константа 737). */
  firstDailyRow: number;
  /** Снимок: значения (для СПП) и формулы (для AI) закрытых дней. */
  cellAt: (row: number, col: number) => CellValue;
  formulaAt: (row: number, col: number) => CellValue;
  /** Значения разрешённых абсолютных ссылок COGS (например, {R45: 240}). */
  refValues: Readonly<Record<string, CellValue>>;
  now: Date;
  storageDueMinutes: number;
}

const fmt = (v: number | null | undefined): string | null => (v === null || v === undefined ? null : String(v));
const positive = (v: number | null | undefined): boolean => typeof v === 'number' && v > 0;

function issue(p: Omit<IntegrityIssue, 'marketplace'>): IntegrityIssue {
  return { marketplace: 'WB', ...p };
}

/** Правила 1, 2: цена. Только SKU с блоком (для SKU без блока — SKU_WITHOUT_BLOCK). */
export function priceRules(facts: readonly IntegrityFactsRow[], blockNm: ReadonlySet<number>, lcd: string): IntegrityIssue[] {
  const out: IntegrityIssue[] = [];
  for (const r of facts) {
    if (r.day > lcd || !blockNm.has(r.nmId)) continue;
    if (r.factualOrderPrice !== null) continue; // PRESENT; 0 ₽ Engine не пишет никогда
    const activity = positive(r.ordersUnitka) || positive(r.cancelsUnitka);
    if (activity) {
      out.push(issue({
        nmId: r.nmId, day: r.day, field: 'price', code: 'PRICE_MISSING_WITH_ORDERS', severity: 'ERROR',
        blocking: true, financialInvalid: true, source: 'V_UNITKA_INTEGRITY.factual_order_price (FACT_ORDERS)',
        sourceValue: `price=NULL; Q=${fmt(r.ordersUnitka)}; S=${fmt(r.cancelsUnitka)}; orders_source=${r.ordersSource}; fact_order_rows=${fmt(r.factOrderRows) ?? 'NULL'}`,
        diagnosticValue: r.observedPriceDiagnostic === null ? null
          : `${OBSERVED_PRICE_LABEL}=${r.observedPriceDiagnostic} @ ${r.observedPriceAt ?? '?'}`,
        dependentFields: [...PRICE_DEPENDENT_FIELDS],
        message: `${r.day} ${r.nmId}: заказы/отмены есть (Q=${fmt(r.ordersUnitka)}, S=${fmt(r.cancelsUnitka)}), фактической цены заказа нет — финансовые поля строки недостоверны`,
      }));
    } else {
      out.push(issue({
        nmId: r.nmId, day: r.day, field: 'price', code: 'PRICE_MISSING_NO_ORDERS', severity: 'INFO',
        blocking: false, financialInvalid: false, source: 'V_UNITKA_INTEGRITY.factual_order_price',
        sourceValue: 'price=NULL; Q=0; S=0', diagnosticValue: null, dependentFields: [],
        message: `${r.day} ${r.nmId}: заказов нет — цены заказа быть не может`,
      }));
    }
  }
  return out;
}

/** Правило 8: хранение — уровень ДАТЫ (покрытие отчёта дневное), одна issue на дату. */
export function storageRules(facts: readonly IntegrityFactsRow[], lcd: string, now: Date, dueMinutes: number): IntegrityIssue[] {
  const uncovered = new Set<string>();
  for (const r of facts) if (r.day <= lcd && !r.storageDateCovered) uncovered.add(r.day);
  const out: IntegrityIssue[] = [];
  for (const day of [...uncovered].sort()) {
    const sev = storageSeverity(day, now, dueMinutes);
    if (sev === null) continue;
    out.push(issue({
      nmId: null, day, field: 'storage', code: 'STORAGE_MISSING', severity: sev,
      blocking: false, financialInvalid: false, source: 'V_WB_STORAGE_DAILY (покрытие даты)',
      sourceValue: 'date_not_covered', diagnosticValue: null, dependentFields: [...STORAGE_DEPENDENT_FIELDS],
      message: sev === 'EXPECTED_DELAY'
        ? `${day}: отчёт хранения ещё не пришёл — ожидаемо до срока загрузки`
        : `${day}: отчёта хранения нет — W/V/I до его прихода не учитывают хранение`,
    }));
  }
  return out;
}

/** Правило 9: расхождение воронки и FACT_ORDERS. INFO по умолчанию; WARNING при ONLY_* или |Δ| ≥ 2. */
export function divergenceRules(facts: readonly IntegrityFactsRow[], lcd: string): IntegrityIssue[] {
  const out: IntegrityIssue[] = [];
  for (const r of facts) {
    if (r.day > lcd || r.divergenceClass === 'EXACT') continue;
    const fq = r.factOrderQty ?? 0;
    const delta = r.ordersFunnel === null ? null : Math.abs(r.ordersFunnel - fq);
    const warn = r.divergenceClass === 'ONLY_FUNNEL' || r.divergenceClass === 'ONLY_FACT' || (delta !== null && delta >= 2);
    out.push(issue({
      nmId: r.nmId, day: r.day, field: 'orders', code: 'ORDERS_SOURCE_DIVERGENCE', severity: warn ? 'WARNING' : 'INFO',
      blocking: false, financialInvalid: false, source: 'V_WB_FUNNEL_DAILY vs FACT_ORDERS',
      sourceValue: `${r.divergenceClass}; funnel=${fmt(r.ordersFunnel) ?? 'NULL'}; fact_qty=${fmt(r.factOrderQty) ?? 'NULL'}`,
      diagnosticValue: null, dependentFields: ['Q'],
      message: `${r.day} ${r.nmId}: воронка и FACT_ORDERS расходятся (${r.divergenceClass}); семантика не доказана — не ошибка`,
    }));
  }
  return out;
}

/** Правило 5: активные SKU источника без блока в листе. */
export function coverageRules(facts: readonly IntegrityFactsRow[], blocks: readonly Block[], lcd: string): IntegrityIssue[] {
  const blockNm = new Set(blocks.map((b) => b.nmId));
  const names = new Map<number, string | null>();
  for (const r of facts) if (!names.has(r.nmId)) names.set(r.nmId, r.productName);
  return [...names.keys()].filter((nm) => !blockNm.has(nm)).sort((a, b) => a - b).map((nm) => issue({
    nmId: nm, day: null, field: 'block', code: 'SKU_WITHOUT_BLOCK', severity: 'ERROR',
    blocking: true, financialInvalid: false, source: 'V_UNITKA_INTEGRITY (активные REF_SKU_MASTER) vs строка блоков листа',
    sourceValue: `active_sku_without_block; lcd=${lcd}`, diagnosticValue: names.get(nm) ?? null,
    dependentFields: ['SUMMARY_C..K', 'MTD_767'],
    message: `${nm} активен в REF_SKU_MASTER, но блока в листе нет — сводка магазина неполна`,
  }));
}

/** Правило 7: СПП. Только закрытый день, Q > 0 (факт Engine), ячейка AB действительно пуста. 0 = заполнено. */
export function sppRules(inp: Pick<IntegrityInputs, 'facts' | 'blocks' | 'lcd' | 'monthStart' | 'firstDailyRow' | 'cellAt'>): IntegrityIssue[] {
  const orders = new Map<string, number | null>();
  for (const r of inp.facts) orders.set(`${r.nmId}|${r.day}`, r.ordersUnitka);
  const closedDays = daysBetween(inp.monthStart, inp.lcd) + 1;
  const out: IntegrityIssue[] = [];
  for (const b of inp.blocks) {
    for (let i = 0; i < closedDays; i++) {
      const day = addDaysIso(inp.monthStart, i);
      if (!positive(orders.get(`${b.nmId}|${day}`))) continue;
      const row = inp.firstDailyRow + i;
      const col = b.start + OFFSET.spp;
      const v = inp.cellAt(row, col);
      if (!isEmpty(v)) continue; // включая числовой 0 — это заполненное значение
      out.push(issue({
        nmId: b.nmId, day, field: 'spp', code: 'SPP_MISSING', severity: 'MANUAL_REQUIRED',
        blocking: false, financialInvalid: false, source: `SHEET:${colA1(col)}${row}`,
        sourceValue: 'blank', diagnosticValue: null, dependentFields: [...SPP_DEPENDENT_FIELDS],
        message: `${day} ${b.nmId}: заказы есть, СПП не введена — ДРР и цена с СПП неполны (прибыль не затронута)`,
      }));
    }
  }
  return out;
}

/** Правила 3, 4: COGS в формуле AI против канона. Issue уровня SKU (day = null). */
export function cogsRules(inp: IntegrityInputs): IntegrityIssue[] {
  if (inp.cogs.state !== 'AVAILABLE') {
    // Устаревшая или недоступная копия НИКОГДА не даёт COGS_VALID / COGS_ZERO_OR_MISSING / COGS_SOURCE_MISMATCH.
    const stale = inp.cogs.state === 'STALE';
    return [issue({
      nmId: null, day: null, field: 'cogs', code: stale ? 'COGS_SNAPSHOT_STALE' : 'COGS_SNAPSHOT_UNAVAILABLE',
      severity: 'WARNING', blocking: false, financialInvalid: false,
      source: 'wb_mart.V_UNITKA_COGS_CANONICAL (копия wb_mart.UNITKA_COGS_EFFECTIVE)',
      sourceValue: `state=${inp.cogs.state}; published_at=${inp.cogs.publishedAt ?? 'NULL'}; age_h=${inp.cogs.ageHours ?? 'NULL'}`,
      diagnosticValue: inp.cogs.reason, dependentFields: [...COGS_DEPENDENT_FIELDS],
      message: stale
        ? `копия COGS устарела (${inp.cogs.reason}) — сверка COGS не выполнялась`
        : `копия COGS недоступна (${inp.cogs.reason ?? 'причина неизвестна'}) — сверка COGS не выполнялась`,
    })];
  }
  const canon = new Map<string, CogsCanonicalRow>();
  for (const c of inp.cogs.rows) canon.set(`${c.nmId}|${c.day}`, c);
  const activity = new Set<number>();
  const activeDays = new Map<number, string[]>();
  for (const r of inp.facts) {
    if (r.day > inp.lcd) continue;
    if (positive(r.ordersUnitka) || positive(r.cancelsUnitka)) {
      activity.add(r.nmId);
      (activeDays.get(r.nmId) ?? activeDays.set(r.nmId, []).get(r.nmId)!).push(r.day);
    }
  }
  const closedDays = daysBetween(inp.monthStart, inp.lcd) + 1;
  const out: IntegrityIssue[] = [];
  for (const b of inp.blocks) {
    const aiCol = b.start + OFFSET.unitProfit;
    const sheetVals = new Map<string, { value: number | null; text: string }>();
    const problems: string[] = [];
    const canonVals = new Set<string>();
    for (let i = 0; i < closedDays; i++) {
      const day = addDaysIso(inp.monthStart, i);
      const row = inp.firstDailyRow + i;
      const t = parseCogsTerm(inp.formulaAt(row, aiCol), b, row);
      let value: number | null = null;
      if (t.kind === 'literal') value = t.value;
      else if (t.kind === 'ref') {
        const rv = inp.refValues[t.ref];
        const n = isEmpty(rv) ? NaN : asNumber(rv);
        value = Number.isFinite(n) ? n : null;
      }
      const key = t.kind === 'unrecognised' ? 'UNRECOGNISED_FORMULA' : `${t.text}=${value === null ? 'BLANK' : value}`;
      if (!sheetVals.has(key)) sheetVals.set(key, { value, text: t.kind === 'unrecognised' ? `${colA1(aiCol)}${row}: ${t.text}` : t.text });
      const c = canon.get(`${b.nmId}|${day}`);
      const cv = c?.canonicalCogs ?? null;
      canonVals.add(cv === null ? `NULL(n=${c?.cogsIntervalCount ?? 0})` : String(cv));
      if (t.kind === 'unrecognised') problems.push(`${day}:UNRECOGNISED_FORMULA`);
      else if (value === null || value <= 0) problems.push(`${day}:SHEET_${value === null ? 'BLANK' : 'ZERO'}`);
      if (cv === null || cv <= 0) problems.push(`${day}:CANONICAL_${cv === null ? (c && c.cogsIntervalCount > 1 ? 'MULTIPLE' : 'MISSING') : 'ZERO'}`);
    }
    const sheetDesc = [...sheetVals.entries()].map(([k, v]) => (k === 'UNRECOGNISED_FORMULA' ? v.text : k)).join(' | ');
    const canonDesc = [...canonVals].join(' | ');
    const active = activity.has(b.nmId);
    if (problems.length) {
      out.push(issue({
        nmId: b.nmId, day: null, field: 'cogs', code: 'COGS_ZERO_OR_MISSING',
        severity: active ? 'ERROR' : 'WARNING', blocking: active, financialInvalid: active,
        source: `SHEET:${colA1(aiCol)} + V_UNITKA_COGS_CANONICAL`,
        sourceValue: `sheet=${sheetDesc}; canonical=${canonDesc}; reasons=${[...new Set(problems.map((p) => p.split(':')[1]))].join(',')}`,
        diagnosticValue: active ? `active_days=${(activeDays.get(b.nmId) ?? []).join(',')}` : 'no_activity_this_month (latent)',
        dependentFields: [...COGS_DEPENDENT_FIELDS],
        message: `${b.nmId}: COGS нулевая/отсутствует/не распознана${active ? ' при наличии заказов — прибыль строк недостоверна' : ' — латентно (заказов в месяце нет)'}`,
      }));
      continue;
    }
    // Нет проблем присутствия → сверка значений. Литерал листа — канон, округлённый до копеек
    // (live: 159.3 ↔ 159.295, 182.79 ↔ 182.785, 406.65 ↔ 406.651629). Округление даёт |Δ| ≤ 0.005,
    // поэтому допуск — полкопейки (+ε на двоичную арифметику). НЕ Math.round(cv*100): 159.295*100 =
    // 15929.4999… и дал бы ложное расхождение.
    const tol = COGS_TOLERANCE_RUB + 1e-9;
    const mism: string[] = [];
    for (let i = 0; i < closedDays; i++) {
      const day = addDaysIso(inp.monthStart, i);
      const row = inp.firstDailyRow + i;
      const t = parseCogsTerm(inp.formulaAt(row, aiCol), b, row);
      const value = t.kind === 'literal' ? t.value : asNumber(inp.refValues[(t as { ref: string }).ref]);
      const cv = canon.get(`${b.nmId}|${day}`)!.canonicalCogs as number;
      if (!(Math.abs(value - cv) <= tol)) mism.push(`${day}:${value}≠${cv}`);
    }
    if (mism.length) {
      out.push(issue({
        nmId: b.nmId, day: null, field: 'cogs', code: 'COGS_SOURCE_MISMATCH', severity: 'WARNING',
        blocking: false, financialInvalid: false, source: `SHEET:${colA1(aiCol)} + V_UNITKA_COGS_CANONICAL`,
        sourceValue: `sheet=${sheetDesc}; canonical=${canonDesc}; days=${mism.length}`,
        diagnosticValue: mism.slice(0, 3).join('; '), dependentFields: [...COGS_DEPENDENT_FIELDS],
        message: `${b.nmId}: COGS в формуле AI (${sheetDesc}) ≠ канону (${canonDesc}) — прибыль посчитана на неверной базе`,
      }));
    }
  }
  return out;
}

/** Все правила V1. BLOCK_WITHOUT_SKU остаётся за plan.ts (BLOCK_MISSING, fail-closed до записи). */
export function evaluateIntegrity(inp: IntegrityInputs): IntegrityIssue[] {
  const blockNm = new Set(inp.blocks.map((b) => b.nmId));
  return [
    ...priceRules(inp.facts, blockNm, inp.lcd),
    ...cogsRules(inp),
    ...coverageRules(inp.facts, inp.blocks, inp.lcd),
    ...sppRules(inp),
    ...storageRules(inp.facts, inp.lcd, inp.now, inp.storageDueMinutes),
    ...divergenceRules(inp.facts, inp.lcd),
  ];
}

/* ───────────────────────── сводка и журнал ───────────────────────── */

export interface IntegritySummary {
  status: IntegrityStatus;
  mode: IntegrityMode;
  evaluated_at: string;
  phase: EvaluationPhase;
  counts: Record<IntegritySeverity, number>;
  financially_invalid_rows: number;
  issue_codes: Record<string, number>;
  /** Ключи ERROR-issue (≤ 50): «день/nm/код». INFO сюда не попадают никогда. */
  error_keys: string[];
  error_keys_truncated: boolean;
  cogs_source: CogsSnapshotState;
  cogs_published_at: string | null;
  cogs_age_hours: number | null;
  subsystem_failure?: { code: string; message: string };
}

export const ERROR_KEYS_LIMIT = 50;

export function summarize(
  issues: readonly IntegrityIssue[], mode: IntegrityMode, phase: EvaluationPhase, evaluatedAt: Date,
  cogs: Pick<CogsSnapshot, 'state' | 'publishedAt' | 'ageHours'>, subsystemFailure?: { code: string; message: string },
): IntegritySummary {
  const counts: Record<IntegritySeverity, number> = { INFO: 0, EXPECTED_DELAY: 0, WARNING: 0, MANUAL_REQUIRED: 0, ERROR: 0 };
  const codes: Record<string, number> = {};
  const invalidRows = new Set<string>();
  const errKeys: string[] = [];
  for (const i of issues) {
    counts[i.severity]++;
    codes[i.code] = (codes[i.code] ?? 0) + 1;
    if (i.financialInvalid && i.day !== null) invalidRows.add(`${i.nmId}|${i.day}`);
    if (i.severity === 'ERROR') errKeys.push(`${i.day ?? '-'}/${i.nmId ?? '-'}/${i.code}`);
  }
  errKeys.sort();
  return {
    status: aggregateStatus(issues, subsystemFailure !== undefined),
    mode, evaluated_at: evaluatedAt.toISOString(), phase, counts,
    financially_invalid_rows: invalidRows.size, issue_codes: codes,
    error_keys: errKeys.slice(0, ERROR_KEYS_LIMIT), error_keys_truncated: errKeys.length > ERROR_KEYS_LIMIT,
    cogs_source: cogs.state, cogs_published_at: cogs.publishedAt, cogs_age_hours: cogs.ageHours,
    ...(subsystemFailure ? { subsystem_failure: subsystemFailure } : {}),
  };
}
