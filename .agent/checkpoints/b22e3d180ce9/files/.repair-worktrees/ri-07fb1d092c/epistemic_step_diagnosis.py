import traceback
import sys

def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    # Parse the error trace
    error_lines = error_trace.split('\n')
    
    # Identify the root cause
    root_cause = None
    
    # Analyze the error lines for the root cause
    for li in error_lines:
        if "capacity restoration" in li:
            root_cause = li
            break
    
    # Return the diagnosis
    return {
        "root_cause": root_cause,
        "suggestion": "Fix the capacity restoration logic in the code to ensure it properly handles capacity changes and allows the system to speak when needed."}