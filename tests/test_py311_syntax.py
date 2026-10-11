"""Package sources must parse under the CI interpreter's grammar.

The dev box runs Python 3.14, whose PEP-701 f-strings accept
multi-line expressions that are SyntaxError on the 3.11 CI runner —
a whole collection run fails there while local tests stay green.
`ast.parse(feature_version)` applies the older grammar locally.
"""

import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "localcodeagent"


class Py311SyntaxTests(unittest.TestCase):
    def test_all_package_files_parse_as_311(self):
        bad = []
        for path in sorted(PKG.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                ast.parse(path.read_text(encoding="utf-8"),
                          filename=str(path),
                          feature_version=(3, 11))
            except SyntaxError as exc:
                bad.append(f"{path.relative_to(PKG)}: {exc}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
