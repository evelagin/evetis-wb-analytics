# R1 — zero-write shadow pilot движка ответов v3.1E

Статус: **подготовлено, НЕ активировано.** Каждый шаг ниже выполняется только после отдельного ACK владельца.

## Что такое R1

Новый движок (3.1E) считает план, черновик и вердикты по **новым** реальным отзывам и вопросам рядом с v2 и
записывает только внутреннее наблюдение. У R1 **нет полномочий** на запись в WB, публикацию, изменение
карточек Telegram, замену черновика v2 или owner override. Для покупателей работает только v2.

| Слой | Флаг | Production сейчас |
|---|---|---|
| v2 (покупатели) | — | работает |
| v3 baseline shadow | `V3_SHADOW_ENABLED` | `true` (с 2026-09-29) |
| 3.1E quality shadow (R1) | `V31_QUALITY_SHADOW_ENABLED` + активация | нет переменной = выключен |
| Восстановление оператора | `V31_OPERATOR_RECOVERY_ENABLED` | нет = `false` |
| Owner override | `V31_OWNER_OVERRIDE_ENABLED` | нет = `false` |
| Автопубликация | `knowledge_v3/policy` `runtime.auto_publish` | `false` (не флаг окружения) |

## Контракт активации (`app/v3/pilot.py`)

- `V31_QUALITY_SHADOW_ENABLED=true`
- `V31_SHADOW_ACTIVATION_ID` — уникальный, `[A-Za-z0-9._-]{1,64}`, например `r1-20261007`
- `V31_SHADOW_START_AT` / `V31_SHADOW_END_AT` — ISO-8601 со смещением, `START` включительно, `END` исключительно
- `V31_SHADOW_PILOT_MAX_COMMUNICATIONS=20` (допустимо 1…20; больше — изменение кода по новому ACK)
- восстановление оператора и owner override обязаны быть выключены, иначе пилот не стартует

Любое нарушение — слой 3.1E выключен (`SHADOW_ACTIVATION_INVALID` в сводке `/poll`), v2 не затронут.
Коммуникация оценивается, только если и дата WB, и первое наблюдение сервиса попадают в окно;
без надёжной метки времени — пропуск. Бэкфилла нет. Перед вызовом LLM берётся транзакционная заявка в
Firestore `v31_shadow_pilot`: одна коммуникация — одна оценка, весь пилот — не больше лимита.

## План выпуска (после ACK владельца)

1. Слить канонический PR; зафиксировать merge SHA.
2. Собрать образ из merge SHA (Cloud Build, как для `fpub-f1eb89d`), записать **digest**.
3. Квалификация образа: полный набор тестов на merge SHA, `python3 deploy/preflight_env.py`
   (без `--env-vars-file` в самом деплое — переменные не перезаписываются).
4. Новая ревизия **без трафика**, флаги R1 не заданы (слой выключен по умолчанию):
   `gcloud run deploy evetis-wb-communications --region europe-west1 --image <digest> --no-traffic --tag r1cand`
5. Проверка ревизии без вызова `/poll`: `/health` по tagged URL, старт без ошибок в журнале, образ = digest,
   отпечаток окружения совпадает с `fpub-f1eb89d`, тот же service account и секреты.
6. Активация — новая ревизия от проверенной с переменными R1 (аддитивно):
   `gcloud run services update evetis-wb-communications --region europe-west1 --no-traffic --tag r1on
   --update-env-vars V31_QUALITY_SHADOW_ENABLED=true,V31_SHADOW_ACTIVATION_ID=<id>,V31_SHADOW_START_AT=<t0>,V31_SHADOW_END_AT=<t0+N дней>,V31_SHADOW_PILOT_MAX_COMMUNICATIONS=20`
   (`V31_OPERATOR_RECOVERY_ENABLED` и `V31_OWNER_OVERRIDE_ENABLED` не задавать.) `<t0>` — время переключения трафика.
7. Атомарное переключение 100% трафика на ревизию `r1on`:
   `gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions <r1on>=100`.
   Делить трафик не нужно и нежелательно: `/poll` приходит одним запросом от Scheduler (без ретраев), и при
   разделении два прогона подряд попадали бы на разные ревизии. Поведение v2 в обеих ревизиях одинаково,
   а заявки пилота транзакционны, поэтому перекрытие старой и новой ревизии не даёт двойной оценки.
8. `/poll` вручную **не вызывать**. Наблюдать естественные запуски `evetis-wb-poll`
   (08, 11, 14, 17, 20 по Москве). Scheduler не менять.
9. После первого прогона проверить: v2-карточки отправлены как раньше; в сводке `/poll` поле
   `v3_shadow.v31_evaluated` / `v31_skips`; документы `v31_shadow_pilot`; записей в WB и правок Telegram от 3.1E нет.

## Kill switch

Самый быстрый путь — вернуть трафик на предыдущую ревизию (секунды, без пересборки):

    gcloud run services update-traffic evetis-wb-communications --region europe-west1 \
      --to-revisions evetis-wb-communications-fpub-f1eb89d=100

Если нужно остаться на новом коде без 3.1E: новая ревизия с выключенным слоем и переключение на неё
(трафик закреплён за именованной ревизией, поэтому одного `--update-env-vars` недостаточно):

    gcloud run services update evetis-wb-communications --region europe-west1 --no-traffic --tag r1off \
      --update-env-vars V31_QUALITY_SHADOW_ENABLED=false
    gcloud run services update-traffic evetis-wb-communications --region europe-west1 --to-revisions <r1off>=100

Проверка: следующий `/poll` — в сводке нет `v31_evaluated` > 0, в журнале нет `v3.1 pilot evaluation`,
счётчик `v31_shadow_pilot/A.<activation>` не растёт.

## Остановка пилота

Пилот останавливается **сам**: после 20 заявок (`PILOT_CAP_REACHED`) или после `V31_SHADOW_END_AT`
(`SHADOW_PILOT_WINDOW_CLOSED`) новых оценок и вызовов LLM нет. После этого владелец либо выключает слой
(kill switch), либо даёт ACK на продолжение с **новым** `V31_SHADOW_ACTIVATION_ID` и окном.

Рекомендуемое окно — **14 дней** (решение владельца): при 5 опросах в день этого обычно хватает на 20 новых
коммуникаций; если не хватило, решение о продлении принимается явно.

## Отчёт пилота

    python scripts/r1_pilot_report.py export --activation <id> --out ~/r1_pilot.csv   # только чтение
    python scripts/r1_pilot_report.py summarize ~/r1_pilot.csv

CSV содержит текст покупателей: держать вне Git, удалить после оценки. Владелец заполняет
`OWNER_PREFERENCE` (V2 / V31E / NEITHER), `EDIT_CLASS` (NONE / MINOR / MATERIAL), `ISSUES`, `NOTE`;
`summarize` считает долю предпочтения 3.1E, правок, ложных блоков, unsupported READY, пропусков safety,
прямых вопросов, сервисных маршрутов и роботизированных ответов.

Критерии R1 (из ACK владельца): unsupported READY = 0, пропуск серьёзной безопасности = 0, утечка
ограниченных значений = 0, выдуманная сервисная предпосылка = 0; ≥ 80% «как есть или мелкая правка»,
≤ 10% существенной переработки. R2 и R3 этим документом **не** разрешаются.
