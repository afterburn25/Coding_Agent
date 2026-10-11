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
        
        # Check if the event is related to capacity restoration
        if event_type == "capacity_restoration":
            # Get the current capacity from the payload
            current_capacity = payload.get("capacity")
            
            # Check if the capacity has been restored from a previous ceiling
            if current_capacity is not None:
                # Remove the previous capacity level from _queue_announced
                prev_capacity = current_capacity - 1
                if prev_capacity in self._queue_announced:
                    self._queue_announced.remove(prev_capacity)
                
                # Add the current capacity level to _queue_announced
                self._queue_announced.add(current_capacity)
                
                # Speak if the capacity has been restored to a level that allows speaking
                if current_capacity >= 3:
                    self.voice.enqueue("cap-" + str(current_capacity), "Capacity restored to " + str(current_capacity))
        
        # Handle other event types if needed
        # ...