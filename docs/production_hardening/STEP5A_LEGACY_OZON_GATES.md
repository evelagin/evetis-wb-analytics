# Owner gates: legacy Unitka и Ozon orphan trigger

## 7. updateDailyUnitV2 (проект «Evetis WB», 1k1gICsz…)
Доказано:
- код не менялся с 18.05; пишет лист «Юнит <Месяц> <Год>» книги `getActiveSpreadsheet()`, берёт данные напрямую из WB API;
- вторник = полный пересчёт месяца → 360,748 с 15.09 (вторник);
- в 88 файлах `evetis-wb-analitics` ссылок на функцию/лист нет;
- триггер ставится `setupDailyUnitTriggerV2` на 10:00 МСК; 04–10.08 боевой sales ежедневно получал HTTP 429 в 10:22–10:23.
Не доказано:
- контейнер: вероятно «EVETIS WB» (1Z2WDKNr…, создана 29.04, за день до проекта), но экспорт Drive отдаёт только первый лист;
- реальное использование человеком: `viewedByMeTime` равен `modifiedTime` и совпадает со временем триггеров (Drive засчитывает скрипт как просмотр) → по метаданным не определить;
- полный список триггеров проекта и downstream (IMPORTRANGE/ссылки из других книг) — нужен Apps Script UI;
- уникальные функции legacy-листа, не покрытые Unitka Engine (например подарки/giveaway, blogger) — нужен сравнительный разбор листа.
**SAFE TO DISABLE LEGACY UNITKA = NO** (до проверки листа, триггеров и подтверждения владельца, что лист не открывается).

## 8. updateStocksOzon (проект «Evetis Ozon», 1MemRXRC…)
Доказано: функции нет (0 определений); замена — `updateWarehouseV2` (`WarehouseOzon_v2.gs:47`, те же остатки через
`v2/analytics/stock_on_warehouses` + продажи по складам из FBO); установщик `setupTriggers…` в `Dashboard_WB.gs:770`
заново создаёт триггер на удалённую функцию. BQ-слой Ozon независим (Cloud Run, свежий 15.09 19:01).
Код-фикс (подготовлен, не применён):
```diff
- var fns = ["updateDashboardOzon", "updateFinanceOzon", "updateStocksOzon"];
+ var fns = ["updateDashboardOzon", "updateFinanceOzon"];   // updateStocksOzon удалена, замена updateWarehouseV2
- Logger.log("  updateStocksOzon — ежедневно ~07:00 MSK");
```
плюс удаление строки `ScriptApp.newTrigger("updateStocksOzon")…` в том же установщике (номер строки сверить в редакторе).
Не проверено: наличие и последнее успешное выполнение триггера `updateWarehouseV2` (нужен Apps Script UI).
Production trigger delete — только после отдельного approval.
