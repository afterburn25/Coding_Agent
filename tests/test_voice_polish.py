import unittest

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.agent.state import State
from localcodeagent.agent.voice import Voice


class TestVoicePolish(unittest.TestCase):
    def setUp(self):
        self.st = State()
        self.st.voice = Voice()
        self.st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
        self.st._on_worker_queue_event("worker_capacity_restored", {"ceiling": 4})
        self.st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})

    def test_capacity_restored_speaks(self):
        self.assertEqual(len(self.st.voice.enqueued), 0)
        self.assertIn("recovering", self.st.voice.enqueued[0][1])

if __name__ == "__main__":
    unittest.main()