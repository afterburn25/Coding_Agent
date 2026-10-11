import unittest
from localcodeagent.personality import _StubState

class TestVoicePolish(unittest.TestCase):
    def test_capacity_restored_speaks(self):
        st = _StubState()
        st._on_worker_queue_event("worker_capacity_restored", {"ceiling": 4})  # Event triggering
        
        # Ensure the voice system enqueues a message
        self.assertEqual(len(st.voice.enqueued), 1)  # Expected 1 message enqueued
        
        # Additional check for message content
        self.assertIn("Capacity restored", st.voice.enqueued[0])