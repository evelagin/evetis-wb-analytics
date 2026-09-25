"""Read-only BigQuery access for architecture and data-quality tooling.

Единственный способ, которым инструменты этого каталога обращаются к BigQuery.
Клиент отказывает до отправки запроса, если оператор не SELECT/WITH: инструменты
архитектурного контура не изменяют production ни при каких аргументах.

Хост по умолчанию — `www.googleapis.com`. Это не стилистический выбор: с рабочей
машины владельца `bigquery.googleapis.com` фильтруется на сетевом уровне (HTTP 403
от Google frontend даже на неаутентифицированный discovery), а legacy-хост работает
с тем же OAuth-токеном. Оба хоста разрешены, менять хост можно `--bq-host`.

Токен берётся ЯВНО: переменная окружения (по умолчанию `BQ_READONLY_ACCESS_TOKEN`)
или команда оператора (`--token-command "gcloud auth print-access-token"`),
исполняемая без shell. Токен не попадает ни в отчёты, ни в сообщения об ошибках.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ALLOWED_HOSTS = frozenset({"www.googleapis.com", "bigquery.googleapis.com"})

# Отклоняется ДО отправки. Список намеренно широкий: инструмент не обязан уметь
# отличать безопасный CREATE от опасного — он не делает никаких CREATE вообще.
# REPLACE намеренно НЕ в списке: это ещё и обычная строковая функция
# (`REPLACE(x, ',', '.')` встречается в приёмочных проверках). Опасна она только
# в составе `CREATE OR REPLACE`, а `CREATE` перехватывается отдельно.
_MUTATING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE"
    r"|EXPORT|LOAD\s+DATA|CALL|BEGIN|COMMIT|ROLLBACK|EXECUTE\s+IMMEDIATE|ASSERT)\b",
    re.IGNORECASE,
)
_STARTS_READONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    """Запрос отклонён клиентом как немутирующий-только."""


class BigQueryError(RuntimeError):
    """Ошибка BigQuery. Текст ответа усечён и не содержит токена."""


def strip_sql_comments(sql: str) -> str:
    """Убрать комментарии, чтобы слово DROP внутри комментария не блокировало SELECT."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":  # строковый литерал — переносим как есть
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "\\":
                    out.append(sql[i + 1: i + 2])  # экранированный символ переносится, а не теряется
                    i += 2
                    continue
                if sql[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("--", i) or sql.startswith("#", i):
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def mask_sql_comments(sql: str) -> str:
    """Заменить комментарии пробелами ПОЗИЦИЯ В ПОЗИЦИЮ.

    В отличие от strip_sql_comments длина строки сохраняется, поэтому смещения,
    найденные в маске, указывают на те же символы исходного текста. Разбор на
    операторы обязан пользоваться именно этим: схлопывание комментария в один
    пробел сдвигает индексы и режет SQL в произвольном месте.
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "\\":
                    if i + 1 < n:
                        out.append(sql[i + 1])
                    i += 2
                    continue
                if sql[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if sql.startswith("--", i) or sql.startswith("#", i):
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            out.append(" " * (stop - i))
            i = stop
            continue
        out.append(ch)
        i += 1
    masked = "".join(out)
    assert len(masked) == n, "маска обязана сохранять длину"
    return masked


def blank_string_literals(sql: str) -> str:
    """Заменить СОДЕРЖИМОЕ строковых литералов пробелами, сохранив длину.

    Ключевое слово внутри литерала не является оператором: приёмочная проверка
    `... LIKE '%TRUNCATE%'` доказывает, что процедура содержит TRUNCATE, и сама
    ничего не выполняет. Без этого read-only гейт отвергал бы именно те проверки,
    которые сторожат мутации.
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                if sql[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == quote:
                    out.append(quote)
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    blanked = "".join(out)
    assert len(blanked) == n, "бланкирование обязано сохранять длину"
    return blanked


def assert_read_only(sql: str) -> None:
    bare = blank_string_literals(strip_sql_comments(sql))
    if not _STARTS_READONLY.match(bare):
        raise ReadOnlyViolation("только SELECT / WITH; запрос отклонён до отправки")
    hit = _MUTATING.search(bare)
    if hit:
        raise ReadOnlyViolation(
            f"мутирующее ключевое слово {hit.group(0).upper()!r}; запрос отклонён до отправки"
        )


def resolve_token(env_name: str | None, token_command: str | None, environ) -> str:
    """Токен приходит явно. Сам модуль ничего за токеном не запускает по умолчанию."""
    if token_command:
        argv = shlex.split(token_command)
        if not argv:
            raise BigQueryError("пустая --token-command")
        try:
            done = subprocess.run(argv, capture_output=True, text=True, check=True, shell=False)
        except FileNotFoundError:
            raise BigQueryError(f"не найдена команда токена: {argv[0]!r}") from None
        except subprocess.CalledProcessError as e:
            raise BigQueryError(f"команда токена завершилась кодом {e.returncode}") from None
        token = done.stdout.strip()
    else:
        token = (environ.get(env_name or "BQ_READONLY_ACCESS_TOKEN") or "").strip()
    if not token:
        raise BigQueryError(
            "нет токена: задай переменную окружения или --token-command "
            '"gcloud auth print-access-token"'
        )
    return token


@dataclass
class ReadOnlyBigQuery:
    project: str
    token: str
    location: str = "EU"
    host: str = "www.googleapis.com"
    label_purpose: str = "architecture-readonly"
    timeout: int = 180
    queries_issued: int = 0
    bytes_billed: int = 0
    _opener: object | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.host not in ALLOWED_HOSTS:
            raise BigQueryError(f"хост {self.host!r} не в списке разрешённых {sorted(ALLOWED_HOSTS)}")

    def query(self, sql: str, *, max_results: int = 10000, dry_run: bool = False) -> list[dict]:
        assert_read_only(sql)
        body = {
            "query": sql,
            "useLegacySql": False,
            "location": self.location,
            "maxResults": max_results,
            "timeoutMs": self.timeout * 1000,
            "dryRun": dry_run,
            "labels": {"purpose": self.label_purpose},
        }
        url = f"https://{self.host}/bigquery/v2/projects/{self.project}/queries"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout + 30) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            raise BigQueryError(f"BigQuery HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise BigQueryError(f"сеть недоступна: {e.reason}") from None
        self.queries_issued += 1
        self.bytes_billed += int(payload.get("totalBytesProcessed") or 0)
        if dry_run:
            return []
        if not payload.get("jobComplete", False):
            raise BigQueryError("BigQuery не завершил задание за отведённое время")
        rows = _rows(payload)
        # Молчаливая усечённая страница даёт неполный инвентарь, который выглядит как полный.
        total = int(payload.get("totalRows") or len(rows))
        if total > len(rows):
            raise BigQueryError(
                f"ответ усечён: {len(rows)} из {total} строк; подними max_results или сузь запрос"
            )
        return rows


def _rows(payload: dict) -> list[dict]:
    fields = [f["name"] for f in payload.get("schema", {}).get("fields", [])]
    out = []
    for row in payload.get("rows", []) or []:
        values = []
        for cell in row["f"]:
            v = cell["v"]
            if isinstance(v, list):
                v = [x.get("v") for x in v]
            values.append(v)
        out.append(dict(zip(fields, values)))
    return out
