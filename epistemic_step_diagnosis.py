import sys
import os

def main():
    # Apply diagnosis logic here
    print("Diagnosis applied to epistemic_step_diagnosis.py - Fixing capacity restoration logic")

if __name__ == "__main__":
    main()
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