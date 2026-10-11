import unittest

class TestInvokeAIConfigUpdate(unittest.TestCase):
    def test_invokeai_config_update(self):
        # Test that the invokeai.yaml file has been updated correctly
        with open('invokeai.yaml', 'r') as file:
            config = file.read()

        self.assertIn('schema_version: 1', config)
        self.assertIn('cuda_pool_cap: 18GB', config)

        # Test that the test case in soak_persona.py is correctly referencing the updated config
        with open('tests/soak_persona.py', 'r') as file:
            soak_code = file.read()

        self.assertIn('facts=["The CUDA out-of-memory error came from the 24GB pool cap in invokeai.yaml."]', soak_code)
        self.assertIn('exact_spans=["invokeai.yaml", "24GB", "18GB"]', soak_code)

if __name__ == '__main__':
    unittest.main()