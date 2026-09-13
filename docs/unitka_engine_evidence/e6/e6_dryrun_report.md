# E6-A dry-run (офлайн, экспорт book_2026-09-13.xlsx (10:56 UTC))

## УФ главного листа
сейчас: 4671 правил / 77861 фрагментов (градиентов 4279, формульных 392)
история + строки 1–46: 4063 правил / 76842 фрагментов → 0 (фон печётся статически, 21 секций)
сентябрь: 608 правил / 1019 фрагментов → 152 правил / 895 фрагментов (градиенты 216 → 96: сброшено 120 дублей/затенённых; формульные 392 → 56)
**итого главный лист: 4671 → 152 правил, 77861 → 895 фрагментов**

| шаблон формульного правила | сейчас правил/фрагментов | действие | после |
|---|---|---|---|
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#)` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#)` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#*MAX(REF:REF))` | 25/25 | merge-relative | 1 |
| `AND(REF<>"",REF>#)` | 25/25 | merge-relative | 1 |
| `WEEKDAY(REF,#)>#` | 25/50 | merge-relative | 1 |
| `REF>REF` | 25/26 | keep-per-block | 25 |
| `REF<>""` | 2/26 | keep-per-block | 2 |
| `REF=""` | 2/26 | keep-per-block | 2 |
| `AND(ISNUMBER(REF),REF<=#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF>#,REF<=#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF>#,REF<=#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF>#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF<#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF>#)` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF=#)` | 1/24 | keep | 1 |
| `NOT(ISNUMBER(REF))` | 1/24 | keep | 1 |
| `AND(ISNUMBER(REF),REF>#)` | 2/49 | merge | 1 |
| `AND(ISNUMBER(REF),REF<#)` | 1/24 | keep | 1 |
| `AND(REF<=REF,ISNUMBER(REF),REF>#,ISNUMBER(REF),OR(REF>=#,AND(REF>=#,RE` | 1/25 | keep | 1 |
| `AND(REF<>"",REF<#)` | 1/25 | keep | 1 |
| `AND(REF<>"",REF>#)` | 1/25 | keep | 1 |
| `AND(REF<=TODAY(),REF>#,REF=#)` | 24/25 | merge-relative | 1 |

## УФ других листов
- WB_Юнит_2024: 100 правил / 501 фрагментов → bake+delete
- OZON_Юнит_2025: 1183 правил / 15774 фрагментов → bake+delete
- Склад: 93 правил / 976 фрагментов → keep

## Правки F1–F6 (до/после)
- **F1** WB_Юнит_2024!C3: `=O26+AJ26+BE26+12331-1200+6000+'выкупы'!K83+'Блогеры'!I3+'Блогеры'!I4+'Блогеры'!I5` (значение `#REF!`) → без изменений формулы (лист «Блогеры» восстановлен из резервной копии 1H8ldc…, values-only)
- **F1** WB_Юнит_2024!BU61: `=F92-W7-X7-Y9-AR7-AS7-AT9-BM7-BN7-1200+6000+'выкупы'!O4+'выкупы'!O38+'выкупы'!O68+'Блогеры` (значение `#REF!`) → без изменений формулы (лист «Блогеры» восстановлен из резервной копии 1H8ldc…, values-only)
- **F1** WB_Юнит_2024!BU96: `=F126-W11-X11-Y11-Y12-Y13-Y14-Y17-AR11-AS11-AT11-AT12-AT13-AT14-AT16+'выкупы'!P4+'выкупы'!` (значение `#REF!`) → без изменений формулы (лист «Блогеры» восстановлен из резервной копии 1H8ldc…, values-only)
- **F1** WB_Юнит_2024!BU130: `=F161-W18-X18-Y18-Y19-Y20-Y21-Y22-Y23-AR17-AS17-AT17-AT18-AT19-AT20-AT21+'Блогеры'!I5` (значение `#REF!`) → без изменений формулы (лист «Блогеры» восстановлен из резервной копии 1H8ldc…, values-only)
- **F1** WB_Юнит_2025!VW10: `=C320-'Блогеры'!K13` (значение `#REF!`) → без изменений формулы (лист «Блогеры» восстановлен)
- **F2** WB_Юнит_2025!DP293: `ё` (значение `ё`) → пусто · ISSUE `ADS_MISSING 2025-08-05 305101361`
- **F3** WB_Юнит_2025!BD663: `=BD632*AO663` (значение `#VALUE!`) → =BD633*AO663
- **F3** WB_Юнит_2025!CB663: `=CB632*BM663` (значение `#VALUE!`) → =CB633*BM663
- **F4** WB_Юнит_2025!JX760: `=JX759-JU760+JW759+JX229` (значение `104`) → =JX759-JU760+JW759
- **F5** WB_Юнит_2025: 8 колонок сводки июня — диапазон FILTER до 24-го блока (UT..VD), как в июле–сентябре; значения не меняются (у блоков 23–24 в июне заказов 0)
    - `C633:C662`: `=SUM(FILTER(N633:SX633,MOD(COLUMN(N633:SX633)-COLUMN(N633),24)=0))` → `=SUM(FILTER(N633:UT633,MOD(COLUMN(N633:UT633)-COLUMN(N633),24)=0))`
    - `D633:D662`: `=SUM(FILTER(O633:SY633,MOD(COLUMN(O633:SY633)-COLUMN(O633),24)=0))` → `=SUM(FILTER(O633:UU633,MOD(COLUMN(O633:UU633)-COLUMN(O633),24)=0))`
    - `E633:E662`: `=SUM(FILTER(P633:SZ633,MOD(COLUMN(P633:SZ633)-COLUMN(P633),24)=0))` → `=SUM(FILTER(P633:UV633,MOD(COLUMN(P633:UV633)-COLUMN(P633),24)=0))`
    - `F633:F662`: `=SUM(FILTER(Q633:TA633,MOD(COLUMN(Q633:TA633)-COLUMN(Q633),24)=0))` → `=SUM(FILTER(Q633:UW633,MOD(COLUMN(Q633:UW633)-COLUMN(Q633),24)=0))`
    - `G633:G662`: `=SUM(FILTER(R633:TB633,MOD(COLUMN(R633:TB633)-COLUMN(R633),24)=0))` → `=SUM(FILTER(R633:UX633,MOD(COLUMN(R633:UX633)-COLUMN(R633),24)=0))`
    - `H633:H662`: `=SUM(FILTER(S633:TC633,MOD(COLUMN(S633:TC633)-COLUMN(S633),24)=0))` → `=SUM(FILTER(S633:UY633,MOD(COLUMN(S633:UY633)-COLUMN(S633),24)=0))`
    - `I633:I662`: `=SUM(FILTER(W633:TG633,MOD(COLUMN(W633:TG633)-COLUMN(W633),24)=0))` → `=SUM(FILTER(W633:VC633,MOD(COLUMN(W633:VC633)-COLUMN(W633),24)=0))`
    - `J633:J662`: `=SUM(FILTER(X633:TH633,MOD(COLUMN(X633:TH633)-COLUMN(X633),24)=0))` → `=SUM(FILTER(X633:VD633,MOD(COLUMN(X633:VD633)-COLUMN(X633),24)=0))`
- **F6** WB_Юнит_2025!TD765: `=TD764-TA765+TC764+1` (значение `1`) → без изменений (D4: сохранить как предполагаемую ручную поправку; ISSUE UNEXPLAINED_MANUAL_ADJUSTMENT до сверки)
- **F6** WB_Юнит_2025!TD730: `=TD729-TA730+TC729+1` (значение `0`) → без изменений (D4)

## Заморозка
- Акции: формул 4563, ошибок 0, TODAY 1 → formulas→values, A1 TODAY→дата, A2 пометка АРХИВ, hide
- OZON_Юнит_2025: формул 31267, ошибок 1764, TODAY 0 → formulas→values, ошибки→пусто, УФ bake+delete, hide
- WB_Юнит_2024: формул 4503, ошибок 14, TODAY 0 → только УФ bake+delete (формулы остаются: витрина E2/E3 2025 ссылается на C2/C3)

## Сетка
- WB_Юнит_2025: 768×650, использовано до 767×600 → удалить колонок 50, строк 0
- WB_Юнит_2024: 1000×80, использовано до 161×75 → удалить колонок 5, строк 789
- OZON_Юнит_2025: 465×424, использовано до 464×404 → удалить колонок 20, строк 0
- Склад: 1000×87, использовано до 134×87 → удалить колонок 0, строк 816
- Ценообразование: 1000×64, использовано до 358×44 → удалить колонок 20, строк 592
- Акции: 1000×235, использовано до 63×215 → удалить колонок 20, строк 887
- выкупы: 83×51, использовано до 83×39 → удалить колонок 12, строк 0
- ZZ_AUDIT_LOG: 214×13, использовано до 214×13 → удалить колонок 0, строк 0
- ZZ_QA_СЕНТЯБРЬ_2026: 127×9, использовано до 127×9 → удалить колонок 0, строк 0
- ZZ_CONFIG: 27×3, использовано до 27×3 → удалить колонок 0, строк 0

## Блогеры (D2)
- источник: 1H8ldcUNpMbkceVzfHGTNwdMLqdaNVzP8JJ1qK_i3Z6s («…_BACKUP_до_аудита», 10.09.2026 14:58 UTC)
- доказательство: ZZ_AUDIT_1789053792496.json (снимок 10.09 15:23 UTC): лист «Блогеры» i=9, 2451×21, формул 3018, непустых 8642, ошибок 0
- метод: values-only копия в лист «Блогеры» живой книги; формулы 2024!C3/BU61/BU96/BU130 и 2025!VW10 не меняются; провенанс RESTORED_FROM_BACKUP в A1-заметке и ZZ_AUDIT_LOG

## Базовые отпечатки (значения/формулы по листам, для сверки с живым снимком)
- WB_Юнит_2025: values 169090 ячеек `ed055ffc6299ae4f`, formulas 91667 `cb1094b0479bff43`
- WB_Юнит_2024: values 8924 ячеек `ce07108a233bf873`, formulas 4503 `de9af21b1c226f4a`
- OZON_Юнит_2025: values 58504 ячеек `a109a04932e55456`, formulas 31267 `6d1ad3dfe22e7e46`
- Склад: values 2809 ячеек `8d8b52bdedcdd327`, formulas 1917 `d84072e6511f3146`
- Ценообразование: values 4506 ячеек `4b4c7c9f2575a2f9`, formulas 1276 `4550658f7d0dcf42`
- Акции: values 5721 ячеек `7549d29a20838ddc`, formulas 4563 `f4df1f391725e4ac`
- выкупы: values 625 ячеек `9d9241824f9194b9`, formulas 250 `3eb09a9c1e7d4ac5`
- ZZ_AUDIT_LOG: values 2589 ячеек `8480c7cb7848ff89`, formulas 0 `e3b0c44298fc1c14`
- ZZ_QA_СЕНТЯБРЬ_2026: values 982 ячеек `5a9e7f4f922c7b61`, formulas 314 `8d59d2c979459cc3`
- ZZ_CONFIG: values 71 ячеек `657ce8d86785d745`, formulas 0 `e3b0c44298fc1c14`
