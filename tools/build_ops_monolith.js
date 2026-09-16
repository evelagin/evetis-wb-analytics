#!/usr/bin/env node
/**
 * OPERATIONS · reproducible build: Ops*.gs -> Код.gs
 *
 * В production проект «EVETIS OPERATIONS · C2» устроен как ОДИН файл `Код.gs` —
 * конкатенация пяти модулей в фиксированном порядке с строками-маркерами.
 * Источник правды в Git — модули; `Код.gs`生成 этим скриптом и коммитится,
 * чтобы первый clasp push не менял структуру проекта.
 *
 *   node tools/build_ops_monolith.js         # собрать и записать
 *   node tools/build_ops_monolith.js --check # только проверить, что файл актуален (exit 1 если нет)
 *
 * Формат зафиксирован подбором под байты production (sha256 0280a3c3f5ee...):
 *   заголовок, пустая строка, затем для каждого модуля: маркер, содержимое, '\n' между модулями.
 * Заголовок намеренно сохраняет старый путь apps-script/evetis_operations — так он
 * выглядит в production. Менять его следует отдельным controlled deployment, иначе
 * первый push перестанет быть no-op.
 */
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const DIR = path.join(__dirname, '..', 'apps-script', 'operations');
const ORDER = ['OpsConfig.gs', 'OpsCore.gs', 'OpsSheetIO.gs', 'OpsInstall.gs', 'OpsMain.gs'];
const BAR = '═'.repeat(11);
const HEADER = '// EVETIS OPERATIONS · C2 — сборка из apps-script/evetis_operations: ' + ORDER.join(', ') + ' (порядок важен)';
const PROD_SHA = '0280a3c3f5ee0f114e60af65a2c66ec1aae9e08d4decbba7e9ac2a9d15d3ad8c';

const parts = [HEADER + '\n\n'];
ORDER.forEach((f, i) => {
  const body = fs.readFileSync(path.join(DIR, f), 'utf8');
  parts.push('// ' + BAR + ' ' + f + ' ' + BAR + '\n' + body + (i < ORDER.length - 1 ? '\n' : ''));
});
const out = parts.join('');
const sha = crypto.createHash('sha256').update(out).digest('hex');
const target = path.join(DIR, 'Код.gs');
const check = process.argv.includes('--check');

if (check) {
  const cur = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : '';
  if (cur !== out) { console.error('Код.gs устарел — перезапустите: node tools/build_ops_monolith.js'); process.exit(1); }
  console.log('Код.gs актуален. sha256 ' + sha.slice(0, 12) + (sha === PROD_SHA ? '  == production' : '  != production (' + PROD_SHA.slice(0, 12) + ')'));
} else {
  fs.writeFileSync(target, out);
  console.log('Код.gs собран: ' + Buffer.byteLength(out) + ' байт, sha256 ' + sha.slice(0, 12) +
    (sha === PROD_SHA ? '  == production (push будет no-op)' : '  != production'));
}
