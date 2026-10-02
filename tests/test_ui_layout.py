"""Structural UI regression tests for the Nexus Core web shell.

These guard the app-wide layout fixes that keep every page on the shared
design system: one token palette, canonical nav, intact chat grid rows,
[hidden] semantics, and a collapsible utility rail. They run without a
browser so CI catches regressions cheaply.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

PAGES = [
    "index", "tools", "research", "image", "missions",
    "trainer", "system", "answers", "voice", "settings", "models",
]

# Canonical secondary-page destinations — every workspace page links all of them.
CANONICAL_LINKS = [
    "/", "/models.html", "/research.html", "/missions.html",
    "/image.html", "/tools.html", "/trainer.html", "/voice.html",
    "/answers.html", "/system.html", "/settings.html",
]


def read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


class DesignTokenTests(unittest.TestCase):
    def setUp(self):
        self.css = read("styles.css")

    def test_shared_tokens_defined(self):
        for token in (
            "--bg", "--surface", "--panel", "--panel2", "--card",
            "--line", "--line2", "--border", "--text", "--text-dim",
            "--muted", "--faint", "--cyan", "--blue", "--violet",
            "--accent", "--link", "--good", "--warn", "--danger", "--info",
            "--code-bg", "--focus", "--selection",
            "--good-tint", "--warn-tint", "--danger-tint", "--info-tint",
        ):
            self.assertIn(token + ":", self.css, f"missing design token {token}")

    def test_no_orphaned_purple_variable(self):
        # --purple was referenced but never defined; it must either exist or
        # no longer be referenced.
        defined = "--purple:" in self.css
        referenced = re.search(r"var\(--purple\b", self.css)
        self.assertTrue(defined or not referenced)

    def test_no_grid_template_colums_typo(self):
        for page_css in WEB.glob("*.css"):
            self.assertNotIn("grid-template-colums", page_css.read_text(encoding="utf-8"))

    def test_hidden_attribute_wins_over_display(self):
        # Bare [hidden] must beat class-level display rules (attach menu bug).
        self.assertRegex(self.css, r"\[hidden\]\s*{\s*display:\s*none")


class ChatLayoutTests(unittest.TestCase):
    """The chat grid rows must match the DOM child count of .main."""

    def setUp(self):
        self.css = read("styles.css")
        self.html = read("index.html")

    def test_main_grid_never_pins_composer_row(self):
        # The original bug: `62px minmax(0,1fr) auto 28px` placed the composer
        # on a fixed 28px track. Content-bearing rows after the flexible chat
        # row must be `auto` (or left implicit), never a fixed pixel height.
        m = re.search(r"\.main\s*{[^}]*grid-template-rows:\s*([^;]+);", self.css)
        self.assertIsNotNone(m, ".main has no grid-template-rows")
        rows = m.group(1).strip().split()
        self.assertIn("1fr", " ".join(rows), "chat message area must flex")
        for i, track in enumerate(rows):
            if i == 0:
                continue  # topbar may be fixed-height
            self.assertNotRegex(
                track, r"^\d+px$",
                f"row {i} is fixed-height {track} — composer/chips/footer "
                "would be crushed onto it",
            )

    def test_composer_row_is_auto_sized(self):
        # Composer must reach an auto track even when #attachChips is visible.
        m = re.search(r"\.main\s*{[^}]*grid-template-rows:\s*([^;]+);", self.css)
        rows = m.group(1).strip().split()
        self.assertEqual(rows[-1], "auto", "last .main track must be auto")

    def test_composer_is_not_fixed_height(self):
        m = re.search(r"\.composer\s*{([^}]*)}", self.css)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertNotRegex(block, r"\bheight\s*:\s*\d+px", "composer must size to content")

    def test_textarea_autoresizes(self):
        js = read("app.js")
        self.assertIn("scrollHeight", js, "composer textarea should auto-resize")

    def test_mobile_nav_exists(self):
        self.assertIn('class="mobile-nav"', self.html)
        self.assertIn(".mobile-nav-item", self.css)

    def test_rail_collapse_control(self):
        self.assertIn('id="railToggle"', self.html)
        self.assertIn("rail-collapsed", self.css)
        self.assertIn("rail-collapsed", read("app.js"))

    def test_attach_menu_hidden_by_default(self):
        m = re.search(r'<div[^>]*id="attachMenu"[^>]*>', self.html)
        self.assertIsNotNone(m)
        self.assertIn("hidden", m.group(0))


class NavigationTests(unittest.TestCase):
    def test_all_pages_load_shared_stylesheet(self):
        for page in PAGES:
            html = read(f"{page}.html")
            self.assertIn('href="/styles.css"', html, f"{page} missing styles.css")

    def _sidebar_block(self, html: str) -> str:
        m = re.search(r'<aside class="sidebar">.*?</aside>', html, re.S)
        self.assertIsNotNone(m, "page has no canonical .sidebar")
        return m.group(0)

    def test_workspace_pages_have_canonical_nav(self):
        for page in [p for p in PAGES if p != "index"]:
            html = read(f"{page}.html")
            for href in CANONICAL_LINKS:
                self.assertIn(f'href="{href}"', html, f"{page} missing nav link {href}")
            self.assertIn('class="primary-nav"', html,
                          f"{page} is not using the shared primary-nav")

    def test_sidebar_is_nav_only(self):
        # The left rail is navigation, not a junk drawer — page controls
        # (selects, forms, status blocks) belong in the page's own areas.
        for page in [p for p in PAGES if p != "index"]:
            sidebar = self._sidebar_block(read(f"{page}.html"))
            self.assertNotIn("<form", sidebar, f"{page} sidebar contains a form")
            self.assertNotIn("<select", sidebar, f"{page} sidebar contains a select")
            self.assertNotIn("<textarea", sidebar, f"{page} sidebar contains a textarea")
            self.assertNotIn('class="side-block', sidebar,
                             f"{page} sidebar still holds control blocks")
            self.assertNotIn('class="section-title"', sidebar,
                             f"{page} sidebar still holds sectioned extras")
            ids = re.findall(r'id="([^"]+)"', sidebar)
            self.assertEqual(ids, ["voiceToggle"],
                             f"{page} sidebar has non-nav widgets: {ids}")

    def test_displaced_controls_still_exist(self):
        # Controls removed from sidebars must land inside the page, not vanish.
        expectations = {
            "tools": ["categoryList", "mcpList"],
            "models": ["hwBox", "fileList"],
            "settings": ["settingsNav"],
            "voice": ["engineStatus", "presetList", "autoRead"],
            "research": ["researchMode", "researchStats", "recentResearch"],
            "trainer": ['class="pipeline"'],
            "system": ["overallCard", "refreshAll"],
            "missions": ["autonomyStatus", "createMission", "standingGoals",
                         "schedules", "triggers"],
            "image": ["imageBackend", "imageModels", "loraLibrary",
                      "imageInventory", "subjectProfiles"],
        }
        for page, ids in expectations.items():
            html = read(f"{page}.html")
            sidebar = self._sidebar_block(html)
            for marker in ids:
                needle = marker if marker.startswith(("id=", "class=")) else f'id="{marker}"'
                self.assertIn(needle, html, f"{page} lost {marker}")
                self.assertNotIn(
                    needle, sidebar,
                    f"{page} {marker} still inside the nav sidebar")

    def test_current_page_marked_active(self):
        for page in [p for p in PAGES if p != "index"]:
            html = read(f"{page}.html")
            self.assertRegex(
                html,
                r'class="[^"]*active[^"]*"\s+href="/' + page + r'\.html"|'
                r'href="/' + page + r'\.html"[^>]*class="[^"]*active',
                f"{page} does not mark itself active in nav",
            )


class SharedComponentTests(unittest.TestCase):
    def setUp(self):
        self.css = read("styles.css")

    def test_button_variants_exist(self):
        for cls in (".btn{", ".btn-primary", ".btn-danger"):
            self.assertIn(cls, self.css.replace(" ", "").replace("\n", ""))

    def test_page_head_pattern(self):
        self.assertIn(".page-head", self.css)

    def test_empty_state_pattern(self):
        self.assertIn(".empty-state", self.css)

    def test_form_control_defaults(self):
        # Element-level defaults so bare inputs/selects are themed.
        self.assertRegex(self.css, r"input:not\(\[type=checkbox\][\s\S]*?{[\s\S]*?background:\s*var\(--surface\)")
        self.assertRegex(self.css, r"input::placeholder")

    def test_workspace_link_component(self):
        for sel in (".nav-item", ".primary-nav", ".nav-icon", ".local-card"):
            self.assertIn(sel, self.css)


if __name__ == "__main__":
    unittest.main()
