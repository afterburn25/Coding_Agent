import traceback
import sys


def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    # Parse the error trace
    error_lines = error_trace.split('\n')
    
    # Identify the root cause - look for the last exception line
    root_cause = None
    error_message = None
    
    # Look for the last line that contains an exception
    for line in reversed(error_lines):
        if 'Traceback' in line:
            continue  # Skip traceback header
        elif 'Error:' in line or 'Exception:' in line or line.strip().endswith(':'):
            root_cause = line.strip()
            break
        elif 'raise' in line:
            root_cause = line.strip()
            break
    
    # If we didn't find a specific error, use the last line with content
    if not root_cause:
        for line in reversed(error_lines):
            if line.strip():
                root_cause = line.strip()
                break
    
    # Extract the error message - look for the actual error type
    for line in error_lines:
        if 'Error:' in line or 'Exception:' in line:
            error_message = line.strip()
            break
        elif line.strip() and (line.strip().endswith(':') or line.strip().count(':') > 1):
            # This might be an error line
            error_message = line.strip()
            break
    
    # Extract the stack trace - lines with 'File'
    stack_trace = [line for line in error_lines if 'File' in line and 'Traceback' not in line]
    
    # Return the diagnosis
    return {
        'root_cause': root_cause or 'No root cause identified',
        'error_message': error_message,
        'stack_trace': stack_trace
    }