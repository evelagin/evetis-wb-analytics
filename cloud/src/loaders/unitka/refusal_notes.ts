/**
 * UNITKA WB — заметки ячейки S «Отменили товаров» о доказанных отказах вне Orders API (Phase 1A, 07.10.2026).
 *
 * S листа = отмены Orders API (следующих дней) + доказанные отказы, которых Orders API не отдал
 * (sql/unitka/refusals_v1.sql). Число в ячейке не говорит, откуда взялась его часть, поэтому каждая ячейка S
 * с отказом вне Orders API получает заметку с доказательством: srid и логистика из финотчёта.
 *
 * Владение заметкой: движок ставит и снимает ТОЛЬКО заметки со своей меткой REFUSAL_NOTE_MARKER. Чужую заметку
 * (владельца) не перезаписывает и не стирает: на такой ячейке заметка не пишется, а план отмечает конфликт.
 */
import type { FactRow } from './bq.js';
import type { ExpectedCell } from './plan.js';
import type { QaCheck } from './qa.js';
import type { SheetsGateway, StructureRequest } from './sheets.js';

export const REFUSAL_NOTE_MARKER = 'ОТКАЗ ВНЕ ORDERS API';

export interface NoteCell { row: number; col: number; nmId: number; date: string; note: string; before: string }

export const noteKey = (row: number, col: number): string => `${row}|${col}`;

const rub = (v: number): string => v.toFixed(2).replace('.', ',');

/** Текст заметки S для строки факта; '' — у строки нет отказов вне Orders API. */
export function refusalNote(f: Pick<FactRow, 'refusalCountedQty' | 'refusalProvenSrids' | 'refusalProvenLogisticsRub' | 'cancels'>): string {
  const n = f.refusalCountedQty ?? 0;
  if (n <= 0) return '';
  const srids = (f.refusalProvenSrids ?? '').split(',').filter(Boolean);
  return [
    `${REFUSAL_NOTE_MARKER}: ${n} шт. из ${f.cancels ?? n} в S`,
    'Заказ есть в воронке (Q), но Orders API его не отдал. В финотчёте у srid прямое и обратное плечо логистики, продажи и возврата нет — это отказ, а не продажа.',
    `srid: ${srids.join(', ') || '—'}`,
    ...(f.refusalProvenLogisticsRub ? [`Логистика по финотчёту: ${rub(f.refusalProvenLogisticsRub)} ₽ (в W — по ставке AF и обратной ноге, как у любой отмены)`] : []),
    'Источник: wb_mart.V_UNITKA_REFUSAL_DAILY (окончательный недельный слой финотчёта)',
  ].join('\n');
}

/**
 * План заметок S по ожидаемым ячейкам отмен. current — заметки листа (ключ noteKey). Пишется только то, что
 * отличается; снимается только собственная заметка движка. conflicts — ячейки с отказом, где стоит чужая заметка.
 */
export function planRefusalNotes(
  expected: readonly ExpectedCell[], factOf: (nmId: number, date: string) => FactRow | undefined,
  current: ReadonlyMap<string, string>,
): { notes: NoteCell[]; conflicts: NoteCell[] } {
  const notes: NoteCell[] = [], conflicts: NoteCell[] = [];
  const seen = new Set<string>();
  for (const e of expected) {
    if (e.key !== 'cancels' || e.nmId === undefined || e.date === undefined) continue;
    if (seen.has(noteKey(e.row, e.col))) continue;          // одна ячейка — одна заметка (цели могут пересекаться)
    seen.add(noteKey(e.row, e.col));
    const f = factOf(e.nmId, e.date);
    if (!f) continue;
    const want = refusalNote(f);
    const before = current.get(noteKey(e.row, e.col)) ?? '';
    if (before === want) continue;
    const ours = before === '' || before.startsWith(REFUSAL_NOTE_MARKER);
    const cell = { row: e.row, col: e.col, nmId: e.nmId, date: e.date, note: want, before };
    if (ours) notes.push(cell);
    else if (want !== '') conflicts.push(cell);
  }
  return { notes, conflicts };
}

/** repeatCell(note) по одной ячейке на заметку. */
export function noteRequests(sheetId: number, notes: readonly NoteCell[]): StructureRequest[] {
  return notes.map((n) => ({
    repeatCell: {
      range: { sheetId, startRowIndex: n.row - 1, endRowIndex: n.row, startColumnIndex: n.col - 1, endColumnIndex: n.col },
      cell: { note: n.note }, fields: 'note',
    },
  }));
}

/** Перечитывание: каждая записанная заметка равна плану. */
export function verifyRefusalNotes(planned: readonly NoteCell[], after: ReadonlyMap<string, string>): QaCheck {
  const bad = planned.filter((n) => (after.get(noteKey(n.row, n.col)) ?? '') !== n.note);
  return { name: 'REFUSAL_NOTES_READBACK', pass: bad.length === 0, count: bad.length, sample: bad.slice(0, 5).map((n) => `${n.date} ${n.nmId} r${n.row}c${n.col}`) };
}

/**
 * Заметки S месяца LCD: прочитать, спланировать, записать одним batchUpdate. Шлюз без readNotes — заметок нет.
 * Возвращает записанные (для перечитывания) и конфликты с чужими заметками.
 */
export async function applyRefusalNotes(
  sheets: SheetsGateway, sheetName: string, sheetId: number, expected: readonly ExpectedCell[],
  factOf: (nmId: number, date: string) => FactRow | undefined,
): Promise<{ notes: NoteCell[]; conflicts: NoteCell[]; applied: number }> {
  const sCells = expected.filter((e) => e.key === 'cancels');
  if (!sheets.readNotes || sCells.length === 0) return { notes: [], conflicts: [], applied: 0 };
  const planned = planRefusalNotes(sCells, factOf, await sheets.readNotes(sheetName, sCells));
  const applied = planned.notes.length ? await sheets.structureWrite(noteRequests(sheetId, planned.notes)) : 0;
  return { ...planned, applied };
}

/** Перечитать записанные заметки и сверить с планом. */
export async function readbackRefusalNotes(sheets: SheetsGateway, sheetName: string, planned: readonly NoteCell[]): Promise<QaCheck | null> {
  if (!planned.length || !sheets.readNotes) return null;
  return verifyRefusalNotes(planned, await sheets.readNotes(sheetName, planned));
}
