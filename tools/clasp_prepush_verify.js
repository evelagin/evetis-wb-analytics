#!/usr/bin/env node
/**
 * STEP 5C — pre-push verification. Запускать ПЕРЕД любым `clasp push`.
 *   node tools/clasp_prepush_verify.js <ingestion|unitka|operations>
 * exit 0 — push безопасен; exit 1 — push запрещён.
 *
 * Главный инвариант: НЕОЖИДАННЫХ УДАЛЕНИЙ В PRODUCTION = 0.
 * Симуляция считается против снимка production (tools/clasp_production_inventory.json).
 *
 * Проверки:
 *   1. .clasp.json есть, scriptId совпадает с ожидаемым, rootDir не выходит за каталог;
 *   2. нет пересечения имён файлов с другим проектом;
 *   3. есть appsscript.json (иначе push перепишет манифест проекта);
 *   4. файлы из neverPush исключены в .claspignore;
 *   5. НЕТ файлов без расширения — clasp их не отправит, а их production-двойники УДАЛИТ;
 *   6. симуляция added / modified / deleted против снимка production; любое deleted = отказ.
 */
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const ROOT = path.join(__dirname, '..');
const CFG = JSON.parse(fs.readFileSync(path.join(__dirname, 'clasp_projects.json'), 'utf8'));
const INV = JSON.parse(fs.readFileSync(path.join(__dirname, 'clasp_production_inventory.json'), 'utf8'));
const name = process.argv[2];
const errs = [], warns = [];

if (!name || !CFG.projects[name]) {
  console.error('Использование: node tools/clasp_prepush_verify.js <' + Object.keys(CFG.projects).join('|') + '>');
  process.exit(1);
}
const p = CFG.projects[name];
const dir = path.join(ROOT, p.dir);
const inv = INV.projects[name];
const PUSHABLE = /\.(gs|js|html)$/i;
const sha12 = b => crypto.createHash('sha256').update(b).digest('hex').slice(0, 12);
const key = f => f.replace(/\.(gs|js|html|json)$/i, '').toLowerCase();

// 1. scriptId / rootDir
const claspPath = path.join(dir, '.clasp.json');
if (!fs.existsSync(claspPath)) errs.push('нет .clasp.json в ' + p.dir);
else {
  const c = JSON.parse(fs.readFileSync(claspPath, 'utf8'));
  if (c.scriptId !== p.scriptId) errs.push('scriptId не совпадает!\n      в .clasp.json: ' + c.scriptId + '\n      ожидался:      ' + p.scriptId);
  if (inv && inv.scriptId !== p.scriptId) errs.push('scriptId в снимке production не совпадает с clasp_projects.json');
  if (path.resolve(dir, c.rootDir || '.') !== path.resolve(dir)) errs.push('rootDir выходит за пределы каталога проекта');
}

const all = fs.readdirSync(dir).filter(f => !f.startsWith('.'));
const pushable = all.filter(f => PUSHABLE.test(f) || f === 'appsscript.json');
const invisible = all.filter(f => !PUSHABLE.test(f) && f !== 'appsscript.json');

// 2. пересечение имён с другим проектом
for (const [k, o] of Object.entries(CFG.projects)) {
  if (k === name) continue;
  const od = path.join(ROOT, o.dir);
  if (!fs.existsSync(od)) continue;
  const theirs = new Set(fs.readdirSync(od).filter(f => PUSHABLE.test(f)));
  const clash = pushable.filter(f => theirs.has(f));
  if (clash.length) errs.push('имена файлов пересекаются с проектом ' + k + ': ' + clash.join(', '));
}

// 3. манифест
if (!all.includes('appsscript.json')) errs.push('нет appsscript.json — push перепишет манифест проекта (scopes, сервисы, timezone)');

// 4. neverPush
const ign = fs.existsSync(path.join(dir, '.claspignore'))
  ? fs.readFileSync(path.join(dir, '.claspignore'), 'utf8').split('\n').map(s => s.trim()) : [];
for (const f of (p.neverPush || [])) {
  if (fs.existsSync(path.join(dir, f)) && !ign.includes(f)) {
    errs.push('файл ' + f + ' не исключён в .claspignore — clasp СОЗДАСТ его в production');
  }
}

// 5. файлы без расширения
if (invisible.length) {
  errs.push('файлов без расширения .gs/.html: ' + invisible.length +
    '. clasp их НЕ отправит, а их production-двойники УДАЛИТ. Переименовать в *.gs до push:\n      ' +
    invisible.join(', '));
}

// 6. симуляция против production
const willPush = pushable.filter(f => !(p.neverPush || []).includes(f));
let added = [], modified = [], deleted = [];
if (!inv) warns.push('нет снимка production для ' + name + ' — симуляция удалений невозможна');
else {
  const local = new Map(willPush.map(f => [key(f), f]));
  const remote = new Map(Object.keys(inv.files).map(f => [key(f), f]));
  for (const [k, f] of local) {
    if (!remote.has(k)) { added.push(f); continue; }
    const h = sha12(fs.readFileSync(path.join(dir, f)));
    if (h !== inv.files[remote.get(k)]) modified.push(f);
  }
  for (const [k, f] of remote) if (!local.has(k)) deleted.push(f);
  added.sort(); modified.sort(); deleted.sort();
  if (deleted.length) {
    errs.push('push УДАЛИЛ БЫ из production ' + deleted.length + ' файл(ов). Допустимо 0:\n      ' + deleted.join(', '));
  }
}

console.log('Проект     : ' + name + '  (' + p.projectName + ')');
console.log('Каталог    : ' + p.dir);
console.log('Script ID  : ' + p.scriptId);
console.log('Привязка   : ' + p.binding);
if (p.note) console.log('ВНИМАНИЕ   : ' + p.note);
console.log('');
console.log('Снимок production : ' + (inv ? Object.keys(inv.files).length + ' файлов (' + INV.capturedAt + ')' : 'нет'));
console.log('Будет отправлено  : ' + willPush.length + ' файлов');
console.log('  ADDED    : ' + (added.length ? added.length + '  ' + added.join(', ') : '0'));
console.log('  MODIFIED : ' + (modified.length ? modified.length + '  ' + modified.join(', ') : '0'));
console.log('  DELETED  : ' + (deleted.length ? deleted.length + '  ' + deleted.join(', ') : '0'));
if ((p.neverPush || []).length) console.log('  исключены: ' + p.neverPush.join(', '));

warns.forEach(w => console.log('\nWARNING: ' + w));
if (errs.length) {
  console.error('\nPUSH ЗАПРЕЩЁН:');
  errs.forEach(e => console.error('  - ' + e));
  process.exit(1);
}
console.log('\nOK — push безопасен.' + (warns.length ? ' Есть предупреждения выше.' : ''));
