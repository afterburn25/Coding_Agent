"""API error-mapping regressions — malformed client input maps to real
4xx codes; handler faults return a JSON 500 instead of dropping the
connection."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class TestApiErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake", endpoint="http://127.0.0.1:1/v1",
                                 model="m", roles=["utility"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False,
            autonomy_enabled=True)
        cls.server, cls.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(lambda: (cls.server.shutdown(),
                                     cls.server.server_close(),
                                     stop_state(cls.state)))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _raw(self, path, data, *, method="POST",
             ctype="application/json"):
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": ctype}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def _post(self, path, body):
        return self._raw(path, json.dumps(body).encode())

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    # -- the fixes -------------------------------------------------------

    def test_malformed_json_is_400_not_500(self):
        code, raw = self._raw("/api/hypotheses", b"{not json")
        self.assertEqual(code, 400, raw)
        self.assertIn("invalid JSON", raw.decode())

    def test_non_object_json_is_400_not_500(self):
        # A list body used to AttributeError through body.get → 500.
        for body in (b"[1,2,3]", b'"text"', b"42", b"null"):
            code, raw = self._raw("/api/hypotheses", body)
            self.assertEqual(code, 400, (body, raw))

    def test_get_handler_fault_returns_json_500(self):
        # Before the wrapper, an in-handler exception killed the
        # connection — the client saw an aborted reply, not JSON.
        orig = self.state.causal.list
        try:
            def boom(**_kw):
                raise RuntimeError("simulated causal store fault")
            self.state.causal.list = boom
            code, raw = self._get("/api/causal-memory")
            self.assertEqual(code, 500)
            payload = json.loads(raw)
            self.assertIn("error", payload)
            self.assertIn("simulated", payload["error"])
        finally:
            self.state.causal.list = orig

    def test_get_permission_error_is_403(self):
        orig = self.state.decisions.list
        try:
            def deny(**_kw):
                raise PermissionError("creator session required")
            self.state.decisions.list = deny
            code, raw = self._get("/api/decisions")
            self.assertEqual(code, 403)
            self.assertIn("creator", raw.decode())
        finally:
            self.state.decisions.list = orig

    def test_patch_outside_profiles_is_405(self):
        code, raw = self._raw("/api/decisions", b"{}", method="PATCH")
        self.assertEqual(code, 405)

    def test_patch_bad_json_is_400(self):
        code, _ = self._raw("/api/profiles/x", b"nope{", method="PATCH")
        self.assertEqual(code, 400)

    def test_missing_required_field_is_400(self):
        code, _ = self._post("/api/decisions", {"alternatives": []})
        self.assertEqual(code, 400)

    def test_unknown_api_route_is_404(self):
        code, _ = self._get("/api/definitely-not-a-route")
        self.assertEqual(code, 404)

    def test_garbage_numeric_fields_fall_back_not_500(self):
        # String where a number belongs used to ValueError → 500.
        code, raw = self._post("/api/assumptions", {
            "text": "numeric garbage tolerance", "confidence": "abc"})
        self.assertEqual(code, 200, raw)
        self.assertEqual(
            json.loads(raw)["assumption"]["confidence"], 0.5)
        code, raw = self._get("/api/github/repos?limit=notanumber")
        # github not connected in this env — must be a JSON response,
        # never a ValueError 500 from the query cast.
        self.assertIn(code, (200, 400, 403, 503), raw)
        payload = json.loads(raw)
        self.assertIsInstance(payload, dict)

    def test_backend_connection_error_is_503_not_500(self):
        # A dead model/backend endpoint is a dependency failure, not a
        # server fault — 503 with the transport's friendly+diagnostic.
        from localcodeagent.netdiag import BackendConnectionError
        orig = self.state.causal.list
        try:
            def down(**_kw):
                raise BackendConnectionError(
                    ConnectionRefusedError("refused"),
                    subsystem="llm", url="http://127.0.0.1:8391/v1/chat",
                    model_id="m1", phase="connect", elapsed_s=1.2)
            self.state.causal.list = down
            code, raw = self._get("/api/causal-memory")
            self.assertEqual(code, 503, raw)
            payload = json.loads(raw)
            self.assertIn("diagnostic", payload)
            self.assertIn("error", payload)
        finally:
            self.state.causal.list = orig

    def test_assumption_routes(self):
        code, raw = self._post("/api/assumptions", {
            "text": "VRAM fits the model", "scope_type": "mission",
            "scope_id": "m-err", "dependents": {"tasks": ["t-1"]}})
        self.assertEqual(code, 200, raw)
        aid = json.loads(raw)["assumption"]["id"]
        code, raw = self._post("/api/assumptions/state", {
            "id": aid, "action": "invalidate", "evidence": "OOM"})
        self.assertEqual(code, 200, raw)
        out = json.loads(raw)
        self.assertEqual(out["dependents"]["tasks"], ["t-1"])
        code, _ = self._post("/api/assumptions", {"text": ""})
        self.assertEqual(code, 400)
        code, _ = self._post("/api/assumptions/state",
                             {"id": "asm-nope", "state": "verified"})
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
