#!/usr/bin/env python3
"""Сверка состава Terraform-плана с ожидаемым — до apply.

Зачем: `infra.yml apply` пересчитывает план заново, поэтому просмотр прежнего `plan` не
гарантирует, что apply сделает то же самое. Guard читает `terraform show -json <planfile>`
и требует, чтобы множество изменяемых адресов и их действий совпало с ожидаемым ТОЧНО.
Тогда apply применяет именно этот сохранённый планфайл, а не новый расчёт.

Ожидание — строка `адрес=действие,адрес=действие`, действие:
create | update | delete | replace (delete+create в любом порядке).
Ресурсы с no-op и read (data sources) не учитываются.

Только чтение: файл плана и аргументы. Код выхода 0 — совпало, 1 — расхождение, 2 — ошибка ввода.
"""
from __future__ import annotations

import json
import sys

ALLOWED = {"create", "update", "delete", "replace"}


def parse_expected(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"ожидается адрес=действие, получено {item!r}")
        addr, action = (x.strip() for x in item.rsplit("=", 1))
        if action not in ALLOWED:
            raise ValueError(f"неизвестное действие {action!r} у {addr}")
        if addr in out:
            raise ValueError(f"адрес указан дважды: {addr}")
        out[addr] = action
    if not out:
        raise ValueError("пустое ожидание")
    return out


def classify(actions: list[str]) -> str | None:
    a = sorted(actions)
    if a in (["no-op"], ["read"]):
        return None
    if a == ["create", "delete"]:
        return "replace"
    if len(a) == 1 and a[0] in ALLOWED:
        return a[0]
    return "+".join(actions)


def actual_changes(plan: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for rc in plan.get("resource_changes", []):
        kind = classify(rc["change"]["actions"])
        if kind is not None:
            out[rc["address"]] = kind
    return out


def compare(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    problems = []
    for addr in sorted(set(expected) | set(actual)):
        e, a = expected.get(addr), actual.get(addr)
        if e is None:
            problems.append(f"НЕОЖИДАННО: {addr} = {a}")
        elif a is None:
            problems.append(f"НЕТ В ПЛАНЕ: {addr} (ожидалось {e})")
        elif e != a:
            problems.append(f"ДРУГОЕ ДЕЙСТВИЕ: {addr} = {a}, ожидалось {e}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: tf_plan_guard.py <plan.json> <адрес=действие,...>", file=sys.stderr)
        return 2
    try:
        expected = parse_expected(argv[2])
        with open(argv[1], encoding="utf-8") as fh:
            plan = json.load(fh)
    except (ValueError, OSError) as e:
        print(f"PLAN GUARD: ошибка ввода — {e}", file=sys.stderr)
        return 2
    actual = actual_changes(plan)
    for addr in sorted(actual):
        print(f"PLAN: {addr} = {actual[addr]}")
    problems = compare(expected, actual)
    if problems:
        for p in problems:
            print(f"PLAN GUARD: {p}", file=sys.stderr)
        print("PLAN GUARD: состав плана не совпал с ожидаемым — apply не выполняется.", file=sys.stderr)
        return 1
    print(f"PLAN GUARD: OK — {len(actual)} изменений ровно как ожидалось.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
