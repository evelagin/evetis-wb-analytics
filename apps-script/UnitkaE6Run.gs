/**
 * UNITKA E6-A — диспетчер контрольного этапа (единственная публичная функция файла — e6()).
 * Нужен потому, что выпадающий список функций в редакторе Apps Script не переключается автоматизацией
 * (13.09.2026: выбор «e6snapshot» в списке привёл к повторному запуску e6backup).
 * Порядок по состоянию ScriptProperties: backup (E6_BACKUP_ID) → snapshot ч.1 (E6_SNAPSHOT_MAIN_AT) → snapshot ч.2 статические фоны (E6_SNAPSHOT_AT) → dryrun (E6_DRYRUN_AT) → STOP.
 * НИКОГДА не вызывает e6cf / e6freeze / e6fix / e6trim / e6qa / e6rollback — они запускаются только вручную по решению владельца.
 */
function e6() {
  var p = PropertiesService.getScriptProperties();
  if (!p.getProperty('E6_BACKUP_ID')) { e6backup(); return 'backup'; }
  if (!p.getProperty('E6_SNAPSHOT_MAIN_AT')) { e6snapshot(); return 'snapshot'; }
  if (!p.getProperty('E6_SNAPSHOT_AT')) { e6snapshotStatic(); return 'snapshotStatic'; }
  if (!p.getProperty('E6_DRYRUN_AT')) { e6dryrun(); p.setProperty('E6_DRYRUN_AT', new Date().toISOString()); return 'dryrun'; }
  // контрольный этап завершён: повторный вызов — только повторный dry-run (read-only), дальнейшие шаги только по решению владельца
  e6dryrun(); p.setProperty('E6_DRYRUN_AT', new Date().toISOString()); return 'dryrun (повтор) — STOP';
}
