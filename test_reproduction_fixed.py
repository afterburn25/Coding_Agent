import sys
import os
import traceback
from epistemic_step_diagnosis import diagnose_test_error

def main():
    # Simulate a test error
    try:
        raise ValueError("Test error")
    except Exception as e:
        error_trace = traceback.format_exc()
        diagnosis = diagnose_test_error(error_trace)
        print(f"Diagnosis: {diagnosis['root_cause']}")
        
if __name__ == "__main__":
    main()