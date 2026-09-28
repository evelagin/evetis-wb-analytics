# SPP-3 — production WRITE колонки `AB` Юнитки WB (2026-09-28)

Решение владельца 28.09: controlled WRITE и штатный режим `UNITKA_SPP_MODE=write` на `unitka-engine-prod`.
Дизайн и контракт — `docs/UNITKA_SPP_3_AB_AUTO_2026-09-28.md`. Код не менялся (образ `sha256:2948d2d5…`,
`main` 22d2025), повторного деплоя не было.

**Итог: SPP_AUTO_PRODUCTION = PASS.** С 01.09.2026 источник истины `AB`:
`WB supplier/orders → RAW_WB_ORDERS → V_WB_ORDERS → wb_mart.V_WB_SPP_DAILY → Юнитка AB`. Ручной ввод `AB`
с 01.09 (в окне 35 дней до кандидата LCD) движок перезаписывает фактом.

## Хронология (UTC)

| Время | Шаг | Результат |
|---|---|---|
| 11:20 | проверка перед записью | образ 2948d2d5, режим observe, LCD 27.09 (B2 = 46292), параллельных прогонов нет, DQ вью 11/11 = 0 |
| 11:22 | свежий план, observe `unitka-engine-prod-n68gj` | 183 строки источника, 328 ячеек, digest `b7d574cf…` — **совпал с observe 10:38**, дельта 0 |
| 11:26 | снимок отката + отпечатки до записи → архив `pre/` | неизменяемое хранение (retention до 2029) |
| 12:01 | `UNITKA_SPP_MODE` observe → write | сверка конфигурации Job'а: изменилось только это значение |
| 12:02 | запись, `unitka-engine-prod-5jxmf` | 168 записано + 160 очищено; `SPP_READBACK` PASS; QA 11/11 PASS; `LCD_NOT_ADVANCED` (кандидат = закоммиченному) |
| 12:08 | независимая сверка по книге | все счётчики 0 (ниже) |
| 12:15 | готовность отката, PLAN `unitka-engine-prod-wnt79` | 328 ячеек в новом состоянии, drift 0, 0 записей |
| 13:02 | повтор, `unitka-engine-prod-jxjhg` | 0 записей, 0 очисток, already_correct 183; отпечатки до/после побайтно равны |

## Классы ячеек

| ACTUAL_REPLACE | ACTUAL_FILL | NEGATIVE_MARKUP_TO_ZERO | MANUAL_CLEAR_NO_ORDER | MANUAL_CLEAR_FUNNEL_ONLY | ALREADY_CORRECT |
|---|---|---|---|---|---|
| 84 | 83 | 1 | 158 | 2 | 15 |

Политика владельца: 100 ручных значений при наличии заказа = 84 + 15 + 1; 158 при `Q = 0` и 2 FUNNEL_FALLBACK
очищены; до 01.09 и будущие дни не тронуты. `SPP_MISSING` (`Q > 0` без строки вью) = 6 дней «воронка без Orders API».

## Независимая сверка (перечитка книги, не журнал писателя)

`SPP_VALUE_MISMATCHES 0 · SPP_FORMAT_MISMATCHES 0 · SPP_FORMULA_MUTATIONS 0 · UNPLANNED_AB_MUTATIONS 0 ·
UNPLANNED_NON_AB_INPUT_MUTATIONS 0 · DATE/SKU_MAPPING 0 · DATES_BEFORE_2026_09_01_TOUCHED 0 · FUTURE_DATES_TOUCHED 0`.
Пересчитались только значения формул `AC` (187), `Z` (122), сводной `K` (28). Ozon, ZZ_CONFIG, именованные
диапазоны, геометрия, условное форматирование — без изменений. Формат `AB` — `NUMBER #,##0` везде, как до записи.

## Эффект на ДРР и прибыль (сентябрь MTD, строка 767)

| | До | После | Δ |
|---|---|---|---|
| ДРР `K767` | 16,826 % | 21,291 % | **+4,466 п.п.** |
| Прибыль «Доходность (общая)» `I767` | 126,49 ₽ | 126,49 ₽ | **0** |

Прибыль не зависит от `AB` (`W = Q·AI − X − AG − S·(AI+AF+REVERSE_LEG_RATE) + Y`, ни одна ячейка `W`/`I` не
изменилась). Знаменатель ДРР `Σ(Q−N)·AC` уменьшился на 56 330,25 ₽, разложение по классам сходится до копейки:
ACTUAL_FILL −54 765,95 (пустая `AB` считалась СПП 0 % — ДРР был занижен), ACTUAL_REPLACE −2 034,68,
FUNNEL_ONLY +322,40, NEGATIVE→0 +147,98, CLEAR при Q=0 — 0.

## Сохранность режима при деплоях

`deploy-prod.yml` обновляет у `unitka-engine-prod` только `IMAGE_DIGEST`/`GIT_SHA` (`--update-env-vars`, остальные
переменные сохраняются — подтверждено деплоем 36410677040: `observe`, выставленный до него, остался); в Terraform
env Job'а — `ignore_changes`; Scheduler запускает Job без переопределений. Пересоздание Job'а Terraform'ом вернуло бы
код-по-умолчанию `off` (безопасно: AB остаются, не пишутся). Изменений конфигурации не требуется.

## Откат (не выполнялся — отказов не было)

```bash
gcloud run jobs execute unitka-engine-prod --region europe-west1 --wait --args=unitka-spp-rollback \
  --update-env-vars=UNITKA_SPP_ROLLBACK_MANIFEST=<manifest_b64>,UNITKA_SPP_ROLLBACK_DIGEST=b7d574cfcedff8faebedd8958917c944b3a0cff42df95e9438abe3ab2a3425d5,UNITKA_SPP_ROLLBACK_WRITE=1
gcloud run jobs update unitka-engine-prod --region europe-west1 --update-env-vars UNITKA_SPP_MODE=off
```

`manifest_b64` — `pre/rollback_manifest_b7d574cf.b64` в архиве. Откат корректен, пока ячейки не изменил
следующий прогон (поздние заказы) — иначе `SPP_ROLLBACK_DRIFT`, откат по манифесту того прогона.

## Доказательства

`gs://evetis-audit-evidence-37074083763/unitka-spp/production_write_2026-09-28/` (`pre/`, `write/`, `second/`),
общий манифест `EVIDENCE_MANIFEST.sha256` = `45b3eb5c9f4b77e7524c025462410900fc9d7d183dd2efe21fc4d92bd8dcb5c4`.
Observe — `…/unitka-spp/observe_2026-09-28/`.

| Объект | SHA-256 |
|---|---|
| снимок отката `rollback_manifest_b7d574cf.b64` (= манифест прогона записи) | `41a7f0e562ed3b17075e5881bc104252a99f6b3be17410f06708e42093202a5c` |
| отпечаток AB до записи | `a049aca0c4fdf07bdcad160e6951090f423b2ca30d9b897152a61eecc67e4472` |
| отпечаток AB после записи = после повтора | `6390a7db353ca6562649717e5777573209ccc4091fc932f9119bd50f11ae8187` |
| независимая сверка | `fbeb59cef1cfd8d30a30d656f369eb1998ae34803aa2e30df1a68dc1f2c1b00a` |
