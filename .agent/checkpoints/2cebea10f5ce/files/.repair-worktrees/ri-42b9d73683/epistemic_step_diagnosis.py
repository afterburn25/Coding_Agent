import os
import sys
import traceback
import logging
from typing import List, Dict, Any

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def diagnose_test_error(error_trace: List[str], repo_state: Dict[str, Any]) -> Dict[str, Any]:
    """Diagnose a test error based on the error trace and repository state."""
    
    # Analyze the error trace
    error_analysis = analyze_error_trace(error_trace)
    
    # Analyze the repository state
    state_analysis = analyze_repo_state(repo_state)
    
    # Combine the analyses to form a diagnosis
    diagnosis = combine_analyses(error_analysis, state_analysis)
    
    return diagnosis


def analyze_error_trace(error_trace: List[str]) -> Dict[str, Any]:
    """Analyze the error trace to identify the root cause of the error."""
    
    # Extract the error message
    error_message = error_trace[0] if error_trace else "No error message found"
    
    # Extract the stack trace
    stack_trace = error_trace[1:] if len(error_trace) > 1 else []
    
    # Analyze the error message
    error_type, error_details = analyze_error_message(error_message)
    
    # Analyze the stack trace
    stack_analysis = analyze_stack_trace(stack_trace)
    
    return {
        "error_type": error_type,
        "error_details": error_details,
        "stack_analysis": stack_analysis
    }


def analyze_error_message(error_message: str) -> Tuple[str, str]:
    """Analyze the error message to identify the type and details of the error."""
    
    # Check for common error types
    if "AssertionError" in error_message:
        return "AssertionError", "An assertion failed"
    elif "ValueError" in error_message:
        return "ValueError", "An invalid value was provided"
    elif "TypeError" in error_message:
        return "TypeError", "An invalid type was provided"
    elif "KeyError" in error_message:
        return "KeyError", "A key was not found in a dictionary"
    elif "IndexError" in error_message:
        return "IndexError", "An index was out of range"
    elif "NameError" in error_message:
        return "NameError", "A variable was not found"
    elif "SyntaxError" in error_message:
        return "SyntaxError", "A syntax error occurred"
    elif "IndentationError" in error_message:
        return "IndentationError", "An indentation error occurred"
    elif "TabError" in error_message:
        return "TabError", "A tab error occurred"
    elif "OverflowError" in error_message:
        return "OverflowError", "An overflow error occurred"
    elif "ZeroDivisionError" in error_message:
        return "ZeroDivisionError", "A division by zero occurred"
    elif "IOError" in error_message:
        return "IOError", "An I/O error occurred"
    elif "EOFError" in error_message:
        return "EOFError", "An end-of-file error occurred"
    elif "KeyboardInterrupt" in error_message:
        return "KeyboardInterrupt", "A keyboard interrupt occurred"
    elif "SystemExit" in error_message:
        return "SystemExit", "A system exit occurred"
    else:
        return "UnknownError", error_message


def analyze_stack_trace(stack_trace: List[str]) -> Dict[str, Any]:
    """Analyze the stack trace to identify the location and context of the error."""
    
    # Extract the function names and line numbers from the stack trace
    function_names = [line.split("(")[0] for line in stack_trace]
    line_numbers = [line.split("(")[1].split(")")[0] for line in stack_trace]
    
    # Analyze the function names and line numbers
    function_analysis = analyze_function_names(function_names)
    line_analysis = analyze_line_numbers(line_numbers)
    
    return {
        "function_analysis": function_analysis,
        "line_analysis": line_analysis
    }


def analyze_function_names(function_names: List[str]) -> Dict[str, Any]:
    """Analyze the function names to identify the location of the error."""
    
    # Count the frequency of each function name
    function_counts = {}
    for function_name in function_names:
        if function_name in function_counts:
            function_counts[function_name] += 1
        else:
            function_counts[function_name] = 1
    
    # Find the most common function name
    most_common_function = max(function_counts, key=function_counts.get)
    
    return {
        "most_common_function": most_common_function,
        "function_counts": function_counts
    }


def analyze_line_numbers(line_numbers: List[str]) -> Dict[str, Any]:
    """Analyze the line numbers to identify the context of the error."""
    
    # Convert the line numbers to integers
    line_numbers = [int(line_number) for line_number in line_numbers]
    
    # Find the minimum and maximum line numbers
    min_line = min(line_numbers)
    max_line = max(line_numbers)
    
    return {
        "min_line": min_line,
        "max_line": max_line
    }


def analyze_repo_state(repo_state: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze the repository state to identify potential issues."""
    
    # Check for uncommitted changes
    uncommitted_changes = repo_state.get("uncommitted_changes", [])
    
    # Check for unpushed changes
    unpushed_changes = repo_state.get("unpushed_changes", [])
    
    # Check for conflicts
    conflicts = repo_state.get("conflicts", [])
    
    return {
        "uncommitted_changes": uncommitted_changes,
        "unpushed_changes": unpushed_changes,
        "conflicts": conflicts
    }


def combine_analyses(error_analysis: Dict[str, Any], state_analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Combine the error analysis and state analysis to form a diagnosis."""
    
    # Combine the error type and details
    error_type = error_analysis["error_type"]
    error_details = error_analysis["error_details"]
    
    # Combine the stack analysis
    stack_analysis = error_analysis["stack_analysis"]
    
    # Combine the state analysis
    uncommitted_changes = state_analysis["uncommitted_changes"]
    unpushed_changes = state_analysis["unpushed_changes"]
    conflicts = state_analysis["conflicts"]
    
    return {
        "error_type": error_type,
        "error_details": error_details,
        "stack_analysis": stack_analysis,
        "uncommitted_changes": uncommitted_changes,
        "unpushed_changes": unpushed_changes,
        "conflicts": conflicts
    }


def main():
    """Main function to run the diagnosis."""
    
    # Read the error trace from the input
    error_trace = sys.stdin.read().splitlines()
    
    # Read the repository state from the input
    repo_state = eval(sys.stdin.read())
    
    # Run the diagnosis
    diagnosis = diagnose_test_error(error_trace, repo_state)
    
    # Print the diagnosis
    print(diagnosis)

if __name__ == "__main__":
    main()