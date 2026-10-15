// D4: перегенерация src/loaders/unitka/ozonstorepnl/qa.ts из канонического QA D3 (sql/ozon/qa_ozon_store_pnl_v1.sql).
// Запуск из cloud/: node scripts/gen_ozon_store_pnl_qa.mjs — тест требует побайтного равенства встроенной копии с каноном.
import { readFileSync, writeFileSync } from 'node:fs';
const sql = readFileSync(new URL('../../sql/ozon/qa_ozon_store_pnl_v1.sql', import.meta.url), 'utf8');
const ts = `/**
 * D4 — копия канонического QA D3 (sql/ozon/qa_ozon_store_pnl_v1.sql) для гейта публикации. Образ собирается из cloud/,
 * поэтому файл вшит строкой; тест cloud/test/ozon_store_pnl.test.ts требует побайтного равенства с каноном.
 * НЕ ПРАВИТЬ РУКАМИ: node scripts/gen_ozon_store_pnl_qa.mjs (из cloud/).
 */
// prettier-ignore
export const QA_SQL: string = ${JSON.stringify(sql)};

export interface QaCheck { id: string; sql: string }

/** Блоки «-- @check <ID>» → по одному запросу (единый запрос по всем вью упирается в «query is too complex»). */
export function qaChecks(text: string = QA_SQL): QaCheck[] {
  return text.split('-- @check ').slice(1).filter((b) => !b.startsWith('<')).map((b) => {
    const nl = b.indexOf('\\n');
    return { id: b.slice(0, nl).trim(), sql: b.slice(nl + 1).trim().replace(/;\\s*$/, '') };
  });
}
`;
writeFileSync(new URL('../src/loaders/unitka/ozonstorepnl/qa.ts', import.meta.url), ts);
