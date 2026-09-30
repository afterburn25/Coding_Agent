from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ImageWorkspaceUiTests(unittest.TestCase):
    def test_mask_editor_and_before_after_viewer_are_present(self):
        html = (ROOT / "web" / "image.html").read_text(encoding="utf-8")
        for element_id in ("beforeImage", "afterImage", "maskCanvas", "maskBrush", "saveMask"):
            self.assertIn(f'id="{element_id}"', html)

    def test_inpaint_submits_saved_local_mask_path(self):
        js = (ROOT / "web" / "image.js").read_text(encoding="utf-8")
        self.assertIn("operation==='inpaint'", js)
        self.assertIn("if(maskDirty)await saveMask()", js)
        self.assertIn("mask_path:maskRef?.path||''", js)
        self.assertIn("/api/image/upload", js)

    def test_generated_output_can_be_reused_or_compared(self):
        js = (ROOT / "web" / "image.js").read_text(encoding="utf-8")
        self.assertIn('class="mini-button use-source"', js)
        self.assertIn('class="mini-button compare-job"', js)
        self.assertIn("refs=[{path,preview", js)
        self.assertIn("updateComparison(data.jobs||[])", js)


if __name__ == "__main__":
    unittest.main()
