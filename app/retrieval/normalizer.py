from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[0-9a-zа-я_-]+", re.IGNORECASE)


def normalize_query(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.casefold().replace("ё", "е")))


def fts_expression(tokens: list[str]) -> str:
    cleaned = [token.replace('"', "") for token in tokens if token]
    return " OR ".join(f'"{token}"' for token in cleaned[:30])
