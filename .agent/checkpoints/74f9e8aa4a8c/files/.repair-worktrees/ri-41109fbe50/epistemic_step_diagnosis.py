import traceback
import sys

def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    # Parse the error trace
    error_lines = error_trace.split('\n')
    
    # Identify the root cause
    root_cause = None
    for line in error_lines:
        if "CUDA out of memory" in line:
            root_cause = "CUDA out of memory error"
            break
    
    # Return analysis
    return {
        "root_cause": root_cause,
        "error_lines": error_lines
    }

if __name__ == "__main__":
    main()