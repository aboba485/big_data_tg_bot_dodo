from __future__ import annotations

from typing import Any

from app.documentation.models import ResponseField


def flatten_schema(
    schema: dict[str, Any] | None,
    *,
    max_depth: int = 15,
) -> list[ResponseField]:
    fields: list[ResponseField] = []

    def visit(node: Any, path: str, depth: int, ancestors: set[int]) -> None:
        if not isinstance(node, dict) or depth > max_depth or id(node) in ancestors:
            return
        current = ancestors | {id(node)}
        variants = [
            item
            for keyword in ("allOf", "oneOf", "anyOf")
            for item in node.get(keyword, [])
            if isinstance(item, dict)
        ]
        for variant in variants:
            visit(variant, path, depth + 1, current)
        node_type = node.get("type", "")
        if path:
            fields.append(
                ResponseField(
                    path=path,
                    type=str(node_type or ("object" if "properties" in node else "")),
                    description=str(node.get("description") or ""),
                    enum=list(node.get("enum") or []),
                    format=str(node.get("format") or ""),
                    nullable=bool(node.get("nullable", False)),
                )
            )
        properties = node.get("properties") or {}
        if isinstance(properties, dict):
            for name, child in properties.items():
                child_path = f"{path}.{name}" if path else name
                visit(child, child_path, depth + 1, current)
        items = node.get("items")
        if isinstance(items, dict):
            array_path = f"{path}[]" if path else "[]"
            visit(items, array_path, depth + 1, current)

    visit(schema, "", 0, set())
    unique: dict[str, ResponseField] = {}
    for field in fields:
        unique.setdefault(field.path, field)
    return list(unique.values())
