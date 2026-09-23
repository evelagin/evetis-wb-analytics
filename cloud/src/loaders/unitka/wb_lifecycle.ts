/**
 * WB — AUTO-LCD И ЖИЗНЕННЫЙ ЦИКЛ МЕСЯЦА В СУТОЧНОМ ПИСАТЕЛЕ (Gate 10).
 *
 * ЧТО БЫЛО. Engine вычислял LCD в BigQuery и писал его (зеркало WB736 + именованный диапазон
 * LAST_CLOSED_DATE = ZZ_CONFIG!B2) в ТОМ ЖЕ batchUpdate, что и факты, — ДО перечитывания.
 * 23.09.2026 в 10:00:42 МСК он так и сделал: book_lcd 21.09 → 22.09, LCD_ADVANCE = 2. Запись
 * прошла проверку — для WB всё было верно. Но тот же B2 читал Ozon, чей писатель в тот день
 * не запустился: успех WB объявил 22.09 закрытым и на листе Ozon. Ручного сдвига не было.
 *
 * ЧТО СТАЛО. Три разных LCD, и путать их нельзя:
 *   COMMITTED  — что держит книга (B2) на старте прогона;
 *   CANDIDATE  — максимальная смежная безопасная дата; по ней планируются данные;
 *   AFTER      — что держит книга после протокола коммита (по перечитыванию).
 * Пока цикл не завершён — запись, перечитывание, QA, — B2 остаётся COMMITTED.
 *
 * МЕСЯЦ. Если кандидат вошёл в месяц, которого нет в листе, секция создаётся ТЕМ ЖЕ
 * канонический генератором, что и в unitka-month-prep (planMonthPrep + toStructureRequests),
 * а не вторым. Манифест отката строится и пишется в журнал ДО записи — обратимость сохранена.
 * Ручной гейт отпечатка манифеста (UNITKA_MONTH_PREP_MANIFEST_DIGEST) в суточном пути НЕ
 * применяется: по решению владельца переход месяца не должен требовать человека. Отдельный
 * загрузчик unitka-month-prep остаётся административным/аварийным инструментом.
 */
import { LoaderError } from '../../errors.js';
import type { Logger } from '../../logging.js';
import type { Config } from '../../config.js';
import type { UnitkaBq } from './bq.js';
import { WB_FUNNEL_COVERAGE_DAYS } from './bq.js';
import type { SheetsGateway } from './sheets.js';
import { colA1, isoToSerial, BOOK_ANCHORS } from './model.js';
import { quoteSheet, readColumnA } from './section.js';
import { formatMonthKey, locateSection, monthKeyOf, nextMonth, previousMonth, type MonthKey } from './calendar.js';
import { classifyCogsSnapshot } from './integrity.js';
import { validateSection } from './plan.js';
import { planMonthPrep, templateRowsOf, toStructureRequests } from './monthprep.js';
import { buildRollbackManifest, encodeManifest } from './monthrollback.js';
import { sectionSnapshot, planLog } from './monthprep_io.js';
import {
  decideCandidate, coveredByLoaderRuns, addDaysIso, type LcdCell, type CandidateDecision, type LcdMode, type RunCoverageRule,
} from './lcd.js';
import { readLifecycleConfig, dateCellToIso, type LifecycleConfig } from './config_sheet.js';

/** Идентичность события жизненного цикла. НЕ ctx.message: логгер затирает имя события полем message. */
export type WbLifecycleEvent =
  | 'LCD_CANDIDATE' | 'LCD_NOT_ADVANCED' | 'LCD_COMMIT_CONFLICT' | 'LCD_COMMITTED' | 'LCD_WRITE_FAILED'
  | 'LCD_REVERTED' | 'LCD_MIRROR_REPAIRED' | 'MANUAL_OVERRIDE_ACTIVE' | 'NEW_MONTH_CREATED' | 'INTEGRITY_FAILED';

export function lifecycleLog(log: Logger, event: WbLifecycleEvent, ctx: Record<string, unknown>, level: 'info' | 'warn' | 'error' = 'info'): void {
  // поле message затёрло бы имя события (logging.ts раскрывает ctx последним) — не пропускаем его
  const safe = Object.fromEntries(Object.entries(ctx).filter(([k]) => k !== 'message'));
  log[level]('unitka_lifecycle', { lifecycle_event: event, platform: 'WB', ...safe });
}

/* ─────────────────────────── авторитетная ячейка ─────────────────────────── */

const WB_LCD_NAME = 'LAST_CLOSED_DATE';

/**
 * B2 (именованный диапазон) + зеркало WB736 — ОДНОЙ записью: разъехаться они не могут.
 * read() читает авторитет — именованный диапазон; зеркало проверяет QA после коммита.
 */
export class WbLcdCell implements LcdCell {
  constructor(private readonly sheets: SheetsGateway, private readonly sheetName: string, private readonly anchorCol: number) {}
  async read(): Promise<string | null> {
    const [grid] = await this.sheets.readValues([WB_LCD_NAME]);
    return dateCellToIso(grid?.[0]?.[0]);
  }
  /**
   * Диапазон зеркала — в том же виде «X736:X736», в каком его всегда писал toWriteRanges: это
   * проверенный в production формат, а не новый.
   */
  private mirrorRange(): string {
    const c = colA1(this.anchorCol);
    return `${quoteSheet(this.sheetName)}!${c}${BOOK_ANCHORS.LCD_MIRROR_ROW}:${c}${BOOK_ANCHORS.LCD_MIRROR_ROW}`;
  }
  async write(iso: string): Promise<void> {
    const serial = isoToSerial(iso);
    await this.sheets.batchWrite([
      { range: WB_LCD_NAME, values: [[serial]] },
      { range: this.mirrorRange(), values: [[serial]] },
    ]);
  }
  /** Выравнивание зеркала без движения LCD (производное состояние). */
  async repairMirror(iso: string): Promise<void> {
    await this.sheets.batchWrite([{ range: this.mirrorRange(), values: [[isoToSerial(iso)]] }]);
  }
}

/* ─────────────────────────── кандидат ─────────────────────────── */

/** Семантика покрытия гейтящих загрузчиков WB (см. lcd.RunCoverageRule — почему не точный матч). */
export const WB_COVERAGE_RULES: Readonly<Record<string, RunCoverageRule>> = {
  funnel: { kind: 'WINDOW', days: WB_FUNNEL_COVERAGE_DAYS },
  mart: { kind: 'CUMULATIVE' },
};

export interface WbCandidate {
  readonly mode: LcdMode;
  readonly committed: string;
  readonly candidate: string;
  readonly decision: CandidateDecision;
  readonly ceiling: string;
  readonly d1Msk: string;
  readonly lifecycle: LifecycleConfig['state'];
  /** Зарезервировано для журнала; кандидат по месяцу не зажимается (см. ensureWbMonthSection). */
  readonly clampedToMonth: string | null;
}

export async function resolveWbCandidate(a: {
  sheets: SheetsGateway; bq: UnitkaBq; config: Config; log: Logger;
  canonical: { lastClosedDate: string; d1Msk: string };
}): Promise<WbCandidate> {
  const life = await readLifecycleConfig(a.sheets);
  if (life.state === 'DRIFT' || life.state === 'INVALID') {
    throw new LoaderError(`блок жизненного цикла ZZ_CONFIG: ${life.issues.slice(0, 4).join(' | ')}`, 'LIFECYCLE_CONFIG_INVALID');
  }
  const wb = life.state === 'PRESENT' ? life.wb : { mode: 'AUTO' as const, manualLcd: undefined, manualRaw: null };

  const [grid] = await a.sheets.readValues([WB_LCD_NAME]);
  const committed = dateCellToIso(grid?.[0]?.[0]);
  if (!committed) throw new LoaderError(`${WB_LCD_NAME} в книге пуст или не дата: ${JSON.stringify(grid?.[0]?.[0] ?? null)}`, 'LCD_BOOK_INVALID');

  // Порядок проверок — прежний (plan.ts до Gate 10): сначала SOURCE_STALE, затем LCD_REGRESSION.
  // Одна и та же ситуация обязана давать тот же код, иначе сломаются алерты и разбор инцидентов.
  const lag = Math.round((Date.parse(`${a.canonical.d1Msk}T00:00:00Z`) - Date.parse(`${a.canonical.lastClosedDate}T00:00:00Z`)) / 86_400_000);
  if (lag > a.config.unitkaMaxLagDays) {
    throw new LoaderError(`LAST_CLOSED_DATE = ${a.canonical.lastClosedDate} отстаёт от D-1 (${a.canonical.d1Msk}) на ${lag} дн. > ${a.config.unitkaMaxLagDays}`, 'SOURCE_STALE');
  }
  // Источник «откатился» ниже опубликованного: планирование по нему ЗАТЁРЛО БЫ уже закрытые
  // сутки пустотой. Это прежний safety-инвариант LCD_REGRESSION — сохраняется без изменений.
  if (a.canonical.lastClosedDate < committed) {
    throw new LoaderError(`LAST_CLOSED_DATE из BigQuery ${a.canonical.lastClosedDate} раньше, чем в книге ${committed}`, 'LCD_REGRESSION');
  }

  const runs = await a.bq.wbRunCoverage(a.config.rawDataset, committed);
  const decision = decideCandidate({
    platform: 'WB', mode: wb.mode, manualLcd: wb.manualLcd, committed, d1Msk: a.canonical.d1Msk,
    covered: coveredByLoaderRuns(runs, WB_COVERAGE_RULES), sourceCeiling: a.canonical.lastClosedDate,
  });
  if (decision.code === 'MANUAL_LCD_INVALID' || decision.code === 'MANUAL_LCD_REGRESSION') {
    lifecycleLog(a.log, 'MANUAL_OVERRIDE_ACTIVE', { outcome: 'REJECTED', code: decision.code, raw: wb.manualRaw, notes: decision.notes }, 'error');
    throw new LoaderError(`WB_MANUAL_LCD отвергнута: ${decision.notes.join('; ')}`, decision.code);
  }

  const candidate = decision.candidate;
  const clampedToMonth: string | null = null;
  // Суточная вью фактов (V_UNITKA_DAILY_FACT) привязана в SQL к КАНОНИЧЕСКОМУ LCD и отдаёт только его
  // месяц. Если барьер придержал кандидата в более раннем месяце, фактов этого месяца у писателя нет:
  // план упал бы на BLOCK_MISSING с неверным объяснением. Отказ — явный и до записи.
  if (monthKeyOf(candidate).year !== monthKeyOf(a.canonical.lastClosedDate).year
      || monthKeyOf(candidate).month !== monthKeyOf(a.canonical.lastClosedDate).month) {
    throw new LoaderError(`кандидат ${candidate} придержан в месяце, которого суточная вью фактов уже не отдаёт `
      + `(канон ${a.canonical.lastClosedDate}): ${decision.notes.join('; ')}`, 'LCD_CANDIDATE_FACTS_UNAVAILABLE');
  }

  const out: WbCandidate = { mode: wb.mode, committed, candidate, decision, ceiling: a.canonical.lastClosedDate, d1Msk: a.canonical.d1Msk, lifecycle: life.state, clampedToMonth };
  lifecycleLog(a.log, wb.mode === 'MANUAL' ? 'MANUAL_OVERRIDE_ACTIVE' : 'LCD_CANDIDATE', {
    mode: wb.mode, committed, candidate, ceiling: out.ceiling, d1_msk: out.d1Msk, will_advance: candidate > committed,
    gap_at: decision.gapAt, clamped_from: clampedToMonth, lifecycle_block: life.state, notes: decision.notes,
  }, wb.mode === 'MANUAL' ? 'warn' : 'info');
  return out;
}

/* ─────────────────────────── месяц ─────────────────────────── */

export interface MonthEnsureResult {
  readonly target: string;
  readonly created: boolean;
  readonly rollbackManifestDigest: string | null;
}

/**
 * Секция месяца кандидата существует или создаётся — ДО записи данных. Идемпотентно: секция
 * уже есть → ничего не делаем (повтор после частичного сбоя не создаёт дубль). Создание —
 * один атомарный spreadsheets.batchUpdate, затем структурное перечитывание и контракт секции.
 * Провал — исключение: до коммита LCD дело не доходит.
 */
/** Больше двух новых месяцев за прогон — это не штатный переход, а долгий простой: решает владелец. */
export const MAX_MONTHS_CREATED_PER_RUN = 2;

export async function ensureWbMonthSection(a: {
  sheets: SheetsGateway; bq: UnitkaBq; config: Config; log: Logger; candidate: string; committed?: string; write: boolean; now: () => Date;
}): Promise<MonthEnsureResult> {
  // Месяцы между закоммиченным и кандидатом создаются ПО ПОРЯДКУ: у каждого предшественник уже есть.
  const last = monthKeyOf(a.candidate);
  let k = a.committed ? nextMonth(monthKeyOf(a.committed)) : last;
  const chain: MonthKey[] = [];
  for (;;) {
    if (k.year > last.year || (k.year === last.year && k.month > last.month)) break;
    chain.push(k);
    k = nextMonth(k);
  }
  if (chain.length === 0) chain.push(last);
  let out: MonthEnsureResult = { target: formatMonthKey(last), created: false, rollbackManifestDigest: null };
  let createdCount = 0;
  for (const key of chain) {
    const r = await ensureOneMonth({ ...a, target: key });
    if (r.created) {
      createdCount++;
      if (createdCount > MAX_MONTHS_CREATED_PER_RUN) {
        throw new LoaderError(`прогон создал бы больше ${MAX_MONTHS_CREATED_PER_RUN} месяцев подряд — это простой, а не переход месяца`, 'MONTH_SECTION_CHAIN_TOO_LONG');
      }
      out = r;
    }
  }
  return out.created ? out : { target: formatMonthKey(last), created: false, rollbackManifestDigest: null };
}

async function ensureOneMonth(a: {
  sheets: SheetsGateway; bq: UnitkaBq; config: Config; log: Logger; target: MonthKey; write: boolean; now: () => Date;
}): Promise<MonthEnsureResult> {
  const sheet = a.config.unitkaSheetName;
  const target = a.target;
  const meta = await a.sheets.readSheetMeta(sheet);
  const columnA = await readColumnA(a.sheets, sheet, meta.rowCount);
  const loc = locateSection(columnA, target);
  if (loc.status === 'FOUND') return { target: formatMonthKey(target), created: false, rollbackManifestDigest: null };
  if (loc.status === 'AMBIGUOUS') {
    throw new LoaderError(`секция ${formatMonthKey(target)} встречается несколько раз: ${loc.rows.join(', ')}`, 'MONTH_SECTION_AMBIGUOUS');
  }
  if (!a.write) {
    // SHADOW не создаёт структуру никогда: прежний отказ MONTH_SECTION_MISSING случится ниже
    return { target: formatMonthKey(target), created: false, rollbackManifestDigest: null };
  }
  // Создаём только в КОНЕЦ книги: генератор кладёт секцию после предшественника. Дыра в середине
  // (есть более поздний месяц, а этого нет) — не переход месяца, а повреждённая книга: её разбирает
  // прежний путь отказа (RECON_SECTION_MISSING / MONTH_SECTION_MISSING), а не достройка наугад.
  for (let later = nextMonth(target), i = 0; i < 24; later = nextMonth(later), i++) {
    if (locateSection(columnA, later).status !== 'MISSING') {
      return { target: formatMonthKey(target), created: false, rollbackManifestDigest: null };
    }
  }

  const [predecessor, population, cogsRead, structure] = await Promise.all([
    sectionSnapshot(a.sheets, sheet, columnA, meta, previousMonth(target)),
    a.bq.activeSkus(),
    a.bq.cogsCanonical().catch((e: unknown) => ({ error: e instanceof Error ? e.message : String(e) })),
    a.sheets.readSheetStructure(sheet, meta.rowCount, meta.columnCount),
  ]);
  if (!predecessor) {
    throw new LoaderError(`секции ${formatMonthKey(previousMonth(target))} нет — создавать ${formatMonthKey(target)} не от чего`, 'MONTH_PREP_NO_PREDECESSOR');
  }
  const rowFormats = await a.sheets.readRowFormats(sheet, templateRowsOf(predecessor.geometry), meta.columnCount);
  const cogs = classifyCogsSnapshot(cogsRead, a.now());
  const plan = planMonthPrep({ target, meta, columnA, predecessor, existing: null, population, cogs, structure, rowFormats });
  a.log.info('unitka_month_prep_plan', { origin: 'daily_lifecycle', sheet_id: meta.sheetId, ...planLog(plan) });
  if (plan.status !== 'PLAN_CREATE') {
    throw new LoaderError(`подготовка ${formatMonthKey(target)}: ${plan.status} ${plan.code ?? ''} — ${plan.reasons.slice(0, 5).join(' | ')}`, plan.code ?? 'MONTH_PREP_FAILED');
  }
  const requests = toStructureRequests(plan, meta.sheetId);
  const manifest = buildRollbackManifest({
    plan, meta, structure, spreadsheetId: a.config.unitkaSpreadsheetId, sheetName: sheet, requests: requests.length,
    engine: 'unitka-engine/lifecycle', gitSha: a.config.gitSha, now: a.now(),
  });
  // манифест — ДО записи: откат месяца возможен при любом исходе
  a.log.info('unitka_month_prep_rollback_manifest', { origin: 'daily_lifecycle', target: plan.target, digest: manifest.digest, manifest_b64: encodeManifest(manifest) });

  const applied = await a.sheets.structureWrite(requests);
  const meta2 = await a.sheets.readSheetMeta(sheet);
  const after = await sectionSnapshot(a.sheets, sheet, await readColumnA(a.sheets, sheet, meta2.rowCount), meta2, target);
  const v = after ? validateSection(after) : null;
  if (!v || v.sectionIssues.length || v.driftIssues.length) {
    throw new LoaderError(`после создания секция ${plan.target} не проходит контракт: ${v ? [...v.sectionIssues, ...v.driftIssues].slice(0, 8).join(' | ') : 'не найдена'} (манифест отката ${manifest.digest})`, 'MONTH_PREP_VERIFY_FAILED');
  }
  lifecycleLog(a.log, 'NEW_MONTH_CREATED', {
    target: plan.target, top_row: plan.geometry?.topRow, days: plan.geometry?.daysInMonth, blocks: plan.blocks.length,
    requests: requests.length, applied, rollback_manifest_digest: manifest.digest,
  });
  return { target: formatMonthKey(target), created: true, rollbackManifestDigest: manifest.digest };
}

/** День, следующий за закоммиченным, — для журнала «что открывает прогон». */
export const nextDay = (iso: string): string => addDaysIso(iso, 1);
