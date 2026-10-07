"""Document processing tools: text extraction, OCR, and conversion.

Lightweight formats (txt/md/html/csv/json) are handled dependency-free; PDF
uses pypdf/PyPDF2/pdfminer when installed; OCR delegates to the Tesseract
manifest tool and document conversion to the Pandoc manifest tool, so each
goes through the same permission and installation gates.
"""
from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

MAX_CHARS = 60000
MAX_FILE_BYTES = 25 * 1024 * 1024


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"):
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(self.chunks))).strip()


def _resolve(workspace: Path, raw: str, *, must_exist: bool = True) -> Path | None:
    p = (workspace / str(raw or "")).resolve()
    if not p.is_relative_to(workspace.resolve()):
        return None
    if must_exist and not p.is_file():
        return None
    return p


def _pdf_text(path: Path) -> str:
    errors: list[str] = []
    try:
        from pypdf import PdfReader  # type: ignore
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pypdf: {exc}")
    try:
        from PyPDF2 import PdfReader  # type: ignore
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        errors.append(f"PyPDF2: {exc}")
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract  # type: ignore
        return str(pdfminer_extract(str(path)))
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pdfminer: {exc}")
    suffix = f" ({'; '.join(errors)})" if errors else ""
    raise ImportError(f"no PDF backend installed — pip install pypdf{suffix}")


def extract_document_text(path: Path, *, max_chars: int = MAX_CHARS) -> dict[str, Any]:
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"file too large ({size} bytes > {MAX_FILE_BYTES})")
    if suffix in (".txt", ".md", ".markdown", ".rst", ".log", ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".cs", ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg"):
        text = path.read_text(encoding="utf-8", errors="replace")
        kind = "text"
    elif suffix in (".html", ".htm"):
        parser = _TextExtractor()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        text, kind = parser.text(), "html"
    elif suffix == ".csv":
        text, kind = _csv_summary(path), "csv"
    elif suffix == ".pdf":
        text, kind = _pdf_text(path), "pdf"
    elif suffix in (".docx", ".pptx", ".epub", ".doc", ".odt"):
        raise ValueError(f"{suffix} requires conversion — use convert_document (pandoc) to produce markdown/text first")
    else:
        raise ValueError(f"unsupported document type: {suffix or '(none)'}")
    return {
        "file": str(path), "kind": kind, "bytes": size,
        "characters": len(text), "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }


def _csv_summary(path: Path, *, sample_rows: int = 50) -> str:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        rows = [row for _, row in zip(range(sample_rows + 1), reader)]
    if not rows:
        return "(empty csv)"
    header, sample = rows[0], rows[1:]
    buf = io.StringIO()
    buf.write(f"columns ({len(header)}): {', '.join(header)}\n")
    buf.write(f"sample rows: {len(sample)}\n\n")
    buf.write("\t".join(header) + "\n")
    for row in sample:
        buf.write("\t".join(row) + "\n")
    return buf.getvalue()


def register_document_tools(registry: ToolRegistry, workspace: Path, *, jobs=None) -> None:

    def extract_text(args: dict[str, Any]) -> str:
        path = _resolve(workspace, str(args.get("file", "")))
        if path is None:
            return "ERROR: file must exist inside the workspace"
        try:
            return json.dumps(extract_document_text(path), ensure_ascii=False)
        except (ValueError, ImportError) as exc:
            return f"ERROR: {exc}"

    def ocr_image(args: dict[str, Any]) -> str:
        path = _resolve(workspace, str(args.get("image", "")))
        if path is None:
            return "ERROR: image must exist inside the workspace"
        targs: list[str] = []
        if args.get("language"):
            targs += ["-l", str(args["language"])]
        if args.get("tsv"):
            targs.append("tsv")
        return registry.execute("tesseract", {
            "image": str(path), "args": targs,
        }, approved=bool(args.get("approved", False)))

    def convert_document(args: dict[str, Any]) -> str:
        src = _resolve(workspace, str(args.get("input", "")))
        dst = _resolve(workspace, str(args.get("output", "")), must_exist=False)
        if src is None:
            return "ERROR: input must exist inside the workspace"
        if dst is None:
            return "ERROR: output must stay inside the workspace"
        pargs: list[str] = []
        if args.get("from"):
            pargs += ["-f", str(args["from"])]
        if args.get("to"):
            pargs += ["-t", str(args["to"])]
        return registry.execute("pandoc", {
            "input": str(src), "output": str(dst), "args": pargs,
        }, approved=bool(args.get("approved", False)))

    registry.register(ToolSpec(
        "extract_text",
        "Extract readable text from a document (txt/md/html/csv/json/pdf). CSV returns columns + sample; HTML is tag-stripped; PDF uses an installed python backend.",
        {
            "type": "object",
            "properties": {"file": {"type": "string"}},
            "required": ["file"],
        },
        "filesystem.read", extract_text,
        category="documents",
        capabilities=["extract_text", "read_document", "read_pdf", "parse_csv", "html_to_text", "summarize_document_input"],
    ))
    registry.register(ToolSpec(
        "ocr_image",
        "OCR an image/screenshot to text via Tesseract (delegates through the registry — install via Tool Manager if missing).",
        {
            "type": "object",
            "properties": {
                "image": {"type": "string"},
                "language": {"type": "string", "description": "tesseract language code, e.g. eng"},
                "tsv": {"type": "boolean", "default": False, "description": "return TSV with text regions"},
            },
            "required": ["image"],
        },
        "filesystem.read", ocr_image,
        category="documents",
        capabilities=["ocr_image", "extract_text_from_screenshot", "detect_text_regions"],
    ))
    registry.register(ToolSpec(
        "convert_document",
        "Convert a document between formats via Pandoc (md/docx/html/epub/latex...). Delegates through the registry.",
        {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "output": {"type": "string"},
                "from": {"type": "string", "description": "input format override"},
                "to": {"type": "string", "description": "output format override"},
            },
            "required": ["input", "output"],
        },
        "filesystem.write", convert_document,
        category="documents",
        capabilities=["convert_document", "markdown_to_docx", "markdown_to_html", "docx_to_markdown"],
    ))
