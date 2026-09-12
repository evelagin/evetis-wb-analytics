// UNITKA 2.0 R5 — доступ к воронке продаж. Только новый файл, существующие не тронуты.
// Токены НИКОГДА не логируются: печатаются только имя и длина. Agent: claude-opus-5, v5.0.0
//
// Результат прогона 10.09.2026 в проекте `evetis-wb-analitics`:
//   Script Properties: 36 ключей, 8 токенов WB (ANALYTICS/CONTENT/DOCUMENTS/FINANCE/
//   MARKETPLACE/PROMOTION/STATISTICS/SUPPLIES). WB_TOKEN_REPORTS ОТСУТСТВУЕТ.
//   Secret Manager list -> 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT: у манифеста нет scope
//   https://www.googleapis.com/auth/cloud-platform. Расширять scope продакшн-проекта
//   нельзя без решения владельца — это заставит переавторизовать боевые триггеры.
var R5_HOST = 'https://seller-analytics-api.wildberries.ru';
var R5_PROJECT = 'project-fa311fc0-4d87-4781-986';
function main() {
var L = [];
var p = PropertiesService.getScriptProperties().getProperties();
var keys = Object.keys(p).sort();
L.push('Script Properties (' + keys.length + '):');
for (var i = 0; i < keys.length; i++) L.push('  ' + keys[i] + '  [len ' + String(p[keys[i]] || '').length + ']');
L.push('');
L.push('--- Secret Manager ---');
try {
var t = ScriptApp.getOAuthToken();
var u = 'https://secretmanager.googleapis.com/v1/projects/' + R5_PROJECT + '/secrets?pageSize=100';
var r = UrlFetchApp.fetch(u, { headers: { Authorization: 'Bearer ' + t }, muteHttpExceptions: true });
L.push('list -> ' + r.getResponseCode());
if (r.getResponseCode() === 200) {
var arr = JSON.parse(r.getContentText()).secrets || [];
L.push('секретов: ' + arr.length);
for (var j = 0; j < arr.length; j++) L.push('  * ' + String(arr[j].name).split('/').pop());
} else {
L.push(r.getContentText().slice(0, 400));
}
} catch (e) {
L.push('exception: ' + e);
}
Logger.log(L.join('\n'));
}
