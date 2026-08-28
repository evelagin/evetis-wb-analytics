#!/usr/bin/env bash
# =============================================================================
# metabase_export_snapshot.sh — штатный экспорт снимка Metabase в каталог metabase/
#
# НАЗНАЧЕНИЕ. Каталог metabase/ — второй, независимый слой аварийного
# восстановления (первый — физическая копия application DB, в Git не хранится).
# Скрипт снимает версионируемые определения коллекций, дашбордов и карточек
# и пересобирает контрольные суммы в metabase/manifest.json.
#
# КОНВЕНЦИЯ ФОРМАТА (воспроизводит существующий снимок байт в байт):
#   1. объект берётся через `mb <noun> get <id> --full --json`;
#   2. РЕКУРСИВНО удаляются волатильные поля из
#      manifest.json -> excluded_volatile_fields (в том числе во вложенных
#      объектах: card.collection, dashboard.dashcards[].card);
#   3. в КАРТОЧКИ (и только в них) добавляется поле metabase_version;
#      дашборды и коллекции его не получают;
#   4. вывод форматируется как `jq -S --indent 1` с завершающим переводом строки.
#
# ТОЛЬКО ЧТЕНИЕ. Скрипт использует исключительно `get`-глаголы `mb`. Он не
# создаёт, не изменяет и не архивирует ни одну карточку, дашборд или коллекцию;
# раскладку дашбордов и привязки фильтров не трогает. Единственное, что он
# пишет, — файлы внутри каталога metabase/ этого репозитория.
#
# СЕКРЕТЫ. Учётные данные в скрипте отсутствуют. Аутентификация выполняется
# именованным профилем `mb`, который хранится в локальной конфигурации CLI вне
# репозитория. Реквизиты подключения к BigQuery в снимок не входят: они лежат в
# поле `details`, которое перечислено в excluded_volatile_fields.
#
# FAIL-CLOSED. Скрипт прекращает работу с ненулевым кодом, если недоступны
# зависимости, профиль не аутентифицирован, Metabase не отвечает, объект не
# отдаётся, результат не является валидным JSON или состав объектов разошёлся
# с манифестом. Каталог metabase/ при любой такой ошибке остаётся нетронутым:
# экспорт целиком выполняется во временный каталог и переносится на место
# только после успешной проверки всех объектов.
#
# ИСПОЛЬЗОВАНИЕ
#   tools/metabase_export_snapshot.sh --dry-run            # проверка без записи
#   tools/metabase_export_snapshot.sh                      # полный снимок
#   tools/metabase_export_snapshot.sh --card 51 --card 53  # выборочно
#   tools/metabase_export_snapshot.sh --profile prod --dry-run
#
#   --dry-run             ничего не записывает; печатает, что изменилось бы
#   --card / --dashboard / --collection <id>   выборочный экспорт (повторяемо)
#   --profile <name>      профиль mb (по умолчанию $MB_PROFILE или evetis-dev)
#   --help                справка
#
# ЗАМЕЧАНИЕ О ШУМЕ В DIFF. Поля last_used_at, cache_invalidated_at и
# query_average_duration в excluded_volatile_fields действующего снимка НЕ
# входят и потому пишутся как есть. Полный экспорт обновит их у всех объектов,
# даже если смысловая часть не менялась. Для точечного чекпоинта используйте
# выборочный режим. Расширение списка исключений — отдельное решение владельца:
# оно обесценивает контрольные суммы всех уже зафиксированных файлов.
# =============================================================================
set -euo pipefail

PROFILE="${MB_PROFILE:-evetis-dev}"
DRY_RUN=0
SEL_CARDS=""; SEL_DASHBOARDS=""; SEL_COLLECTIONS=""; SELECTIVE=0

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

usage() { sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1; shift ;;
    --profile)    [ $# -ge 2 ] || die "--profile требует значение"; PROFILE="$2"; shift 2 ;;
    --card)       [ $# -ge 2 ] || die "--card требует id";       SEL_CARDS="$SEL_CARDS $2";             SELECTIVE=1; shift 2 ;;
    --dashboard)  [ $# -ge 2 ] || die "--dashboard требует id";  SEL_DASHBOARDS="$SEL_DASHBOARDS $2";   SELECTIVE=1; shift 2 ;;
    --collection) [ $# -ge 2 ] || die "--collection требует id"; SEL_COLLECTIONS="$SEL_COLLECTIONS $2"; SELECTIVE=1; shift 2 ;;
    --all)        SELECTIVE=0; shift ;;
    --help|-h)    usage ;;
    *)            die "неизвестный аргумент: $1" ;;
  esac
done

# ── Зависимости и расположение репозитория (без абсолютных путей) ────────────
for bin in mb jq git shasum; do
  command -v "$bin" >/dev/null 2>&1 || die "не найден '$bin' в PATH"
done
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "не git-репозиторий"
cd "$REPO_ROOT"
SNAP_DIR="metabase"
MANIFEST="$SNAP_DIR/manifest.json"
[ -f "$MANIFEST" ] || die "не найден $MANIFEST — каталог снимка отсутствует"

# ── Fail-closed: профиль должен быть аутентифицирован ────────────────────────
AUTH="$(mb auth status --profile "$PROFILE" --json 2>/dev/null)" \
  || die "профиль '$PROFILE' недоступен; проверьте 'mb auth list'"
[ "$(printf '%s' "$AUTH" | jq -r '.present // false')" = "true" ] \
  || die "профиль '$PROFILE' не аутентифицирован; выполните 'mb auth login' вручную"

EXCL="$(jq -c '.excluded_volatile_fields' "$MANIFEST")"
[ "$EXCL" != "null" ] || die "в манифесте нет excluded_volatile_fields"
MBV="$(jq -r '"v" + .metabase_version.tag[1:] + " (" + .metabase_version.hash + ")"' "$MANIFEST")"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Скраб: рекурсивное удаление волатильных полей + опциональная инъекция версии.
scrub() { # $1 = inject_version (0|1)
  if [ "$1" = "1" ]; then
    jq -S --indent 1 --argjson excl "$EXCL" --arg mbv "$MBV" \
      'def s: walk(if type == "object" then delpaths([$excl[] | [.]]) else . end);
       s | .metabase_version = $mbv'
  else
    jq -S --indent 1 --argjson excl "$EXCL" \
      'def s: walk(if type == "object" then delpaths([$excl[] | [.]]) else . end);
       s'
  fi
}

# ТОЛЬКО ЧТЕНИЕ: единственный вызов mb во всём скрипте — глагол `get`.
fetch_one() { # $1=noun $2=id $3=inject $4=outfile
  local raw; raw="$TMP/raw.json"
  mb "$1" get "$2" --full --max-bytes 4000000 --profile "$PROFILE" --json > "$raw" 2>/dev/null \
    || die "не удалось прочитать $1 $2 из Metabase (профиль '$PROFILE')"
  jq -e 'type == "object" and (.id != null)' "$raw" >/dev/null 2>&1 \
    || die "$1 $2: ответ Metabase не является объектом с полем id"
  scrub "$3" < "$raw" > "$4" || die "$1 $2: не удалось нормализовать JSON"
  [ -s "$4" ] || die "$1 $2: пустой результат экспорта"
}

# ── Инвентарь снимка берётся из манифеста; изобретать состав скрипт не вправе ─
# bash 3.2 (штатный на macOS) не знает mapfile — держим id-шники строками.
ALL_CARDS="$(jq -r '.cards[].id'       "$MANIFEST" | tr '\n' ' ')"
ALL_DASHBOARDS="$(jq -r '.dashboards[].id'  "$MANIFEST" | tr '\n' ' ')"
ALL_COLLECTIONS="$(jq -r '.collections[].id' "$MANIFEST" | tr '\n' ' ')"

file_for() { jq -r --arg id "$2" ".${1}[] | select(.id == (\$id|tonumber)) | .file" "$MANIFEST"; }

if [ "$SELECTIVE" = "1" ]; then
  CARDS="$SEL_CARDS"; DASHBOARDS="$SEL_DASHBOARDS"; COLLECTIONS="$SEL_COLLECTIONS"
else
  CARDS="$ALL_CARDS"; DASHBOARDS="$ALL_DASHBOARDS"; COLLECTIONS="$ALL_COLLECTIONS"
fi

count_of() { set -- $1; echo $#; }

note "профиль      : $PROFILE"
note "режим        : $([ "$DRY_RUN" = 1 ] && echo 'DRY-RUN (без записи)' || echo 'ЗАПИСЬ')"
note "объектов     : карточек $(count_of "$CARDS"), дашбордов $(count_of "$DASHBOARDS"), коллекций $(count_of "$COLLECTIONS")"
note ""

CHANGED=0; UNCHANGED=0
export_group() { # $1=noun $2=manifest-key $3=inject ; далее id-шники
  local noun="$1" key="$2" inject="$3"; shift 3
  local id rel out
  for id in "$@"; do
    rel="$(file_for "$key" "$id")"
    [ -n "$rel" ] && [ "$rel" != "null" ] \
      || die "$noun $id отсутствует в манифесте; состав снимка обновляется вручную"
    out="$TMP/$(basename "$rel")"
    fetch_one "$noun" "$id" "$inject" "$out"
    if [ -f "$rel" ] && cmp -s "$rel" "$out"; then
      UNCHANGED=$((UNCHANGED+1))
    else
      CHANGED=$((CHANGED+1))
      note "  изменится: $rel"
      if [ "$DRY_RUN" = "1" ] && [ -f "$rel" ]; then
        diff <(jq -S 'keys' "$rel") <(jq -S 'keys' "$out") >/dev/null 2>&1 \
          || note "             (изменился набор ключей верхнего уровня)"
      fi
    fi
    [ "$DRY_RUN" = "1" ] || cp "$out" "$rel"
  done
}

export_group collection collections 0 $COLLECTIONS
export_group dashboard  dashboards  0 $DASHBOARDS
export_group card       cards       1 $CARDS

# ── Манифест: sha256 всех перечисленных файлов + метки снимка ────────────────
TS="$(date +%Y-%m-%dT%H:%M:%S%z | sed 's/\(..\)$/:\1/')"
BASE="$(git rev-parse HEAD)"
SHAS="$TMP/shas.json"; echo '{}' > "$SHAS"
while IFS= read -r rel; do
  [ -f "$rel" ] || die "манифест ссылается на отсутствующий файл: $rel"
  jq --arg k "$rel" --arg v "$(shasum -a 256 "$rel" | cut -d' ' -f1)" '.[$k]=$v' "$SHAS" > "$SHAS.n"
  mv "$SHAS.n" "$SHAS"
done < <(jq -r '.sha256 | keys[]' "$MANIFEST")

jq -S --indent 1 --arg ts "$TS" --arg base "$BASE" --slurpfile shas "$SHAS" \
  '.export_timestamp = $ts | .baseline_commit = $base | .sha256 = $shas[0]' \
  "$MANIFEST" > "$TMP/manifest.json" || die "не удалось пересобрать манифест"

if [ "$DRY_RUN" = "1" ]; then
  cmp -s "$MANIFEST" "$TMP/manifest.json" || note "  изменится: $MANIFEST"
  note ""
  note "DRY-RUN: файлы не записаны. изменилось бы $CHANGED, совпадает $UNCHANGED."
  exit 0
fi

cp "$TMP/manifest.json" "$MANIFEST"

# ── Проверка целостности: sha256 манифеста должны сойтись с диском ───────────
FAIL=0
while IFS=$'\t' read -r rel sha; do
  [ "$(shasum -a 256 "$rel" | cut -d' ' -f1)" = "$sha" ] || { note "НЕСОВПАДЕНИЕ sha256: $rel"; FAIL=1; }
done < <(jq -r '.sha256 | to_entries[] | "\(.key)\t\(.value)"' "$MANIFEST")
[ "$FAIL" = "0" ] || die "контрольные суммы манифеста не сошлись с диском"

note ""
note "снимок обновлён: изменено $CHANGED, без изменений $UNCHANGED"
note "baseline_commit: $BASE"
note "все sha256 манифеста сверены с диском — совпадают"
