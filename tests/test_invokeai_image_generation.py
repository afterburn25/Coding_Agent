from invokeai import InvokeAIBackend
from invokeai.backend import InvokeAIBackend

class TestInvokeAIImageGenerationTimeout(unittest.TestCase):
    def test_invokeai_image_generation_timeout(self):
        backend = InvokeAIBackend()
        try:
            healthy, _ = backend.health()
        except Exception:
            healthy = False
        if not healthy:
            self.skipTest("InvokeAI backend not running at 127.0.0.1:9090")
        with self.assertRaises(TimeoutError):
            backend.submit({"prompt": "test", "count": 1, "timeout": 10})

if __name__ == "__main__":
    unittest.main()