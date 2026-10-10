"""The knowledge center: SQLite FTS5 (BM25) over the second brain and the world's own work.

Every crossing from residential to work passes through here (the canon):
on the way in an agent receives a *capsule*, a deterministic, size-capped
digest of what is already known; on the way out it *deposits* what it made.
A capsule is a pure function of the index and the query, so the same job gets
the same context and the model-answer cache stays warm.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from .store import Store

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
_STOP = set("the and for with that this from into over have has was were are not but you your our "
            "his her its their they them then than also can will would should could about which what "
            "when where who how why all any each more most some such only very just".split())


def _fts_query(text: str, limit: int = 12) -> str:
    seen: list[str] = []
    for w in _WORD.findall(text.lower()):
        if w not in _STOP and w not in seen:
            seen.append(w)
        if len(seen) >= limit:
            break
    return " OR ".join(f'"{w}"' for w in seen)


class KnowledgeCenter:
    def __init__(self, store: Store, vault_paths: list[str] | None = None):
        self.store = store
        self.vaults = [Path(os.path.expanduser(p)) for p in (vault_paths or [])]

    def index_vaults(self) -> dict[str, int]:
        """Incremental: only files whose content digest changed are rewritten."""
        added = skipped = 0
        known = {r["path"]: r["digest"] for r in self.store.query(
            "SELECT path, digest FROM docs WHERE source='vault'")}
        present = set()
        for root in self.vaults:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*.md")):
                if any(part.startswith(".") for part in f.relative_to(root).parts):
                    continue
                try:
                    body = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                path = str(f)
                present.add(path)
                dig = hashlib.sha256(body.encode()).hexdigest()[:16]
                if known.get(path) == dig:
                    skipped += 1
                    continue
                self.store.execute("DELETE FROM docs WHERE path=?", (path,))
                self.store.execute("INSERT INTO docs(path,title,body,source,digest) VALUES(?,?,?,?,?)",
                                   (path, f.stem, body, "vault", dig))
                added += 1
        gone = [p for p in known if p not in present]
        for p in gone:
            self.store.execute("DELETE FROM docs WHERE path=?", (p,))
        return {"indexed": added, "unchanged": skipped, "removed": len(gone)}

    def deposit(self, title: str, body: str, source: str = "world", path: str | None = None) -> str:
        dig = hashlib.sha256(body.encode()).hexdigest()[:16]
        path = path or f"world://{source}/{dig}"
        self.store.execute("DELETE FROM docs WHERE path=?", (path,))
        self.store.execute("INSERT INTO docs(path,title,body,source,digest) VALUES(?,?,?,?,?)",
                           (path, title, body, source, dig))
        return path

    def search(self, text: str, k: int = 5) -> list[dict[str, Any]]:
        q = _fts_query(text)
        if not q:
            return []
        rows = self.store.query(
            "SELECT path, title, source, snippet(docs, 2, '[', ']', ' … ', 24) AS snippet, "
            "bm25(docs) AS score FROM docs WHERE docs MATCH ? ORDER BY score, path LIMIT ?", (q, k))
        return rows

    def capsule(self, topic: str, max_chars: int = 3000, k: int = 6) -> tuple[str, str]:
        """(context text, digest). Deterministic for a given index state."""
        parts: list[str] = []
        used = 0
        for r in self.search(topic, k):
            line = f"- {r['title']} ({r['source']}): {r['snippet']}"
            if used + len(line) > max_chars:
                break
            parts.append(line)
            used += len(line)
        text = "\n".join(parts)
        return text, hashlib.sha256(text.encode()).hexdigest()[:12]

    def count(self) -> int:
        return self.store.one("SELECT COUNT(*) n FROM docs")["n"]
