"""Global search — one endpoint aggregating the workstation's stores.

`/api/search?q=…` fans a case-insensitive query across tasks, missions,
projects, skills, saved answers, knowledge entities, queued work,
dev servers and workspace file names. Every source is isolated behind
its own try/except: one failing store degrades that group, never the
whole palette. Results carry `kind`, `title`, `detail`, `ref` (a page
URL the Command-K palette navigates to) and a simple rank score.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_FILE_SKIP_DIRS = {
    ".git", ".agent", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "data", "models", ".runtime",
}


class GlobalSearch:
    def __init__(self, state: Any) -> None:
        self.state = state

    @staticmethod
    def _score(query: str, *fields: str) -> int:
        """0 = no match; higher = better. Exact > prefix > substring."""
        q = query.lower()
        best = 0
        for f in fields:
            if not f:
                continue
            hay = str(f).lower()
            if hay == q:
                best = max(best, 100)
            elif hay.startswith(q):
                best = max(best, 70)
            elif q in hay:
                # earlier hits rank slightly higher
                best = max(best, 50 - min(hay.index(q), 20))
        return best

    @staticmethod
    def _item(kind: str, title: str, detail: str, ref: str,
              score: int) -> dict:
        return {"kind": kind, "title": str(title)[:160],
                "detail": str(detail)[:200], "ref": ref, "score": score}

    def query(self, q: str, *, limit: int = 40,
              per_source: int = 6) -> dict:
        q = (q or "").strip()
        if not q:
            return {"query": q, "results": []}
        results: list[dict] = []
        for source in (self._tasks, self._missions, self._projects,
                       self._skills, self._answers, self._knowledge,
                       self._queue, self._devservers, self._files):
            try:
                results.extend(source(q, per_source))
            except Exception:
                continue
        results.sort(key=lambda r: -r["score"])
        return {"query": q, "results": results[:limit]}

    # ------------------------------------------------------------------
    def _tasks(self, q: str, n: int) -> list[dict]:
        out = []
        for t in self.state.tasks.recent(200):
            score = self._score(q, t.get("prompt") or "",
                                t.get("summary") or "")
            if score:
                out.append(self._item(
                    "task", (t.get("prompt") or "")[:120],
                    f"{t.get('status','?')} · {t.get('id','')}",
                    f"/missions.html", score))
        return sorted(out, key=lambda r: -r["score"])[:n]

    def _missions(self, q: str, n: int) -> list[dict]:
        out = []
        try:
            rows = self.state.autonomy.missions.list()
        except Exception:
            return out
        for m in rows:
            score = self._score(q, m.get("title") or "",
                                m.get("prompt") or "",
                                m.get("id") or "")
            if score:
                out.append(self._item(
                    "mission", m.get("title") or m.get("prompt") or m["id"],
                    f"{m.get('status','?')} · {m.get('id','')}",
                    "/missions.html", score))
        return sorted(out, key=lambda r: -r["score"])[:n]

    def _projects(self, q: str, n: int) -> list[dict]:
        out = []
        try:
            rows = self.state.projects.list()
        except Exception:
            return out
        for p in rows:
            score = self._score(q, p.get("name") or "",
                                p.get("summary") or "",
                                p.get("root") or "")
            if score:
                out.append(self._item(
                    "project", p.get("name") or "?",
                    p.get("root") or "", "/projects.html", score))
        return sorted(out, key=lambda r: -r["score"])[:n]

    def _skills(self, q: str, n: int) -> list[dict]:
        out = []
        for s in self.state.skills.list():
            score = self._score(q, s.get("name") or "",
                                s.get("description") or "")
            if score:
                out.append(self._item(
                    "skill", s.get("name") or "?",
                    s.get("description") or "", "/tools.html", score))
        return sorted(out, key=lambda r: -r["score"])[:n]

    def _answers(self, q: str, n: int) -> list[dict]:
        out = []
        try:
            rows = self.state.answer_memory.list_answers(
                query=q, limit=n) or []
        except Exception:
            return out
        for a in rows:
            if isinstance(a, dict):
                q_text = a.get("question") or a.get("query") or ""
                a_text = a.get("answer") or a.get("response") or ""
                aid = a.get("id") or ""
                score = self._score(q, q_text, a_text) or 30
                out.append(self._item(
                    "answer", q_text or "(saved answer)",
                    a_text, "/answers.html", score))
        return out[:n]

    def _knowledge(self, q: str, n: int) -> list[dict]:
        out = []
        kg = self.state.knowledge  # lazy — may be None after failures
        if kg is None:
            return out
        try:
            rows = kg.find_entities(name_like=q, limit=n)
        except Exception:
            return out
        for e in rows:
            out.append(self._item(
                "knowledge", e.get("name") or "?",
                f"{e.get('kind','entity')}", "/projects.html",
                self._score(q, e.get("name") or "") or 30))
        return out[:n]

    def _queue(self, q: str, n: int) -> list[dict]:
        out = []
        for item in self.state.queue.list():
            prompt = item.get("prompt") or ""
            score = self._score(q, prompt, item.get("id") or "")
            if score:
                out.append(self._item(
                    "queue", prompt[:120],
                    f"position {item.get('position','?')}",
                    "/command.html", score))
        return out[:n]

    def _devservers(self, q: str, n: int) -> list[dict]:
        out = []
        try:
            rows = self.state.devservers.list(refresh=False)
        except Exception:
            return out
        for s in rows:
            score = self._score(q, s.get("name") or "",
                                s.get("command") or "",
                                s.get("url") or "")
            if score:
                out.append(self._item(
                    "devserver", s.get("name") or s.get("command") or "?",
                    s.get("url") or s.get("status") or "",
                    "/workspace.html", score))
        return out[:n]

    def _files(self, q: str, n: int) -> list[dict]:
        out = []
        root = Path(self.state.workspace)
        ql = q.lower()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _FILE_SKIP_DIRS and not d.startswith(".")]
            depth = Path(dirpath).relative_to(root).parts
            if len(depth) > 4:
                dirnames[:] = []
                continue
            for fn in filenames:
                if ql in fn.lower():
                    rel = (Path(dirpath) / fn).relative_to(root).as_posix()
                    out.append(self._item(
                        "file", rel, "", "/workspace.html",
                        self._score(q, fn) or 20))
                    if len(out) >= n:
                        return out
            if len(out) >= n:
                return out
        return out
