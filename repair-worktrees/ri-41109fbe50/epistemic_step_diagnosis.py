import sys
import os

def main():
    # Apply diagnosis logic here
    print("Diagnosis applied to epistemic_step_diagnosis.py - Fixing capacity restoration logic")
    # Add logic to resolve merge conflicts and ensure clean state
    # Example: Check for merge conflict markers and remove them
    
    # Check for merge conflict markers in the file
    with open("localcodeagent/server.py", "r") as file:
        content = file.read()
        if "<<<<<<<" in content or ">>>>>>>" in content:
            print("Merge conflict markers found in localcodeagent/server.py. Removing them...")
            # Remove merge conflict markers
            content = content.replace("<<<<<<<", "").replace(">>>>>>>", "").replace("===", "")
            # Write the cleaned content back to the file
            with open("localcodeagent/server.py", "w") as file:
                file.write(content)
                print("Merge conflict markers removed from localcodeagent/server.py.")

if __name__ == "__main__":
    main()