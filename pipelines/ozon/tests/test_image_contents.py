"""Состав образа Ozon runtime: всё, что импортируется, обязано попасть в образ.

Зачем этот тест существует. Dockerfile копирует модули ПОИМЁННО
(`COPY common.py entities.py main.py ... ./`). Новый модуль, добавленный в
runtime и импортированный из `entities.py`, собирается в образ только если его
руками дописали в эту строку. Забыть её нечем не ловится:

  - `docker build` в ci.yml проходит — Dockerfile синтаксически верен;
  - офлайн-тесты проходят — они импортируют из исходников, а не из образа;
  - отказ проявляется ТОЛЬКО в production, при старте контейнера, ImportError.

Ровно это и случилось с `promo.py` (PR-PROMO-1, 2026-09-22): образ собрался бы
без модуля, а `entities.py` импортирует его на уровне модуля.

Тест читает Dockerfile и исходники, ничего не собирая и не запуская.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
DOCKERFILE = RUNTIME / "Dockerfile"

# Модули, которые Python приносит сам; в образ их копировать не нужно.
STDLIB_PREFIXES = {
    "ast", "base64", "csv", "datetime", "hashlib", "io", "json", "os", "re",
    "sys", "time", "types", "urllib", "uuid", "zipfile", "collections",
    "itertools", "functools", "pathlib", "subprocess", "math", "decimal",
}


def copied_modules() -> set[str]:
    """Имена .py-файлов из инструкций COPY в Dockerfile."""
    out: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.upper().startswith("COPY "):
            continue
        for token in line.split()[1:]:
            if token.endswith(".py"):
                out.add(token)
    return out


def local_imports_of(path: Path) -> set[str]:
    """Локальные модули, которые файл импортирует (без пакетов и stdlib)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    local = set()
    for n in names:
        if n in STDLIB_PREFIXES:
            continue
        if (RUNTIME / f"{n}.py").exists():
            local.add(f"{n}.py")
    return local


def test_dockerfile_copies_every_locally_imported_module():
    copied = copied_modules()
    entrypoint = RUNTIME / "main.py"
    assert entrypoint.name in copied, "точка входа не копируется в образ"

    # Транзитивное замыкание импортов от точки входа.
    pending = [entrypoint]
    seen: set[str] = set()
    required: set[str] = {entrypoint.name}
    while pending:
        f = pending.pop()
        if f.name in seen:
            continue
        seen.add(f.name)
        for mod in local_imports_of(f):
            required.add(mod)
            pending.append(RUNTIME / mod)

    missing = sorted(required - copied)
    assert not missing, (
        f"Dockerfile не копирует модули, которые импортируются из main.py: {missing}. "
        "Образ соберётся, но упадёт при старте с ImportError — и только в production."
    )


def test_dockerfile_does_not_copy_nonexistent_modules():
    """Обратная сторона: COPY несуществующего файла роняет сборку образа."""
    missing = sorted(m for m in copied_modules() if not (RUNTIME / m).exists())
    assert not missing, f"Dockerfile копирует отсутствующие файлы: {missing}"


def test_promo_module_is_in_the_image():
    """Явная проверка именно того модуля, на котором дефект и проявился."""
    assert "promo.py" in copied_modules()
    assert re.search(r"^\s*from promo import", (RUNTIME / "entities.py").read_text(encoding="utf-8"), re.M)
