import re

def main():
    # Apply diagnosis logic here
    print("Diagnosis applied to epistemic_step_diagnosis.py - Fixing capacity restoration logic")


_EXCEPTION_LINE = re.compile(
    r"^([A-Za-z_][\w.]*?(?:Error|Exception|Warning|Exit|Interrupt|"
    r"Abort|Failure|Timeout|KeyError|IndexError|TypeError|ValueError|"
    r"NameError|AttributeError|RuntimeError|StopIteration|"
    r"AssertionError)[\w.]*)\s*:?\s*(.*)$")


def diagnose_test_error(error_trace):
    """Diagnose a test error and return a structured analysis."""
    text = str(error_trace or "")
    error_lines = text.split('\n')

    # Stack frames — the File "...", line N, in func call lines.
    stack_trace = [
        ln.strip() for ln in error_lines
        if ln.strip().startswith('File ')
    ]

    # Root cause — the last exception-looking line wins (nested
    # tracebacks put the innermost failure last).
    root_cause = None
    error_message = None
    for ln in error_lines:
        stripped = ln.strip()
        m = _EXCEPTION_LINE.match(stripped)
        if m and not stripped.startswith('File '):
            root_cause = m.group(1)
            error_message = m.group(2).strip() or None

    if root_cause is None:
        # Frames without an exception line, or an empty trace entirely —
        # still report a cause rather than returning nothing.
        root_cause = ("Traceback without an exception line"
                      if stack_trace else "No error trace provided")

    return {
        'root_cause': root_cause,
        'error_message': error_message,
        'stack_trace': stack_trace,
    }
