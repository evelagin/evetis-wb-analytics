#!/usr/bin/env node
/**
 * STEP 5B Phase D — pre-push verification. Запускать ПЕРЕД любым `clasp push`.
 *   node tools/clasp_prepush_verify.js <ingestion|unitka|operations>
 * Выходной код 0 — можно пушить; 1 — пушить нельзя.
 *
 * Проверяет ровно то, что может испортить production:
 *   1. .clasp.json существует и его scriptId СОВПАДАЕТ с ожидаемым для этого каталога;
 *   2. каталог не содержит файлов, принадлежащих другому проекту (перекрёстное заражение);
 *   3. rootDir не выходит за пределы своего каталога (push не увидит чужие .gs);
 *   4. присутствует appsscript.json (без него clasp push обрубит манифест проекта);
 *   5. файлы из neverPush не попадут в пуш (иначе они БУДУТ СОЗДАНЫ в production);
 *   6. печатает точный список файлов, которые уйдут в проект, — для глазами-проверки.
 */
const fs = require('fs'), path = require('path');
const ROOT = path.join(__dirname, '..');
const CFG = JSON.parse(fs.readFileSync(path.join(__dirname, 'clasp_projects.json'), 'utf8'));
const name = process.argv[2];
const errs = [], warns = [];

if (!name || !CFG.projects[name]) {
  console.error('Использование: node tools/clasp_prepush_verify.js <' + Object.keys(CFG.projects).join('|') + '>');
  process.exit(1);
}
const p = CFG.projects[name];
const dir = path.join(ROOT, p.dir);

// 1. scriptId
const claspPath = path.join(dir, '.clasp.json');
if (!fs.existsSync(claspPath)) {
  errs.push('нет .clasp.json в ' + p.dir);
} else {
  const c = JSON.parse(fs.readFileSync(claspPath, 'utf8'));
  if (c.scriptId !== p.scriptId) {
    errs.push('scriptId не совпадает!\n      в .clasp.json: ' + c.scriptId + '\n      ожидался:      ' + p.scriptId);
  }
  // 3. rootDir не должен выводить за пределы каталога
  const rootDir = path.resolve(dir, c.rootDir || '.');
  if (rootDir !== path.resolve(dir)) {
    errs.push('rootDir указывает вне каталога проекта: ' + rootDir);
  }
}

// 2. перекрёстное заражение: файл этого каталога не должен числиться за другим проектом
const others = Object.entries(CFG.projects).filter(([k]) => k !== name);
const here = fs.readdirSync(dir).filter(f => /\.(gs|html)$/.test(f) || f === 'appsscript.json');
for (const [k, o] of others) {
  const odir = path.join(ROOT, o.dir);
  if (!fs.existsSync(odir)) continue;
  const theirs = new Set(fs.readdirSync(odir).filter(f => /\.(gs|html)$/.test(f)));
  const clash = here.filter(f => theirs.has(f));
  if (clash.length) errs.push('файлы с теми же именами есть и в проекте ' + k + ': ' + clash.join(', '));
}

// 4. манифест
if (!here.includes('appsscript.json')) {
  (p.manifestPresent ? errs : warns).push(
    'в каталоге нет appsscript.json. clasp push без манифеста перезапишет настройки проекта ' +
    '(сервисы, scopes, часовой пояс). Сначала `clasp pull` и зафиксировать манифест в git.');
}

// 5. neverPush
const ignPath = path.join(dir, '.claspignore');
const ign = fs.existsSync(ignPath) ? fs.readFileSync(ignPath, 'utf8').split('\n').map(s => s.trim()) : [];
for (const f of (p.neverPush || [])) {
  if (!fs.existsSync(path.join(dir, f))) continue;
  if (!ign.includes(f)) errs.push('файл ' + f + ' не исключён в .claspignore — clasp СОЗДАСТ его в production');
}

// 6. что уйдёт
const willPush = here.filter(f => !(p.neverPush || []).includes(f)).sort();

console.log('Проект     : ' + name + '  (' + p.projectName + ')');
console.log('Каталог    : ' + p.dir);
console.log('Script ID  : ' + p.scriptId);
console.log('Привязка   : ' + p.binding + (p.container ? '  -> ' + p.containerName + ' (' + p.container + ')' : '  -> ' + p.containerName));
if (p.note) console.log('ВНИМАНИЕ   : ' + p.note);
console.log('К отправке : ' + willPush.length + ' файлов');
willPush.forEach(f => console.log('   ' + f));
if ((p.neverPush || []).length) console.log('Исключены  : ' + p.neverPush.join(', '));

warns.forEach(w => console.log('\nWARNING: ' + w));
if (errs.length) {
  console.error('\nPUSH ЗАПРЕЩЁН:');
  errs.forEach(e => console.error('  - ' + e));
  process.exit(1);
}
console.log('\nOK — проверки пройдены.' + (warns.length ? ' Есть предупреждения выше.' : ''));
