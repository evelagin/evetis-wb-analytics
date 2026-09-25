// Добавляет в CANONICAL_CURRENT_WB_PRESENTATION_CONTRACT цвета рамок, снятые с живого WB
// (Сентябрь 2026). Стили рамок НЕ меняются: скрипт падает, если сериализация без цветов
// не воспроизводит файл побайтно.
import fs from 'fs';
const F = '/Users/evgenelagin/Projects/evetis-wb-analytics-ozon-visual/cloud/src/loaders/unitka/ozon/wbcontract.ts';
const src = fs.readFileSync(F, 'utf8');
const i = src.indexOf('PresentationContract =') ; const j = src.indexOf('{', i);
let d = 0, k = j; for (; k < src.length; k++) { if (src[k] == '{') d++; else if (src[k] == '}') { d--; if (!d) break; } }
const slice = src.slice(j, k + 1);
const C = JSON.parse(slice);
if (JSON.stringify(C, null, 2) !== slice) throw new Error('формат файла не JSON.stringify(…,2) — генератор не применим');
const cols = JSON.parse(fs.readFileSync('derived_border_colours.json', 'utf8'));
const BLACK = new Set(['#000000', 'theme:TEXT']);
let added = 0;
for (const kind of ['title', 'header', 'day', 'mtd']) for (const scope of ['summary', 'block']) {
  for (const [role, spec] of Object.entries(C.rows[kind][scope])) {
    const c = cols[`${kind}|${scope}|${role}`] ?? {};
    const nb = {};
    for (const side of ['top', 'bottom', 'left', 'right']) {
      if (!spec.borders?.[side]) continue;
      const v = c[side];
      if (v === undefined || v === null) throw new Error(`нет цвета ${kind}/${scope}/${role}/${side}`);
      if (!BLACK.has(v)) nb[side] = v;
    }
    if (Object.keys(nb).length) {
      // borderColors встаёт сразу после borders — порядок ключей сохраняет читаемость диффа
      const out = {};
      for (const [kk, vv] of Object.entries(spec)) { out[kk] = vv; if (kk === 'borders') out.borderColors = nb; }
      C.rows[kind][scope][role] = out; added += Object.keys(nb).length;
    }
  }
}
fs.writeFileSync(F, src.slice(0, j) + JSON.stringify(C, null, 2) + src.slice(k + 1));
console.log('border sides coloured:', added);
