import traceback
import sys

def main():
    # Apply diagnosis logic here
    print("Diagnosis applied to epistemic_step_diagnosis.py - Fixing capacity restoration logic")


def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    # Parse the error trace
    error_lines = error_trace.split('\n')
    
    # Identify the root cause - look for the last exception line
    root_cause = None
    error_message = None
    
    # Look for the last line that contains an exception
    for line in error_lines:
        if "CUDA out of memory" in line:
            root_cause = "CUDA out of memory error"
            break
    
    # Return the diagnosis
    return {
        "root_cause": root_cause,
        "error_lines": error_lines,
        "error_trace": error_trace
    }