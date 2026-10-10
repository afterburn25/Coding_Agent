import unittest
from localcodeagent.image.invokeai import InvokeAIBackend

class TestInvokeAITimeoutFix(unittest.TestCase):
    def test_invokeai_backend_default_timeout_is_sufficient(self):
        """Test that InvokeAI backend has a sufficient default timeout."""
        # The backend should now have a 15.0 second timeout instead of 4.0 seconds
        backend = InvokeAIBackend(endpoint="http://127.0.0.1:9090")
        
        # Verify the timeout is set to 15.0 seconds (our fix)
        self.assertGreaterEqual(backend.timeout, 15.0)

    def test_invokeai_backend_custom_timeout_still_works(self):
        """Test that custom timeout values still work."""
        backend = InvokeAIBackend(endpoint="http://127.0.0.1:9090", timeout=10.0)
        
        # Verify custom timeout is preserved
        self.assertEqual(backend.timeout, 10.0)

if __name__ == "__main__":
    unittest.main()