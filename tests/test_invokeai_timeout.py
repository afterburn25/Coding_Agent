import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from localcodeagent.image.invokeai import InvokeAIBackend

class TestInvokeAITimeout(unittest.TestCase):
    def test_invokeai_backend_timeout_handling(self):
        """Test that InvokeAI backend properly handles timeout scenarios."""
        backend = InvokeAIBackend(endpoint="http://127.0.0.1:9090", timeout=2.0)

        # Simulate a transport-level timeout at the urlopen layer — _json
        # must wrap it as BackendConnectionError, not leak raw URLError.
        with patch("localcodeagent.image.invokeai.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("timed out")

            with self.assertRaises(Exception) as context:
                backend.submit({"prompt": "test prompt", "count": 1})

            # The error should be a BackendConnectionError, not a raw timeout
            self.assertIn("BackendConnectionError", str(type(context.exception)))

if __name__ == "__main__":
    unittest.main()