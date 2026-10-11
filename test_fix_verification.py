import unittest
from epistemic_step_diagnosis import diagnose_test_error

class TestDiagnoseTestError(unittest.TestCase):
    def test_diagnose_test_error(self):
        # Test with a sample error trace
        error_trace = """
        Traceback (most recent call last):
          File "test_reproduction.py", line 10, in <module>
            main()
          File "test_reproduction.py", line 7, in main
            raise ValueError("Test error")
        ValueError: Test error
        """
        
        result = diagnose_test_error(error_trace)
        self.assertEqual(result['root_cause'], 'ValueError: Test error')

if __name__ == '__main__':
    unittest.main()