"""Editing the control sheet without losing its comments (tomlkit)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit

from .config import Charter


def set_value(path: Path | None, dotted: str, value: Any) -> dict[str, Any]:
    """Set ``a.b.c`` in world.toml, validate the result, write only if it is still a valid charter."""
    if path is None:
        return {"ok": False, "error": "charter has no path"}
    doc = tomlkit.parse(Path(path).read_text(encoding="utf-8"))
    node = doc
    keys = dotted.split(".")
    for k in keys[:-1]:
        if k not in node:
            node[k] = tomlkit.table()
        node = node[k]
    old = node.get(keys[-1])
    node[keys[-1]] = value
    try:
        Charter.model_validate(doc.unwrap())
    except Exception as e:
        return {"ok": False, "error": f"rejected, the sheet would be invalid: {e}"[:400]}
    Path(path).write_text(tomlkit.dumps(doc), encoding="utf-8")
    return {"ok": True, "path": dotted, "old": old if not hasattr(old, "unwrap") else old.unwrap(),
            "new": value, "note": "takes effect when the city restarts"}
