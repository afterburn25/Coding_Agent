import unittest
from localcodeagent.image.invokeai import InvokeAIBackend

class TestInvokeAIImageGeneration(unittest.TestCase):
    def test_invokeai_image_generation_timeout(self):
        backend = InvokeAIBackend()
        with self.assertRaises(TimeoutError):
            backend.submit({"prompt": "test", "count": 1, "timeout": 10})

if __name__ == "__main__":
    unittest.main()