// VISUAL PARITY rehearsal harness. Uses the compiled CANONICAL formatter of a given checkout.
//   node harness.mjs <distRoot> plan    <spreadsheet> <sheetName>   -> migration_plan.json (no writes)
//   node harness.mjs <distRoot> snapshot <spreadsheet> <sheetName>  -> rollback_plan.json from the LIVE sheet (no writes);
//        run it right BEFORE apply: it restores exactly the pre-migration borders and row heights (format only)
//   node harness.mjs <distRoot> apply   <spreadsheet> <sheetName> [plan.json]  -> sends the plan (TEST only;
//        default migration_plan.json, rollback_plan.json for a format-only rollback)
//   node harness.mjs <distRoot> writer  <spreadsheet> <sheetName>   -> runs ozonUnitkaLoader (TEST only)
import fs from 'fs';
import { execFileSync } from 'child_process';
import { OAuth2Client } from 'google-auth-library';

const [,, distRoot, mode, spreadsheet, sheetName] = process.argv;
const TEST = '1HTgd__BMvvuDsS02HdiGtkpyrzWqeX5pp9TrN6k8ftY';
const PROD = '1E4L4JuwfEqr9owhsGkAjb8F24lRpWpWkEyVmSuRxaJg';
const SA = 'sa-unitka-sheet-rehearsal@project-fa311fc0-4d87-4781-986.iam.gserviceaccount.com';
const planFile = process.argv[6] ?? 'migration_plan.json';
if ((mode === 'apply' || mode === 'writer') && spreadsheet !== TEST) throw new Error('writes allowed ONLY into TEST');
if (![TEST, PROD].includes(spreadsheet)) throw new Error('unknown spreadsheet');

const imp = (p) => import(`${distRoot}/${p}`);
const { SheetsRest } = await imp('loaders/unitka/sheets.js');
const { rowHeightRequests, layoutOf, columnName } = await imp('loaders/unitka/ozon/requests.js');
const { staticFormatRequests } = await imp('loaders/unitka/ozon/structure.js');
const { deriveOzonGeometry } = await imp('loaders/unitka/ozon/ozon_lifecycle.js');
const { parseLiveLayout, ozonUnitkaLoader, defaultOzonUnitkaDeps } = await imp('loaders/unitka/ozon/loader.js');
const { ozonMonthSpec } = await imp('loaders/unitka/ozon/month.js');
const { loadConfig } = await imp('config.js');
const { Logger } = await imp('logging.js');

function authFor(readonly) {
  const scope = 'https://www.googleapis.com/auth/spreadsheets' + (readonly ? '.readonly' : '');
  const tok = execFileSync('gcloud', ['auth', 'print-access-token', `--impersonate-service-account=${SA}`, `--scopes=${scope}`],
    { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
  const c = new OAuth2Client(); c.setCredentials({ access_token: tok });
  return { getClient: async () => c };
}

async function liveLayouts(sheets) {
  const meta = await sheets.readSheetMeta(sheetName, false);
  const q = `'${sheetName.replace(/'/g, "''")}'`;
  const grid = (await sheets.readValues([`${q}!A1:${columnName(meta.columnCount)}${meta.rowCount}`]))[0] ?? [];
  const geo = deriveOzonGeometry({ grid, columnCount: meta.columnCount, mirror: meta.namedRanges?.OZON_LCD_MIRROR ?? null, envTailFirst: 562 });
  // Слоты считаются по НЕПУСТОЙ подписи блока: для оформления важно число физических блоков секции.
  const { sections } = parseLiveLayout(grid, geo.tailFirst, (t) => (/^\d+$/.test(t) ? t : null));
  const layouts = sections.map((s, i) => {
    const y = Number(s.monthKey.slice(0, 4)); const mo = Number(s.monthKey.slice(5, 7));
    const prev = sections[i - 1]; const prevDays = prev ? prev.days.length : 0;
    return { key: s.monthKey, blocks: s.blocks.length, layout: layoutOf(ozonMonthSpec(y, mo, s.titleRow - prevDays - 4, prevDays, s.blocks)) };
  });
  return { meta, geo, layouts };
}

// Границы канонических секций — из аудита: май–сентябрь 2026 оформлены каноническим форматтером
// (их достаёт глубокое окно 120 суток), 2025 – апрель 2026 — легаси и НЕ перекрашиваются.
const CANONICAL_FROM = '2026-05';

if (mode === 'plan' || mode === 'apply') {
  const sheets = new SheetsRest(spreadsheet, mode === 'plan', authFor(mode === 'plan'));
  if (mode === 'plan') {
    const { meta, geo, layouts } = await liveLayouts(sheets);
    const all = layouts.map((x) => x.layout);
    const canon = layouts.filter((x) => x.key >= CANONICAL_FROM);
    const heights = rowHeightRequests(meta.sheetId, all);
    // рамки — ТОЛЬКО свойство borders из канонического статического формата, остальное не трогаем
    const borders = staticFormatRequests(meta.sheetId, canon.map((x) => x.layout)).map((r) => {
      const f = r.repeatCell.cell.userEnteredFormat;
      return f.borders ? { repeatCell: { range: r.repeatCell.range, cell: { userEnteredFormat: { borders: f.borders } },
        fields: 'userEnteredFormat.borders' } } : null;
    }).filter(Boolean);
    const plan = { spreadsheet, sheetName, sheetId: meta.sheetId, tailFirst: geo.tailFirst,
      sections: layouts.map((x) => ({ key: x.key, blocks: x.blocks, ...x.layout })), canonical: canon.map((x) => x.key),
      requests: [...heights, ...borders], counts: { rowHeights: heights.length, borders: borders.length } };
    fs.writeFileSync('migration_plan.json', JSON.stringify(plan));
    console.log(JSON.stringify({ sections: plan.sections.length, canonical: plan.canonical, counts: plan.counts }));
  } else {
    const plan = JSON.parse(fs.readFileSync(planFile, 'utf8'));
    if (plan.spreadsheet !== TEST || plan.sheetName !== sheetName) throw new Error('plan is not for this TEST sheet');
    let n = 0;
    for (let i = 0; i < plan.requests.length; i += 400) n += await sheets.structureWrite(plan.requests.slice(i, i + 400));
    console.log(JSON.stringify({ applied: plan.requests.length, replies: n }));
  }
}

if (mode === 'snapshot') {
  // Откат ТОЛЬКО оформления: для каждой строки/колонки, которых касается migration_plan.json, запоминается
  // живое состояние. Рамки берутся с запасом в одну строку и одну колонку вокруг каждого диапазона:
  // Sheets переносит общее ребро на соседнюю ячейку. Значения, формулы, УФ, объединения не читаются и не пишутся.
  const plan = JSON.parse(fs.readFileSync('migration_plan.json', 'utf8'));
  if (plan.spreadsheet !== spreadsheet || plan.sheetName !== sheetName) throw new Error('migration plan is for another sheet');
  const client = await authFor(true).getClient();
  const rows = new Set(); let r0 = Infinity, r1 = 0, c1 = 0;
  for (const rq of plan.requests) {
    if (rq.updateDimensionProperties) {
      const g = rq.updateDimensionProperties.range; for (let i = g.startIndex; i < g.endIndex; i++) rows.add(i);
    } else if (rq.repeatCell) {
      const g = rq.repeatCell.range; r0 = Math.min(r0, g.startRowIndex); r1 = Math.max(r1, g.endRowIndex); c1 = Math.max(c1, g.endColumnIndex);
    }
  }
  r0 = Math.max(0, r0 - 1); r1 += 1; c1 = Math.min(plan.tailFirst - 1, c1 + 1);   // хвост владельца не трогаем никогда
  const q = `'${sheetName.replace(/'/g, "''")}'`;
  const url = (range, fields) => `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheet}?ranges=${encodeURIComponent(range)}&includeGridData=true&fields=${encodeURIComponent(fields)}`;
  const meta = (await client.request({ url: url(`${q}!A1:A${Math.max(...rows) + 1}`, 'sheets(data(rowMetadata(pixelSize)))') })).data;
  const rm = meta.sheets[0].data[0].rowMetadata;
  const out = [];
  const sorted = [...rows].sort((a, b) => a - b);
  for (let i = 0; i < sorted.length;) {
    let j = i; const px = rm[sorted[i]].pixelSize;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1 && rm[sorted[j + 1]].pixelSize === px) j++;
    out.push({ updateDimensionProperties: { range: { sheetId: plan.sheetId, dimension: 'ROWS', startIndex: sorted[i], endIndex: sorted[j] + 1 },
      properties: { pixelSize: px }, fields: 'pixelSize' } });
    i = j + 1;
  }
  for (let a = r0; a < r1; a += 40) {
    const b = Math.min(r1, a + 40);
    const d = (await client.request({ url: url(`${q}!A${a + 1}:${columnName(c1)}${b}`, 'sheets(data(rowData(values(userEnteredFormat(borders)))))') })).data;
    const rd = d.sheets[0].data[0].rowData ?? [];
    for (let k = 0; k < b - a; k++) {
      const vals = rd[k]?.values ?? [];
      const cells = [];
      for (let c = 0; c < c1; c++) {
        const br = vals[c]?.userEnteredFormat?.borders ?? {};
        const clean = Object.fromEntries(Object.entries(br).map(([s, v]) => [s, { style: v.style, ...(v.colorStyle ? { colorStyle: v.colorStyle } : {}) }]));
        cells.push({ userEnteredFormat: { borders: clean } });
      }
      out.push({ updateCells: { range: { sheetId: plan.sheetId, startRowIndex: a + k, endRowIndex: a + k + 1, startColumnIndex: 0, endColumnIndex: c1 },
        rows: [{ values: cells }], fields: 'userEnteredFormat.borders' } });
    }
  }
  const rb = { spreadsheet, sheetName, sheetId: plan.sheetId, tailFirst: plan.tailFirst, takenAt: new Date().toISOString(),
    scope: { rows: [r0 + 1, r1], columns: [1, c1], rowHeights: rows.size }, requests: out };
  fs.writeFileSync('rollback_plan.json', JSON.stringify(rb));
  console.log(JSON.stringify({ rollbackRequests: out.length, scope: rb.scope }));
}

if (mode === 'writer') {
  const env = {
    ENVIRONMENT: 'prod', GCP_PROJECT_ID: 'project-fa311fc0-4d87-4781-986', BQ_LOCATION: 'EU', BQ_RAW_DATASET: 'wb_raw',
    BQ_MANIFEST_TABLE: 'LOADER_RUNS', LOG_LEVEL: 'info', LOADER_NAME: 'ozon-unitka',
    UNITKA_SPREADSHEET_ID: TEST, OZON_UNITKA_SHEET_NAME: sheetName, OZON_UNITKA_WRITE_ENABLED: '1',
    OZON_UNITKA_LCD_CELL: 'OZON_LAST_CLOSED_DATE', OZON_UNITKA_LCD_REF: '$VA$2', OZON_UNITKA_TAIL_FIRST_COLUMN: '562',
    OZON_UNITKA_BLOCK_SLOTS: '22', OZON_UNITKA_EXISTING_CF_RULES: '434', OZON_UNITKA_MAX_SOURCE_LAG_DAYS: '1',
    OZON_UNITKA_OFFER_ALIASES: '{"9099514444":"909951444"}',
  };
  const config = loadConfig(env);
  if (config.unitkaSpreadsheetId !== TEST) throw new Error('config does not point to TEST');
  const ctx = { config, logger: new Logger({ harness: 'visual-parity' }, 'info'), logicalPeriod: '', runId: 'visual-parity', targetDate: '' };
  const deps = { ...defaultOzonUnitkaDeps, makeSheets: (_c, ro) => new SheetsRest(TEST, ro, authFor(ro)) };
  const r = await ozonUnitkaLoader(ctx, deps);
  console.log(JSON.stringify({ result: r }));
}
