import traceback
import sys

def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    # Parse the error trace
    error_lines = error_trace.split('\n')
    
    # Identify the root cause
    root_cause = None
    for line in error_lines:
        if 'Traceback' in line:
            root_cause = line
            break
    
    # Extract the error message
    error_message = None
    for line in error_lines:
        if 'Error:' in line:
            error_message = line
            break
    
    # Extract the stack trace
    stack_trace = [line for line in error_lines if 'File' in line]
    
    # Return the diagnosis
    return {
        'root_cause': root_cause,
        'error_message': error_message,
        'stack_trace': stack_trace
    }