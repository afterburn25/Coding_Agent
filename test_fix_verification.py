#!/usr/bin/env python3

class StubVoice:
    def __init__(self):
        self.enqueued = []
    
    def enqueue(self, task_id, text, **kw):
        self.enqueued.append((task_id, text))

class StubState:
    def __init__(self):
        self.voice = StubVoice()
        self._queue_announced = set()
        self._notice_last = {}
        self._notice_cursor = {'status': 0}
        
    def _on_worker_queue_event(self, event_type, payload):
        if event_type == "worker_capacity_reduced":
            # Resource pressure explains slowdowns — speak once per
            # distinct ceiling so repeated failures don't nag, AND
            # once per 10 min for the family so a flapping ceiling
            # doesn't narrate every oscillation.
            ceiling = payload.get("ceiling")
            # Next restore lands at ceiling+1 — let it speak again.
            self._queue_announced.discard(f"cap-up-{int(ceiling or 0) + 1}")
            self._speak_notice(
                f"cap-{ceiling}", "status",
                "Worker capacity reduced — heavy jobs may run slower.",
                family="capacity", cooldown_s=600.0)
            return
        if event_type == "worker_capacity_restored":
            ceiling = payload.get("ceiling")
            # Next reduction lands at ceiling-1 — let it speak again.
            self._queue_announced.discard(f"cap-{ceiling}")
            self._speak_notice(
                f"cap-up-{ceiling}", "status",
                "Workers are recovering — capacity is back up.",
                family="capacity", cooldown_s=600.0)
            return

    def _speak_notice(self, task_id, kind, text, family, cooldown_s):
        # Simple deduplication logic
        if task_id in self._queue_announced:
            return
        self._queue_announced.add(task_id)
        self.voice.enqueue(task_id, text)

def test_original_bug():
    """Test to demonstrate the original bug"""
    st = StubState()
    
    # First capacity reduction - should speak
    st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
    print(f"After first reduction: {len(st.voice.enqueued)} enqueued")
    
    # Second capacity reduction - should NOT speak (deduplication)
    st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
    print(f"After second reduction: {len(st.voice.enqueued)} enqueued")
    
    # Capacity restoration - should speak
    st._on_worker_queue_event("worker_capacity_restored", {"ceiling": 3})
    print(f"After restoration: {len(st.voice.enqueued)} enqueued")
    
    # Third capacity reduction - should speak again (because we discarded the right key)
    st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
    print(f"After third reduction: {len(st.voice.enqueued)} enqueued")
    
    print(f"Expected: 3 enqueued (one for each unique event)")
    print(f"Actual: {len(st.voice.enqueued)} enqueued")
    
    return len(st.voice.enqueued) == 3

if __name__ == "__main__":
    success = test_original_bug()
    print(f"Test {'PASSED' if success else 'FAILED'}")