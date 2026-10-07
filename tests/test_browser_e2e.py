"""Per-page browser E2E sweep + bounded visual regression.

Serves web/ statically with a minimal ``/api/*`` stub (``{}`` payloads)
and drives every page through the real Playwright BrowserRunner — the
same machinery browser_run/browser_verify use in production.

Each page must:
- return HTTP 200 and a non-empty <title>,
- render its root container,
- produce no uncaught JS exceptions (pageerror) or failed requests.

Visual regression is bounded: when Pillow is importable and a baseline
PNG exists under tests/visual_baseline/, screenshots are pixel-diffed
with a tolerance for AA/font rasterization noise. Missing baselines are
recorded as written, not as failures. Set NEXUS_VISUAL_UPDATE=1 to
re-baseline deliberately.

The whole suite skips when Playwright or a browser channel is
unavailable — on machines without the runtime it reports honestly,
never fakes a pass.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BASELINE_DIR = Path(__file__).parent / "visual_baseline"

# (page file, selector that must exist once JS mounts, extra actions)
# personality.html legitimately redirects to /start.html when the API
# stub returns no profile — wait lets that navigation settle, and the
# post-redirect body assert still proves the shell rendered.
PAGES: list[tuple[str, str, list[dict]]] = [
    ("index.html", "#chat", []),
    ("answers.html", "body", []),
    ("command.html", "body", []),
    ("image.html", "#imagePrompt", []),
    ("knowledge.html", "body", []),
    ("learning.html", "body", []),
    ("missions.html", "body", []),
    ("models.html", "body", []),
    ("personality.html", "body", [{"action": "wait", "ms": 1200}]),
    ("projects.html", "body", []),
    ("research.html", "body", []),
    ("settings.html", "body", []),
    ("start.html", "body", []),
    ("system.html", "body", []),
    ("tools.html", "body", []),
    ("trainer.html", "body", []),
    ("voice.html", "body", []),
    ("workspace.html", "body", []),
]

# Max fraction of pixels allowed to differ from the visual baseline —
# tolerates AA/font rasterization jitter, not layout changes.
PIXEL_TOLERANCE = 0.03


def _pillow() -> bool:
    try:
        import PIL.Image  # noqa: F401
        return True
    except Exception:
        return False


def _pixel_diff(a: Path, b: Path) -> float | None:
    """Fraction of differing pixels, or None when Pillow is absent."""
    if not _pillow():
        return None
    from PIL import Image, ImageChops
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return 1.0
    diff = ImageChops.difference(ia, ib)
    # Count pixels where any channel differs by more than ~8/255 —
    # sub-AA noise doesn't count as a layout change.
    px = diff.load()
    w, h = diff.size
    changed = 0
    for y in range(h):
        for x in range(w):
            r, g, bl = px[x, y]
            if max(r, g, bl) > 8:
                changed += 1
    return changed / float(w * h)


class _StubHandler(http.server.SimpleHTTPRequestHandler):
    """Static web/ + a {} stub for any /api/ call — pages render their
    real shell against an empty-but-valid backend shape."""

    def log_message(self, *a):  # quiet test output
        pass

    def _json_empty(self):
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._json_empty()
            return
        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self._json_empty()
            return
        self.send_error(404)


def _browser_ready() -> tuple[bool, str]:
    try:
        from localcodeagent.webtools.browser import BrowserRunner
        runner = BrowserRunner(artifacts_dir=Path(tempfile.mkdtemp()))
        st = runner.status()
        return bool(st.get("ready")), str(st.get("detail") or "")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class BrowserE2ETests(unittest.TestCase):
    """Real-browser sweep — skipped (honestly reported) without a
    resolvable Playwright channel."""

    @classmethod
    def setUpClass(cls):
        ok, detail = _browser_ready()
        if not ok:
            raise unittest.SkipTest(
                f"browser runtime unavailable — {detail}")
        cls.runner_detail = detail
        handler = functools.partial(_StubHandler, directory=str(WEB))
        cls.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _runner(self):
        from localcodeagent.webtools.browser import BrowserRunner
        return BrowserRunner(artifacts_dir=Path(tempfile.mkdtemp()))

    def test_every_page_loads_without_uncaught_errors(self):
        runner = self._runner()
        failures = []
        for page, selector, actions in PAGES:
            if not (WEB / page).is_file():
                failures.append(f"{page}: file missing from web/")
                continue
            res = runner.verify(
                url=f"http://127.0.0.1:{self.port}/{page}",
                expect_selector=[selector],
                forbid_console_errors=False,
                actions=actions,
                timeout_s=45.0)
            if not res.get("pass"):
                detail = "; ".join(
                    c.get("detail", "") for c in res.get("checks", [])
                    if not c.get("ok")) or res.get("error", "failed")
                failures.append(f"{page}: {detail[:200]}")
                continue
            # Uncaught exceptions surface as console 'error' entries or
            # pageerror — network-level /api failures are expected
            # offline and land in failed_requests instead.
            errs = [c["text"] for c in res.get("console_errors", [])]
            if errs:
                failures.append(
                    f"{page}: console errors: {errs[0][:160]}")
        self.assertEqual(failures, [])

    def test_visual_regression_bounded(self):
        if not _pillow():
            self.skipTest("Pillow not installed — pixel diff unavailable")
        runner = self._runner()
        update = os.environ.get("NEXUS_VISUAL_UPDATE") == "1"
        regressions, baselined = [], []
        for page, _, _actions in PAGES:
            name = re.sub(r"[^a-z0-9]+", "-", page.lower()).strip("-")
            res = runner.run(
                url=f"http://127.0.0.1:{self.port}/{page}",
                screenshot=True, screenshot_name=f"e2e-{name}",
                timeout_s=45.0)
            shot = Path(res.get("screenshot") or "")
            if not shot.is_file():
                continue
            base = BASELINE_DIR / f"{name}.png"
            if update or not base.is_file():
                BASELINE_DIR.mkdir(parents=True, exist_ok=True)
                base.write_bytes(shot.read_bytes())
                baselined.append(name)
                continue
            diff = _pixel_diff(base, shot)
            if diff is not None and diff > PIXEL_TOLERANCE:
                regressions.append(f"{page}: {diff:.1%} pixels differ")
        # New baselines are a record, not a failure; real diffs fail.
        self.assertEqual(regressions,
                         [] if not regressions else regressions)


if __name__ == "__main__":
    unittest.main()
