"""Tests for epistemic step diagnosis functionality."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import unittest
from epistemic_step_diagnosis import diagnose_test_error


class EpistemicDiagnosisTests(unittest.TestCase):
    def test_diagnose_test_error_with_complex_traceback(self):
        """Test that diagnose_test_error handles complex tracebacks correctly."""
        # This is a more complex traceback that should be handled properly
        error_trace = """Traceback (most recent call last):
  File "test_module.py", line 10, in <module>
    result = some_function()
  File "test_module.py", line 5, in some_function
    return divide(10, 0)
  File "test_module.py", line 2, in divide
    return a / b
ZeroDivisionError: division by zero"""
        
        result = diagnose_test_error(error_trace)
        
        # The diagnosis should identify the root cause properly
        self.assertIsNotNone(result['root_cause'])
        self.assertIn('ZeroDivisionError', result['root_cause'])
        self.assertIsNotNone(result['error_message'])
        self.assertIsNotNone(result['stack_trace'])
        self.assertGreater(len(result['stack_trace']), 0)

    def test_diagnose_test_error_with_no_error(self):
        """Test that diagnose_test_error handles tracebacks without clear errors."""
        error_trace = """Traceback (most recent call last):
  File "test_module.py", line 10, in <module>
    result = some_function()
  File "test_module.py", line 5, in some_function
    return divide(10, 0)"""
        
        result = diagnose_test_error(error_trace)
        
        # Should still return a diagnosis even if no clear error message
        self.assertIsNotNone(result['root_cause'])
        self.assertIsNotNone(result['stack_trace'])
        self.assertGreater(len(result['stack_trace']), 0)

    def test_diagnose_test_error_empty_traceback(self):
        """Test that diagnose_test_error handles empty tracebacks."""
        error_trace = ""
        
        result = diagnose_test_error(error_trace)
        
        # Should handle empty tracebacks gracefully
        self.assertIsNotNone(result['root_cause'])
        self.assertIsNotNone(result['stack_trace'])
        self.assertEqual(len(result['stack_trace']), 0)