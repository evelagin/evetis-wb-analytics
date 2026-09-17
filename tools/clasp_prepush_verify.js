#!/usr/bin/env node
/**
 * Pre-push verification. Запускать ПЕРЕД любым `clasp push`.
 *   node tools/clasp_prepush_verify.js <ingestion|unitka|operations>
 * exit 0 — push безопасен; exit 1 — push запрещён.
 *
 * Инвариант: НЕОЖИДАННЫХ УДАЛЕНИЙ В PRODUCTION = 0.
 * Симуляция считается против снимка production (tools/clasp_production_inventory.json).
 *
 * Имя файла в проекте Apps Script = имя локального файла минус ОДНО расширение
 * .gs/.js/.html; appsscript.json -> "appsscript". Сравнение имён РЕГИСТРОЗАВИСИМОЕ:
 * Wbadsdaily и WbAdsDaily — разные файлы, и push первого удалил бы второй.
 */
const fs = require('fs'), path = require('path'), crypto = require('crypto'), cp = require('child_process');
const ROOT = path.join(__dirname, '..');
const CFG = JSON.parse(fs.readFileSync(path.join(__dirname, 'clasp_projects.json'), 'utf8'));
const INV = JSON.parse(fs.readFileSync(path.join(__dirname, 'clasp_production_inventory.json'), 'utf8'));
const name = process.argv[2];
const errs = [], warns = [];
if (!name || !CFG.projects[name]) {
  console.error('Использование: node tools/clasp_prepush_verify.js <' + Object.keys(CFG.projects).join('|') + '>');
  process.exit(1);
}
const p = CFG.projects[name], dir = path.join(ROOT, p.dir), inv = INV.projects[name];
const PUSHABLE = /\.(gs|js|html)$/;
const remoteName = f => (f === 'appsscript.json' ? 'appsscript' : f.replace(/\.(gs|js|html)$/, ''));
const sha12 = b => crypto.createHash('sha256').update(b).digest('hex').slice(0, 12);

// 1. scriptId / rootDir
const cp1 = path.join(dir, '.clasp.json');
if (!fs.existsSync(cp1)) errs.push('нет .clasp.json в ' + p.dir);
else {
  const c = JSON.parse(fs.readFileSync(cp1, 'utf8'));
  if (c.scriptId !== p.scriptId) errs.push('scriptId не совпадает: в .clasp.json ' + c.scriptId + ', ожидался ' + p.scriptId);
  if (inv && inv.scriptId !== p.scriptId) errs.push('scriptId в снимке production не совпадает с clasp_projects.json');
  if (path.resolve(dir, c.rootDir || '.') !== path.resolve(dir)) errs.push('rootDir выходит за пределы каталога проекта');
}

const all = fs.readdirSync(dir).filter(f => !f.startsWith('.'));
const invisible = all.filter(f => !PUSHABLE.test(f) && f !== 'appsscript.json');
const ignored = new Set(p.neverPush || []);
const willPush = all.filter(f => (PUSHABLE.test(f) || f === 'appsscript.json') && !ignored.has(f)).sort();

// 2. регистр на диске должен совпадать с git-индексом (macOS APFS регистронезависима)
try {
  const idx = cp.execSync('git ls-files ' + JSON.stringify(p.dir), { cwd: ROOT }).toString()
    .trim().split('\n').filter(Boolean).map(s => s.split('/').pop());
  const disk = new Set(all);
  const mismatch = idx.filter(f => !f.startsWith('.') && !disk.has(f) && [...disk].some(d => d.toLowerCase() === f.toLowerCase()));
  if (mismatch.length) errs.push('регистр имён на диске не совпадает с git-индексом (' + mismatch.length +
    '). clasp читает диск — push создаст дубли и удалит оригиналы:\n      ' + mismatch.join(', '));
} catch (e) { warns.push('не удалось сверить git-индекс: ' + e.message); }

// 3. пересечение имён с другим проектом (кроме задокументированных)
const allowed = new Set((CFG.crossProjectDuplicateNames || []).map(x => x.file));
for (const [k, o] of Object.entries(CFG.projects)) {
  if (k === name) continue;
  const od = path.join(ROOT, o.dir);
  if (!fs.existsSync(od)) continue;
  const theirs = new Set(fs.readdirSync(od).filter(f => PUSHABLE.test(f)));
  const clash = willPush.filter(f => theirs.has(f) && !allowed.has(f));
  if (clash.length) errs.push('незадокументированное пересечение имён с проектом ' + k + ': ' + clash.join(', '));
}

// 4. манифест
if (!all.includes('appsscript.json')) errs.push('нет appsscript.json — push перепишет манифест проекта');

// 5. neverPush должны быть в .claspignore
const ign = fs.existsSync(path.join(dir, '.claspignore'))
  ? fs.readFileSync(path.join(dir, '.claspignore'), 'utf8').split('\n').map(s => s.trim()) : [];
const allowlist = ign.filter(l => l.startsWith('!')).map(l => l.slice(1));
for (const f of ignored) {
  const covered = ign.includes(f) || (allowlist.length && !allowlist.includes(f));
  if (fs.existsSync(path.join(dir, f)) && !covered) errs.push('файл ' + f + ' не исключён в .claspignore — clasp СОЗДАСТ его в production');
}

// 6. файлы, которые clasp не отправит
if (invisible.length) errs.push('файлов без расширения .gs/.html: ' + invisible.length +
  '. clasp их не отправит, а production-двойников удалит:\n      ' + invisible.join(', '));

// 7. симуляция
let added = [], modified = [], deleted = [];
if (!inv) warns.push('нет снимка production — симуляция удалений невозможна');
else {
  const local = new Map(willPush.map(f => [remoteName(f), f]));
  for (const [rn, f] of local) {
    if (!(rn in inv.files)) { added.push(f); continue; }
    if (sha12(fs.readFileSync(path.join(dir, f))) !== inv.files[rn]) modified.push(f);
  }
  for (const rn of Object.keys(inv.files)) if (!local.has(rn)) deleted.push(rn);
  added.sort(); modified.sort(); deleted.sort();
  if (deleted.length) errs.push('push УДАЛИЛ БЫ из production ' + deleted.length + ' файл(ов). Допустимо 0:\n      ' + deleted.join(', '));
}

console.log('Проект     : ' + name + '  (' + p.projectName + ')');
console.log('Script ID  : ' + p.scriptId);
if (p.buildCommand) console.log('Сборка     : ' + p.buildCommand);
console.log('Снимок prod: ' + (inv ? Object.keys(inv.files).length + ' файлов (' + INV.capturedAt + ')' : 'нет'));
console.log('Отправится : ' + willPush.length + ' файлов');
console.log('  ADDED    : ' + (added.length ? added.length + '  ' + added.join(', ') : '0'));
console.log('  MODIFIED : ' + (modified.length ? modified.length + '  ' + modified.join(', ') : '0'));
console.log('  DELETED  : ' + (deleted.length ? deleted.length + '  ' + deleted.join(', ') : '0'));
if (ignored.size) console.log('  не пушится: ' + [...ignored].join(', '));
warns.forEach(w => console.log('\nWARNING: ' + w));
if (errs.length) { console.error('\nPUSH ЗАПРЕЩЁН:'); errs.forEach(e => console.error('  - ' + e)); process.exit(1); }
console.log('\nOK — push безопасен.' + (warns.length ? ' Есть предупреждения выше.' : ''));
