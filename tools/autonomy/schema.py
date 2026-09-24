"""Строгий валидатор подмножества JSON Schema (draft 2020-12). Только stdlib.

Почему не `jsonschema`: CI ставит зависимости с проверкой хешей только из wheel, а
`jsonschema` тянет компилируемый `rpds-py`. Подмножество ниже покрывает все схемы
`quality/autonomy/*.schema.json`, а неизвестное ключевое слово — ОШИБКА, а не
игнорирование: схема не может молча потребовать того, чего валидатор не проверяет.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ANNOTATIONS = {"$schema", "$id", "$comment", "title", "description", "examples", "default"}
SUPPORTED = ANNOTATIONS | {
    "type", "required", "properties", "additionalProperties", "enum", "const",
    "pattern", "minLength", "maxLength", "minItems", "maxItems", "items",
    "minimum", "maximum", "format", "uniqueItems",
}
TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


class SchemaError(ValueError):
    """Схема использует то, что валидатор не умеет проверять."""


def _is_type(value, t: str) -> bool:
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[t])


def _date_time(s: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", s):
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """Вернуть список нарушений. Пустой список — экземпляр валиден."""
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: неподдерживаемые ключевые слова схемы {sorted(unknown)}")
    errors: list[str] = []

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, t) for t in types):
            return [f"{path}: ожидался тип {types}, получен {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: ожидалось значение {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} не входит в {schema['enum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: короче {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: длиннее {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: не соответствует шаблону {schema['pattern']}")
        if schema.get("format") == "date-time" and not _date_time(instance):
            errors.append(f"{path}: не RFC 3339 date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: меньше {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: больше {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: элементов меньше {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: элементов больше {schema['maxItems']}")
        if schema.get("uniqueItems"):
            seen = [json.dumps(x, sort_keys=True) for x in instance]
            if len(seen) != len(set(seen)):
                errors.append(f"{path}: элементы не уникальны")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], f"{path}[{i}]")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: нет обязательного поля {key!r}")
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                errors += validate(value, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: неизвестное поле {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += validate(value, schema["additionalProperties"], f"{path}.{key}")
    return errors


SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "quality" / "autonomy"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def require_valid(instance, name: str) -> None:
    """Fail-closed: невалидный документ не проходит дальше ни при каких условиях."""
    errors = validate(instance, load_schema(name))
    if errors:
        raise ValueError(f"{name}: документ невалиден:\n  " + "\n  ".join(errors[:20]))


def check_schema(schema: dict, path: str = "$") -> None:
    """Рекурсивно проверить, что ВСЁ дерево схемы использует только поддерживаемое."""
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: неподдерживаемые ключевые слова схемы {sorted(unknown)}")
    for key, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{path}.properties.{key}")
    if isinstance(schema.get("items"), dict):
        check_schema(schema["items"], f"{path}.items")
    if isinstance(schema.get("additionalProperties"), dict):
        check_schema(schema["additionalProperties"], f"{path}.additionalProperties")
