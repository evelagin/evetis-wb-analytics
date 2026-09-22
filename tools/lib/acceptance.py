"""Разбор приёмочных SQL-артефактов в исполнимые проверки.

В репозитории накопились три способа записывать приёмку, и все три уже согласованы
владельцем при сдаче этапов. Переписывать их нельзя: переписать условие приёмки —
значит переписать бизнес-правило. Поэтому здесь не новый формат, а **адаптеры**,
которые исполняют существующие артефакты как есть.

Адаптеры:

* `check_blocks`   — `-- @check <ID>` + один SELECT с колонкой `status` ∈ {PASS, FAIL}.
                     Введён SCALE 1; единственный, у которого есть отчёт по строкам.
* `assert_script`  — `ASSERT <булево выражение> AS '<имя>';`. Скрипт BigQuery,
                     который падает на первом нарушении и не даёт отчёта. Адаптер
                     переписывает КАЖДЫЙ оператор в `SELECT IF(<то же выражение>,
                     'PASS','FAIL')`, не трогая само выражение: меняется только
                     способ доставки вердикта, не условие.
* `verdict_select` — набор самостоятельных SELECT, каждый со своей колонкой-вердиктом
                     (`verdict` или `status`). Имя проверки берётся из первого
                     строкового литерала оператора, если он есть.

Всё, что адаптер не смог разобрать однозначно, становится не «пропущено», а
записью `NOT_EXECUTABLE` с причиной: молчаливо потерянная проверка опаснее явно
нерешённой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bq_readonly import ReadOnlyViolation, assert_read_only, mask_sql_comments

CHECK_MARKER = re.compile(r"^--\s*@check\s+([A-Z0-9_]+)\s*$", re.M)
EXPECT_EMPTY = re.compile(r"^--\s*@expect\s+empty_ok\s*$", re.M)
ASSERT_START = re.compile(r"^\s*ASSERT\b", re.I)


def _assert_keyword_pos(stmt: str) -> int | None:
    """Позиция ключевого слова ASSERT в КОДЕ оператора, не в комментарии.

    Приёмочные файлы предваряют каждый ASSERT шапкой из `--`-комментариев, а
    слово ASSERT встречается и в этих комментариях. Искать по сырому тексту —
    значит резать оператор в произвольном месте.
    """
    masked = mask_sql_comments(stmt)
    m = re.match(r"\s*ASSERT\b", masked, re.I)
    return m.start() if m is None else m.end() - len("ASSERT")


def _is_assert(stmt: str) -> bool:
    return bool(ASSERT_START.match(mask_sql_comments(stmt).lstrip("\n\r\t ") and
                                   mask_sql_comments(stmt).strip()))
IDENT_SAFE = re.compile(r"[^A-Z0-9]+")

ADAPTERS = ("check_blocks", "assert_script", "verdict_select")
VERDICT_COLUMNS = ("status", "verdict")


class SuiteContractError(RuntimeError):
    """Артефакт не соответствует контракту выбранного адаптера."""


@dataclass
class Check:
    check_id: str
    source: str
    line: int
    sql: str
    adapter: str
    label: str = ""
    expect_empty_ok: bool = False
    status_column: str = "status"
    executable: bool = True
    not_executable_reason: str | None = None
    original_sql: str = ""


@dataclass
class ParseResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def executable(self) -> list[Check]:
        return [c for c in self.checks if c.executable]

    @property
    def not_executable(self) -> list[Check]:
        return [c for c in self.checks if not c.executable]


# ------------------------------------------------------------- общие утилиты ---

def split_statements(block: str) -> list[str]:
    """Разделить по `;` вне строк и комментариев. Индексы маски = индексы block."""
    masked = mask_sql_comments(block)
    out, start, i, n = [], 0, 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch in "'\"":
            quote, i = ch, i + 1
            while i < n:
                if masked[i] == "\\":
                    i += 2
                    continue
                if masked[i] == quote:
                    break
                i += 1
        elif ch == ";":
            out.append(block[start:i].strip())
            start = i + 1
        i += 1
    tail = block[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def split_statements_with_offsets(text: str) -> list[tuple[int, str]]:
    """То же, но с позицией начала каждого оператора в исходном тексте."""
    masked = mask_sql_comments(text)
    out, start, i, n = [], 0, 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch in "'\"":
            quote, i = ch, i + 1
            while i < n:
                if masked[i] == "\\":
                    i += 2
                    continue
                if masked[i] == quote:
                    break
                i += 1
        elif ch == ";":
            chunk = text[start:i]
            if chunk.strip():
                out.append((start + len(chunk) - len(chunk.lstrip()), chunk.strip()))
            start = i + 1
        i += 1
    tail = text[start:]
    if tail.strip():
        out.append((start + len(tail) - len(tail.lstrip()), tail.strip()))
    return out


def _string_literals(stmt: str) -> list[str]:
    """Строковые литералы в одинарных кавычках, в порядке появления."""
    masked = mask_sql_comments(stmt)  # комментарии убраны, литералы сохранены
    return re.findall(r"'((?:[^'\\]|\\.)*)'", masked)


def make_check_id(prefix: str, label: str, ordinal: int, used: set[str]) -> str:
    """Устойчивый идентификатор из человеческого имени проверки.

    Имя из артефакта — русский текст с пунктуацией; идентификатор обязан быть
    стабильным и машинным. При коллизии добавляется порядковый номер, а не
    случайный суффикс: воспроизводимость важнее красоты.
    """
    base = IDENT_SAFE.sub("_", label.upper().replace("Ё", "Е")).strip("_")
    base = re.sub(r"_+", "_", base)[:60] or f"CHECK_{ordinal:02d}"
    cid = f"{prefix}__{base}" if prefix else base
    if cid in used:
        cid = f"{cid}__{ordinal:02d}"
    used.add(cid)
    return cid


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


# ------------------------------------------------------- адаптер check_blocks ---

def parse_check_blocks(text: str, source: str) -> ParseResult:
    marks = list(CHECK_MARKER.finditer(text))
    if not marks:
        raise SuiteContractError(f"{source}: нет ни одного маркера `-- @check <ID>`")
    res, seen = ParseResult(), set()
    for i, m in enumerate(marks):
        check_id = m.group(1)
        if check_id in seen:
            raise SuiteContractError(f"{source}: повторяющийся идентификатор проверки {check_id!r}")
        seen.add(check_id)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.end():end]
        stmts = split_statements(block)
        if len(stmts) != 1:
            raise SuiteContractError(
                f"{source}:{check_id}: ожидался ровно один оператор, найдено {len(stmts)}")
        sql = stmts[0]
        try:
            assert_read_only(sql)
        except ReadOnlyViolation as e:
            raise SuiteContractError(f"{source}:{check_id}: {e}") from None
        res.checks.append(Check(
            check_id=check_id, source=source, line=_line_of(text, m.start()) , sql=sql,
            adapter="check_blocks", label=check_id,
            expect_empty_ok=bool(EXPECT_EMPTY.search(block)), original_sql=sql))
    return res


# ------------------------------------------------------ адаптер assert_script ---

def _top_level_as_positions(stmt: str) -> list[int]:
    """Позиции ключевого слова AS на нулевой глубине скобок, вне строк."""
    masked = mask_sql_comments(stmt)
    positions, depth, i, n = [], 0, 0, len(masked)
    while i < n:
        ch = masked[i]
        if ch in "'\"":
            quote, i = ch, i + 1
            while i < n:
                if masked[i] == "\\":
                    i += 2
                    continue
                if masked[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and masked[i:i + 2].upper() == "AS" and (i == 0 or not masked[i - 1].isalnum()
                                                                 and masked[i - 1] != "_"):
            after = masked[i + 2:i + 3]
            if after == "" or (not after.isalnum() and after != "_"):
                positions.append(i)
        i += 1
    return positions


def _parse_one_assert(stmt: str) -> tuple[str, str]:
    """`ASSERT <выражение> AS '<имя>'` → (выражение, имя). Иначе SuiteContractError."""
    if not _is_assert(stmt):
        raise SuiteContractError("оператор не начинается с ASSERT")
    masked = mask_sql_comments(stmt)
    kw = re.search(r"\bASSERT\b", masked, re.I)
    if kw is None:
        raise SuiteContractError("ключевое слово ASSERT не найдено в коде оператора")
    body = stmt[kw.end():]
    as_positions = [p for p in _top_level_as_positions(body)]
    if not as_positions:
        raise SuiteContractError("у ASSERT нет имени (`AS '<имя>'`) — вердикт нельзя назвать")
    last = as_positions[-1]
    expr = body[:last].strip()
    tail = body[last + 2:].strip()
    lit = re.fullmatch(r"'((?:[^'\\]|\\.)*)'", tail, re.S)
    if not lit:
        raise SuiteContractError(f"после AS ожидался строковый литерал, получено {tail[:40]!r}")
    if not expr:
        raise SuiteContractError("пустое выражение ASSERT")
    return expr, lit.group(1)


def assert_to_select(expr: str, label: str) -> str:
    """Переписать условие ASSERT в read-only SELECT, НЕ меняя само условие.

    Перевод строк вокруг выражения обязателен: в выражении может стоять
    строчный комментарий `--`, и без перевода строки он съест хвост оператора.
    """
    safe_label = label.replace("\\", "\\\\").replace("'", "\\'")
    return (
        "SELECT IF(\n"
        f"{expr}\n"
        f", 'PASS', 'FAIL') AS status, '{safe_label}' AS check_label"
    )


def parse_assert_script(text: str, source: str, prefix: str = "") -> ParseResult:
    res, used = ParseResult(), set()
    stmts = split_statements_with_offsets(text)
    if not stmts:
        raise SuiteContractError(f"{source}: в файле нет операторов")
    for ordinal, (pos, stmt) in enumerate(stmts, start=1):
        line = _line_of(text, pos)
        if not _is_assert(stmt):
            # Не выбрасываем: информационный SELECT в приёмочном скрипте — это не
            # проверка, и он обязан быть виден в реестре как неисполнимый.
            label = (_string_literals(stmt) or [f"statement {ordinal}"])[0]
            res.checks.append(Check(
                check_id=make_check_id(prefix, f"NONASSERT {ordinal}", ordinal, used),
                source=source, line=line, sql="", adapter="assert_script", label=label[:120],
                executable=False, original_sql=stmt,
                not_executable_reason="оператор не ASSERT: вердикт PASS/FAIL не определён автором"))
            continue
        try:
            expr, label = _parse_one_assert(stmt)
        except SuiteContractError as e:
            res.checks.append(Check(
                check_id=make_check_id(prefix, f"UNPARSED {ordinal}", ordinal, used),
                source=source, line=line, sql="", adapter="assert_script", label="",
                executable=False, original_sql=stmt,
                not_executable_reason=f"ASSERT не разобран: {e}"))
            continue
        sql = assert_to_select(expr, label)
        try:
            assert_read_only(sql)
        except ReadOnlyViolation as e:
            res.checks.append(Check(
                check_id=make_check_id(prefix, label, ordinal, used), source=source, line=line,
                sql="", adapter="assert_script", label=label, executable=False,
                original_sql=stmt,
                not_executable_reason=f"после перевода остаётся мутирующая конструкция: {e}"))
            continue
        res.checks.append(Check(
            check_id=make_check_id(prefix, label, ordinal, used), source=source, line=line,
            sql=sql, adapter="assert_script", label=label, original_sql=stmt))
    return res


# ----------------------------------------------------- адаптер verdict_select ---

def parse_verdict_select(text: str, source: str, prefix: str = "",
                         status_column: str = "verdict") -> ParseResult:
    res, used = ParseResult(), set()
    col = re.compile(rf"\bAS\s+{re.escape(status_column)}\b", re.I)
    for ordinal, (pos, stmt) in enumerate(split_statements_with_offsets(text), start=1):
        line = _line_of(text, pos)
        literals = _string_literals(stmt)
        label = literals[0] if literals else f"statement {ordinal}"
        if not col.search(mask_sql_comments(stmt)):
            res.checks.append(Check(
                check_id=make_check_id(prefix, f"NOVERDICT {ordinal}", ordinal, used),
                source=source, line=line, sql="", adapter="verdict_select", label=label[:120],
                executable=False, original_sql=stmt, status_column=status_column,
                not_executable_reason=(f"в операторе нет колонки `{status_column}`: "
                                       "автор не выразил условие PASS/FAIL")))
            continue
        try:
            assert_read_only(stmt)
        except ReadOnlyViolation as e:
            res.checks.append(Check(
                check_id=make_check_id(prefix, label, ordinal, used), source=source, line=line,
                sql="", adapter="verdict_select", label=label[:120], executable=False,
                original_sql=stmt, status_column=status_column,
                not_executable_reason=str(e)))
            continue
        res.checks.append(Check(
            check_id=make_check_id(prefix, label, ordinal, used), source=source, line=line,
            sql=stmt, adapter="verdict_select", label=label[:120],
            status_column=status_column, original_sql=stmt))
    return res


# ------------------------------------------------------------------ диспетчер ---

def parse_source(text: str, source: str, adapter: str, prefix: str = "",
                 status_column: str = "verdict") -> ParseResult:
    if adapter == "check_blocks":
        return parse_check_blocks(text, source)
    if adapter == "assert_script":
        return parse_assert_script(text, source, prefix)
    if adapter == "verdict_select":
        return parse_verdict_select(text, source, prefix, status_column)
    raise SuiteContractError(f"неизвестный адаптер {adapter!r}; доступны {ADAPTERS}")
