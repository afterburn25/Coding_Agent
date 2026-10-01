"""Local data tools: SQL over CSV/JSON/SQLite (DuckDB when installed) and
dependency-free SVG chart generation.

DuckDB CLI is preferred for large/analytical sources (Parquet, big CSV); the
stdlib fallback loads CSV/JSON into in-memory SQLite so the agent can still
answer questions offline with no extra installs.
"""
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

MAX_ROWS = 5000
MAX_CELL = 2000


def find_duckdb() -> str | None:
    return shutil.which("duckdb") or shutil.which("duckdb.exe")


def _resolve(workspace: Path, raw: str) -> Path | None:
    p = (workspace / str(raw or "")).resolve()
    return p if p.is_relative_to(workspace) else None


def _load_into_sqlite(source: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ext = source.suffix.lower()
    if ext == ".csv":
        with open(source, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            header = [h.strip() or f"col_{i}" for i, h in enumerate(next(reader, []))]
            cols = ", ".join(f'"{h}" TEXT' for h in header)
            conn.execute(f'CREATE TABLE "data" ({cols})')
            marks = ", ".join("?" for _ in header)
            conn.executemany(f'INSERT INTO "data" VALUES ({marks})', reader)
        conn.commit()
        return conn
    if ext in {".json", ".jsonl", ".ndjson"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        rows: list[dict[str, Any]]
        if ext == ".json":
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else [parsed]
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        keys = sorted({k for row in rows if isinstance(row, dict) for k in row})
        cols = ", ".join(f'"{k}" TEXT' for k in keys)
        col_spec = cols or '"empty" TEXT'
        conn.execute(f'CREATE TABLE "data" ({col_spec})')
        if keys:
            marks = ", ".join("?" for _ in keys)
            conn.executemany(
                f'INSERT INTO "data" VALUES ({marks})',
                [[str(r.get(k, "")) if not isinstance(r.get(k), (dict, list)) else json.dumps(r.get(k)) for k in keys]
                 for r in rows if isinstance(r, dict)],
            )
        conn.commit()
        return conn
    if ext in {".db", ".sqlite", ".sqlite3"}:
        return sqlite3.connect(str(source))
    raise ValueError(f"unsupported fallback source '{ext}' — install duckdb for parquet/large analytics")


def _rows_to_json(cur: sqlite3.Cursor, limit: int) -> list[dict[str, Any]]:
    cols = [d[0] for d in (cur.description or [])]
    rows = []
    for i, row in enumerate(cur.fetchmany(limit)):
        rows.append({c: (str(v)[:MAX_CELL] if v is not None else None) for c, v in zip(cols, row)})
    return rows


def _duckdb_query(workspace: Path, source: Path | None, sql: str, limit: int) -> dict[str, Any]:
    exe = find_duckdb()
    db_arg = str(source) if source and source.suffix.lower() in {".duckdb", ".ddb"} else ":memory:"
    if source and source.suffix.lower() not in {".duckdb", ".ddb"}:
        reader = {".csv": "read_csv_auto", ".parquet": "read_parquet", ".json": "read_json_auto"}.get(source.suffix.lower())
        if reader:
            sql = sql.replace("{source}", f"{reader}('{str(source).replace(chr(39), chr(39)*2)}')")
    argv = [exe, db_arg, "-json", "-c", f"{sql} LIMIT {limit}" if "limit" not in sql.lower() else sql]
    proc = subprocess.run(argv, cwd=str(workspace), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "duckdb failed")[-500:])
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        rows = [{"output": proc.stdout[:MAX_ROWS * 100]}]
    return {"engine": "duckdb", "rows": rows[:limit]}


def query_source(workspace: Path, source_raw: str, sql: str, *, limit: int = 100) -> dict[str, Any]:
    source = _resolve(workspace, source_raw) if source_raw else None
    if source_raw and (source is None or not source.is_file()):
        raise ValueError(f"source must be a file inside the workspace: {source_raw}")
    if find_duckdb() and (source is None or source.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}):
        return _duckdb_query(workspace, source, sql, limit)
    if source is None:
        raise ValueError("a source file is required without duckdb installed")
    conn = _load_into_sqlite(source)
    try:
        cur = conn.execute(sql)
        return {"engine": "sqlite", "rows": _rows_to_json(cur, limit), "row_limit": limit}
    finally:
        conn.close()


def profile_source(workspace: Path, source_raw: str, *, sample: int = 1000) -> dict[str, Any]:
    source = _resolve(workspace, source_raw)
    if source is None or not source.is_file():
        raise ValueError(f"source must be a file inside the workspace: {source_raw}")
    if find_duckdb() and source.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        s = str(source).replace("'", "''")
        ext = source.suffix.lower()
        reader = {".csv": "read_csv_auto", ".parquet": "read_parquet", ".json": "read_json_auto"}.get(ext)
        if not reader:
            raise ValueError(f"unsupported source '{ext}'")
        exe = find_duckdb()
        describe = subprocess.run([exe, ":memory:", "-json", "-c", f"DESCRIBE SELECT * FROM {reader}('{s}')"],
                                  cwd=str(workspace), capture_output=True, text=True, timeout=120)
        count = subprocess.run([exe, ":memory:", "-json", "-c", f"SELECT COUNT(*) AS n FROM {reader}('{s}')"],
                               cwd=str(workspace), capture_output=True, text=True, timeout=300)
        return {
            "engine": "duckdb", "source": source.name,
            "size_bytes": source.stat().st_size,
            "columns": json.loads(describe.stdout or "[]"),
            "row_count": (json.loads(count.stdout or "[{}]")[0].get("n") if count.returncode == 0 else None),
        }
    conn = _load_into_sqlite(source)
    try:
        info = conn.execute('PRAGMA table_info("data")').fetchall() if source.suffix.lower() != ".db" else None
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        out: dict[str, Any] = {"engine": "sqlite", "source": source.name, "size_bytes": source.stat().st_size, "tables": tables}
        if info:
            out["columns"] = [{"name": c[1], "type": c[2]} for c in info]
            out["row_count"] = conn.execute('SELECT COUNT(*) FROM "data"').fetchone()[0]
            stats = []
            for c in info:
                name = c[1]
                try:
                    r = conn.execute(
                        f'SELECT COUNT("{name}"), SUM("{name}" IS NULL OR "{name}"=\'\'), MIN("{name}"), MAX("{name}") FROM "data"'
                    ).fetchone()
                    stats.append({"column": name, "non_null": r[0], "nullish": r[1], "min": str(r[2])[:80], "max": str(r[3])[:80]})
                except sqlite3.Error:
                    continue
            out["column_stats"] = stats
        return out
    finally:
        conn.close()


# -- dependency-free SVG charts ------------------------------------------------

def _svg_header(w: int, h: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="ui-monospace,Consolas,monospace" font-size="11">'


def render_chart(spec: dict[str, Any], out: Path) -> Path:
    kind = str(spec.get("type", "line")).lower()
    labels = [str(x) for x in spec.get("labels") or spec.get("x") or []]
    values = [float(v) for v in spec.get("values") or spec.get("y") or []]
    if not values:
        raise ValueError("chart requires non-empty values/y")
    title = str(spec.get("title") or "")[:120]
    w, h = int(spec.get("width", 720)), int(spec.get("height", 360))
    pad = 46
    cw, ch = w - 2 * pad, h - 2 * pad
    vmax, vmin = max(values), min(values)
    span = (vmax - vmin) or 1.0
    n = len(values)
    parts = [_svg_header(w, h), '<rect width="100%" height="100%" fill="#0d1117"/>']
    parts.append(f'<text x="{w/2}" y="20" fill="#e8eefb" text-anchor="middle" font-size="13">{title}</text>')
    parts.append(f'<line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" stroke="#303744"/>')
    parts.append(f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h-pad}" stroke="#303744"/>')
    parts.append(f'<text x="{pad-4}" y="{pad+4}" fill="#77839a" text-anchor="end">{vmax:g}</text>')
    parts.append(f'<text x="{pad-4}" y="{h-pad}" fill="#77839a" text-anchor="end">{vmin:g}</text>')

    def px(i: int) -> float:
        return pad + (cw * i / max(n - 1, 1))

    def py(v: float) -> float:
        return h - pad - (ch * (v - vmin) / span)

    if kind == "line":
        pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#58a6ff" stroke-width="1.6"/>')
    elif kind == "scatter":
        for i, v in enumerate(values):
            parts.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" fill="#58a6ff"/>')
    elif kind in {"bar", "histogram"}:
        bw = cw / n * 0.8
        for i, v in enumerate(values):
            x = pad + cw * i / n + (cw / n - bw) / 2
            y = py(max(v, 0)) if vmin < 0 else py(v)
            hh = abs(py(0 if vmin < 0 else vmin) - py(v))
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{hh:.1f}" fill="#58a6ff"/>')
    elif kind == "pie":
        total = sum(abs(v) for v in values) or 1.0
        cx, cy, r = w / 2, h / 2, min(cw, ch) / 2
        a0 = -math.pi / 2
        colors = ["#58a6ff", "#3fb950", "#f0cd8a", "#ff97a5", "#c4a5ff", "#7fd4e8", "#f0883e", "#8b949e"]
        for i, v in enumerate(values):
            frac = abs(v) / total
            a1 = a0 + frac * 2 * math.pi
            large = 1 if frac > 0.5 else 0
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            parts.append(f'<path d="M{cx},{cy} L{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f} Z" fill="{colors[i % len(colors)]}" stroke="#0d1117"/>')
            mid = (a0 + a1) / 2
            lx, ly = cx + (r + 18) * math.cos(mid), cy + (r + 18) * math.sin(mid)
            label = labels[i] if i < len(labels) else str(i)
            parts.append(f'<text x="{lx:.0f}" y="{ly:.0f}" fill="#aeb7c6" text-anchor="middle" font-size="9">{label}</text>')
            a0 = a1
    else:
        raise ValueError(f"unsupported chart type '{kind}' (line|bar|histogram|scatter|pie)")
    if kind != "pie":
        step = max(1, n // 8)
        for i in range(0, n, step):
            label = labels[i] if i < len(labels) else str(i)
            parts.append(f'<text x="{px(i):.1f}" y="{h-pad+14}" fill="#77839a" text-anchor="middle" font-size="9">{label[:12]}</text>')
    parts.append("</svg>")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def register_data_tools(registry: ToolRegistry, workspace: Path, *, artifacts_dir: Path | None = None) -> None:
    artifacts = Path(artifacts_dir or (workspace / ".agent" / "charts"))
    artifacts.mkdir(parents=True, exist_ok=True)

    def data_query(args: dict[str, Any]) -> str:
        try:
            result = query_source(workspace, str(args.get("source", "") or ""), str(args["sql"]),
                                  limit=max(1, min(int(args.get("limit", 100)), MAX_ROWS)))
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        return json.dumps(result, ensure_ascii=False)

    def profile_dataset(args: dict[str, Any]) -> str:
        try:
            result = profile_source(workspace, str(args["source"]))
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        return json.dumps(result, ensure_ascii=False)

    def chart_generate(args: dict[str, Any]) -> str:
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(args.get("name", "chart")))[:60]
        out = artifacts / f"{name}-{int(time.time())}.svg"
        try:
            render_chart(args, out)
        except (ValueError, KeyError) as exc:
            return f"ERROR: {exc}"
        return json.dumps({"ok": True, "chart": str(out), "type": args.get("type", "line")}, ensure_ascii=False)

    registry.register(ToolSpec(
        "data_query",
        "Run SQL over a local dataset file (csv/json/jsonl/db/sqlite; parquet via duckdb when installed). Table name is 'data' for file sources. Returns JSON rows.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "workspace-relative dataset path; optional with duckdb"},
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["sql"],
        },
        "filesystem.read",
        data_query,
        category="data",
        capabilities=["query_csv", "query_json", "query_database", "execute_sql", "summarize_table"],
    ))
    registry.register(ToolSpec(
        "profile_dataset",
        "Profile a dataset file: row count, columns, types, null counts, min/max. Use before writing analysis SQL.",
        {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]},
        "filesystem.read",
        profile_dataset,
        category="data",
        capabilities=["profile_dataset", "summarize_table"],
    ))
    registry.register(ToolSpec(
        "chart_generate",
        "Render a chart (line|bar|histogram|scatter|pie) from labels/values into an SVG artifact. Use with summarized data from data_query.",
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["line", "bar", "histogram", "scatter", "pie"], "default": "line"},
                "title": {"type": "string"},
                "labels": {"type": "array"},
                "values": {"type": "array"},
                "x": {"type": "array"},
                "y": {"type": "array"},
                "name": {"type": "string"},
                "width": {"type": "integer", "default": 720},
                "height": {"type": "integer", "default": 360},
            },
        },
        "filesystem.read",
        chart_generate,
        category="data",
        capabilities=["chart_generate", "visualization"],
    ))
