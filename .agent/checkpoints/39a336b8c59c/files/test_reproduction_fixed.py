#!/usr/bin/env python3

# Reproduction script for capacity restoration issue - FIXED VERSION

class _StubVoice:
    def __init__(self):
        self.enqueued = []
    
    def enqueue(self, id, line, vocalize=False, priority=False):
        self.enqueued.append((id, line))

class _StubState:
    def __init__(self):
        self.voice = _StubVoice()
        self._queue_announced = set()
        self._notice_last = {}
        self._notice_cursor = {"status": 0}
        
    def _on_worker_queue_event(self, event_type, payload):
        """Fixed logic for capacity restoration"""
        if event_type == "worker_capacity_reduced":
            ceiling = payload.get("ceiling")
            dedup_id = f"cap-{ceiling}"
            if dedup_id in self._queue_announced:
                return
            self._queue_announced.add(dedup_id)
            # Simulate speaking
            self.voice.enqueue(dedup_id, f"Capacity reduced to {ceiling}")
            return
            
        if event_type == "worker_capacity_restored":
            ceiling = payload.get("ceiling")
            # This is the FIXED logic - it discards the correct key
            self._queue_announced.discard(f"cap-{ceiling}")
            dedup_id = f"cap-up-{ceiling}"
            if dedup_id in self._queue_announced:
                return
            self._queue_announced.add(dedup_id)
            # Simulate speaking
            self.voice.enqueue(dedup_id, f"Capacity restored to {ceiling}")
            return

# Test the fix
print("Testing FIXED capacity restoration logic...")

st = _StubState()

# First capacity reduction - should speak
st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
print(f"After first reduction: {len(st.voice.enqueued)} enqueued")

# Second capacity reduction - should NOT speak (deduplication)
st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
print(f"After second reduction: {len(st.voice.enqueued)} enqueued")

# Capacity restoration - should speak
st._on_worker_queue_event("worker_capacity_restored", {"ceiling": 3})
print(f"After restoration: {len(st.voice.enqueued)} enqueued")

# Capacity reduction again - should speak (because restoration should have allowed it)
st._on_worker_queue_event("worker_capacity_reduced", {"ceiling": 3})
print(f"After third reduction: {len(st.voice.enqueued)} enqueued")

print("Expected: 3 enqueued (one for each unique event)")
print(f"Actual: {len(st.voice.enqueued)} enqueued")