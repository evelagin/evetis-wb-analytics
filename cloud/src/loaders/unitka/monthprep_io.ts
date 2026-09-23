/**
 * Общие помощники подготовки месяца — вынесены из prep.ts, чтобы суточный писатель мог
 * использовать ТОТ ЖЕ код без цикла импорта index → wb_lifecycle → prep → index
 * (prep.ts читает ENGINE_VERSION при загрузке модуля и в цикле упал бы в TDZ).
 */
import { geometryAt, locateSection, type MonthKey } from './calendar.js';
import { readSnapshot } from './section.js';
import type { MonthPrepPlan } from './monthprep.js';
import type { Snapshot } from './plan.js';
import type { SheetsGateway, SheetMeta } from './sheets.js';

export async function sectionSnapshot(sheets: SheetsGateway, sheetName: string, columnA: readonly unknown[], meta: SheetMeta, key: MonthKey): Promise<Snapshot | null> {
  const loc = locateSection(columnA, key);
  if (loc.status !== 'FOUND') return null;
  const g = geometryAt(key, loc.topRow);
  if (g.spacerRow > meta.rowCount) return null;
  return readSnapshot(sheets, sheetName, g, meta.columnCount, meta.anchorCol);
}

export function planLog(plan: MonthPrepPlan): Record<string, unknown> {
  return {
    status: plan.status, code: plan.code, target: plan.target, reasons: plan.reasons.slice(0, 10),
    section: plan.geometry ? { top: plan.geometry.topRow, first: plan.geometry.firstDailyRow, last: plan.geometry.lastDailyRow, mtd: plan.geometry.mtdRow, spacer: plan.geometry.spacerRow, days: plan.geometry.daysInMonth } : null,
    predecessor: plan.predecessor,
    blocks: plan.blocks.map((b) => ({ slot: b.slot, nm_id: b.nmId, origin: b.origin, cogs: b.params.cogsTerm })),
    // Семейство формулы остатка MTD, перенесённое из месяца-источника (production: native; копия книги: wrapped).
    mtd_stock_family: plan.blocks.reduce<Record<string, number>>((m, b) => { m[b.params.mtdStockFamily] = (m[b.params.mtdStockFamily] ?? 0) + 1; return m; }, {}),
    retired: plan.retiredNmIds, unmapped_active: plan.unmappedActive,
    append_rows: plan.appendRows, insert_columns: plan.insertColumns, anchor_col: plan.anchorCol, chain_gaps: plan.chainGaps, cf_trims: plan.cfTrims.length, width_upgrades: plan.widthUpgrades, row_heights: plan.rowHeights, tail_group_detached: plan.tailGroupDetachedColumn, groups: plan.groupRequests.length,
    cells: plan.cells.length, format_copies: plan.formatCopies.length, merges: plan.merges.length,
    cf: plan.conditionalFormats ? { rules: plan.conditionalFormats.rules.length, start_index: plan.cfStartIndex, families: plan.conditionalFormats.families } : null,
    dimension_requests: plan.dimensionRequests.length, formula_style: plan.formulaStyle,
    manual_blank_cells: plan.manualBlankCells, cogs_provenance: plan.cogsProvenance,
  };
}
