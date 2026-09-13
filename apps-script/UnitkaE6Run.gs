/**
 * UNITKA E6-A — диспетчер (единственная публичная функция файла — e6()).
 * Нужен потому, что выпадающий список функций в редакторе Apps Script не переключается автоматизацией
 * (13.09.2026: выбор «e6snapshot» в списке привёл к повторному запуску e6backup).
 * Каждый вызов выполняет РОВНО ОДНУ стадию по состоянию ScriptProperties и останавливается — журнал
 * проверяется между стадиями. Порядок:
 *   backup (E6_BACKUP_ID) → snapshot ч.1 (E6_SNAPSHOT_MAIN_AT) → snapshot ч.2 (E6_SNAPSHOT_AT) → dryrun (E6_DRYRUN_AT)
 *   — контрольный этап, PASS 13.09 21:34 МСК —
 *   cfBake (E6_CF_BAKED_DONE) → cfRules (E6_CF_DONE) → freeze (E6_FREEZE_DONE) → fix (E6_FIX_DONE) → trim (E6_TRIM_DONE)
 *   → qa1 (E6_QA1_AT) → qa2 (E6_QA_DONE) → STOP.
 * Write-фаза разрешена владельцем 13.09.2026 22:10 МСК («E6-A WRITE PHASE APPROVED»), Engine prod на паузе.
 * НИКОГДА не вызывает e6rollback, Engine, scheduler — только по решению владельца.
 */
var E6_WRITE_PHASE_APPROVED = '2026-09-13 22:10 МСК — владелец: E6-A WRITE PHASE APPROVED (unitka-engine-prod PAUSED)';

function e6() {
  var p = PropertiesService.getScriptProperties();
  if (!p.getProperty('E6_BACKUP_ID')) { e6backup(); return 'backup'; }
  if (!p.getProperty('E6_SNAPSHOT_MAIN_AT')) { e6snapshot(); return 'snapshot'; }
  if (!p.getProperty('E6_SNAPSHOT_AT')) { e6snapshotStatic(); return 'snapshotStatic'; }
  if (!p.getProperty('E6_DRYRUN_AT')) { e6dryrun(); p.setProperty('E6_DRYRUN_AT', new Date().toISOString()); return 'dryrun'; }
  if (!E6_WRITE_PHASE_APPROVED) throw new Error('E6-A: write-фаза не разрешена — STOP');
  if (!p.getProperty('E6_CF_BAKED_DONE')) { e6cfBake(); return p.getProperty('E6_CF_BAKED_DONE') ? 'cfBake (готово)' : 'cfBake (частично — повторить запуск)'; }
  if (!p.getProperty('E6_CF_DONE')) { e6cfRules(); return 'cfRules'; }
  if (!p.getProperty('E6_FREEZE_DONE')) { e6freeze(); return 'freeze'; }
  if (!p.getProperty('E6_FIX_DONE')) { e6fix(); return 'fix'; }
  if (!p.getProperty('E6_TRIM_DONE')) { e6trim(); return 'trim'; }
  if (!p.getProperty('E6_QA1_AT')) { e6qa1(); return 'qa1'; }
  if (!p.getProperty('E6_QA_DONE')) { e6qa2(); return 'qa2 — STOP'; }
  throw new Error('E6-A завершён (QA сделан). STOP — дальнейшие шаги (Engine SHADOW/resume, E6-B) только по решению владельца.');
}
