import pytest
from epistemic_step_diagnosis import diagnose_test_error

def test_diagnose_cuda_out_of_memory_error():
    error_trace = """
    Traceback (most recent call last):
      File "soak_persona.py", line 62, in <module>
        test_case.run()
      File "soak_persona.py", line 45, in run
        result = self.invoke()
      File "invokeai.py", line 123, in invoke
        output = self.model.generate()
    RuntimeError: CUDA out of memory. Tried to allocate 24GB (GPU 0; 24GB total cap)
    """
    
    diagnosis = diagnose_test_error(error_trace)
    
    assert diagnosis['root_cause'] == 'RuntimeError: CUDA out of memory. Tried to allocate 24GB (GPU 0; 24GB total cap)'
    assert 'RuntimeError: CUDA out of memory. Tried to allocate 24GB (GPU 0; 24GB total cap)' in diagnosis['error_lines']
    
    print("Test passed: diagnosed CUDA out of memory error correctly")