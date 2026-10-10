import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from localcodeagent.image.invokeai import InvokeAIBackend

class TestInvokeAITimeout(unittest.TestCase):
    def test_invokeai_backend_timeout_handling(self):
        """Test that InvokeAI backend properly handles timeout scenarios."""
        backend = InvokeAIBackend(endpoint="http://127.0.0.1:9090", timeout=2.0)
        
        # Mock the _json method to simulate a timeout
        with patch.object(backend, '_json') as mock_json:
            # Simulate a timeout error
            mock_json.side_effect = urllib.error.URLError("timed out")
            
            # Test that the backend properly raises a BackendConnectionError
            with self.assertRaises(Exception) as context:
                backend.submit({"prompt": "test prompt", "count": 1})
            
            # The error should be a BackendConnectionError, not a raw timeout
            self.assertIn("BackendConnectionError", str(type(context.exception)))

if __name__ == "__main__":
    unittest.main()