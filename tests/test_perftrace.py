"""§20 — unified performance timeline tests."""
from __future__ import annotations

import time
import unittest

from localcodeagent.events import EventBus
from localcodeagent.perftrace import PerfTrace


class PerfTraceTests(unittest.TestCase):

    def test_marks_keep_order_and_deltas(self):
        p = PerfTrace()
        p.mark("chat", "send")
        time.sleep(0.01)
        p.mark("chat", "first_token")
        rows = p.timeline(lane="chat")
        self.assertEqual([r["stage"] for r in rows], ["send", "first_token"])
        self.assertGreaterEqual(rows[1]["delta_s"], 0.005)

    def test_unknown_lane_rejected(self):
        p = PerfTrace()
        p.mark("bogus", "x")
        self.assertEqual(p.timeline(), [])

    def test_route_event_maps_lanes(self):
        p = PerfTrace()
        p.route_event("task", {"state": "running"})
        p.route_event("voice", {"event": "segment"})
        p.route_event("model", {"event": "evict"})
        p.route_event("unrelated", {"x": 1})
        lanes = {r["lane"] for r in p.timeline()}
        self.assertEqual(lanes, {"chat", "voice", "gpu"})

    def test_ring_buffer_bounded(self):
        p = PerfTrace(max_events=50)
        for i in range(120):
            p.mark("chat", f"s{i}")
        self.assertEqual(len(p.timeline(limit=500)), 50)

    def test_summary_groups_by_lane(self):
        p = PerfTrace()
        p.mark("gpu", "evict")
        p.mark("gpu", "release")
        s = p.summary()
        self.assertEqual(s["events"], 2)
        self.assertIn("evict", s["lanes"]["gpu"])
        self.assertEqual(s["lanes"]["gpu"]["evict"]["count"], 1)

    def test_boot_mark_is_startup_lane(self):
        p = PerfTrace()
        p.boot_mark("state-init")
        rows = p.timeline(lane="startup")
        self.assertEqual(len(rows), 1)
        self.assertIn("t+", rows[0]["detail"])


class EventBusObserverTests(unittest.TestCase):

    def test_observer_sees_publishes(self):
        bus = EventBus()
        seen = []
        bus.observe(lambda etype, event: seen.append(etype))
        bus.publish("task", {"state": "running"})
        bus.publish("voice", {"event": "play"})
        self.assertEqual(seen, ["task", "voice"])

    def test_observer_error_never_breaks_publish(self):
        bus = EventBus()
        bus.observe(lambda *_: (_ for _ in ()).throw(RuntimeError()))
        event = bus.publish("task", {"state": "done"})
        self.assertEqual(event["state"], "done")

    def test_perf_trace_rides_the_bus(self):
        bus = EventBus()
        p = PerfTrace()
        bus.observe(p.route_event)
        bus.publish("task", {"state": "queued"})
        bus.publish("task", {"state": "completed"})
        stages = [r["stage"] for r in p.timeline(lane="chat")]
        self.assertEqual(stages, ["send", "done"])


if __name__ == "__main__":
    unittest.main()
