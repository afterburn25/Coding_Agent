"""NexusBrain — the cognitive architecture coordinator.

Owns the Corpus Callosum and the seven core regions, wires existing Nexus
services into them, and provides the canonical input path:

    input → Thalamus (classify/fast-path/route)
          → Hippocampus (memory)
          → PFC (plan)
          → BasalGanglia (select action)
          → MotorCortex (execute)
          → Cerebellum (record/optimize)
          → BrainStem (health throughout)

Existing subsystems keep doing the work — this layer makes them
addressable, traceable, and resilient as one architecture.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from .bus import CorpusCallosum
from .events import CognitiveEvent, EventType, Priority
from .regions import BrainRegion
from . import events as ev
from .brainstem import BrainStem
from .hippocampus import Hippocampus
from .thalamus import Thalamus
from .pfc import PrefrontalCortex
from .basal_ganglia import BasalGanglia, ActionCandidate
from .motor import MotorCortex
from .cerebellum import Cerebellum
from .specialists import SPECIALISTS, SpecialistBrain


class NexusBrain:
    """The persistent cognitive architecture — survives model swaps,
    crashes, and restarts because cognition lives here, not in any model."""

    def __init__(self, *, state_dir: Path | None = None,
                 health=None, hardware_probe=None, crash_history=None,
                 answer_memory=None, knowledge_graph=None,
                 knowledge_memory=None, conversation_memory=None,
                 locked_vault=None, activity_source=None,
                 mission_planner=None, mission_store=None,
                 evaluator=None, tool_router=None, sandbox=None,
                 model_catalog=None, model_router=None,
                 classify_intent=None, research_class=None,
                 version_lookup=None, health_lookup=None, config_lookup=None,
                 twin=None, model_telemetry=None, tool_stats=None) -> None:
        self.bus = CorpusCallosum()
        state_dir = Path(state_dir) if state_dir else None
        brain_dir = state_dir / "brain" if state_dir else None

        self.brainstem = BrainStem(
            self.bus, health, hardware_probe=hardware_probe,
            crash_history=crash_history)
        self.hippocampus = Hippocampus(
            self.bus,
            (brain_dir / "memory.db") if brain_dir else None,
            answer_memory=answer_memory, knowledge_graph=knowledge_graph,
            knowledge_memory=knowledge_memory,
            conversation_memory=conversation_memory,
            locked_vault=locked_vault, activity_source=activity_source)
        self.thalamus = Thalamus(
            self.bus, hippocampus=self.hippocampus, brainstem=self.brainstem,
            classify_intent=classify_intent, research_class=research_class,
            model_catalog=model_catalog, model_router=model_router,
            version_lookup=version_lookup, health_lookup=health_lookup,
            config_lookup=config_lookup)
        self.pfc = PrefrontalCortex(
            self.bus, mission_planner=mission_planner,
            mission_store=mission_store, hippocampus=self.hippocampus,
            evaluator=evaluator)
        self.basal_ganglia = BasalGanglia(
            self.bus, hippocampus=self.hippocampus,
            tool_stats=tool_stats,
            resource_probe=self.brainstem.resources)
        self.motor = MotorCortex(
            self.bus, tool_router=tool_router, sandbox=sandbox)
        self.cerebellum = Cerebellum(
            self.bus, twin=twin, tool_stats=tool_stats,
            model_telemetry=model_telemetry,
            opt_path=(brain_dir / "optimizations.jsonl") if brain_dir else None)

        self.regions: dict[str, BrainRegion] = {
            r.name: r for r in (self.brainstem, self.hippocampus, self.thalamus,
                                self.pfc, self.basal_ganglia, self.motor,
                                self.cerebellum)}
        self.specialists = {s.name: SpecialistBrain(
            s, self.bus, hippocampus=self.hippocampus, motor=self.motor)
            for s in SPECIALISTS}

        # The Cerebellum consumes execution/model telemetry bus-wide.
        self.bus.subscribe_type(EventType.EXECUTION_RESULT,
                              self.cerebellum._guarded_handle)
        self.bus.subscribe_type(EventType.MODEL_RESULT,
                              self.cerebellum._guarded_handle)
        # Measured model latency feeds back into routing decisions —
        # the learning loop that makes selection improve over time.
        self.thalamus._benchmark = lambda mid: self.cerebellum.trend(
            "inference", mid)
        # Health events reach the Thalamus so routing reacts to dead models,
        # and the diagnostics specialist so repair incidents become
        # episodic memory it can recall when the same signature recurs.
        self.bus.subscribe_type(EventType.HEALTH_EVENT,
                              self.thalamus._guarded_handle)
        if "diagnostics_brain" in self.specialists:
            self.bus.subscribe_type(
                EventType.HEALTH_EVENT,
                self.specialists["diagnostics_brain"]._guarded_handle)

    # -- UI bridge ---------------------------------------------------------------
    _UI_FORWARD_TYPES = {
        EventType.ATTENTION, EventType.PLAN, EventType.ACTION_SELECTION,
        EventType.EXECUTION_RESULT, EventType.HEALTH_EVENT,
        EventType.CONFLICT_DETECTED, EventType.MISSION_EVENT,
    }

    def attach_ui_bus(self, ui_bus) -> None:
        """Mirror structural cognitive events onto the existing UI event bus
        so the activity stream shows region hops without a second consumer
        channel. Forwarded payloads stay structural — never raw reasoning."""
        def forward(event: CognitiveEvent) -> None:
            try:
                ui_bus.publish({
                    "type": "cognitive",
                    "region": event.source,
                    "event_type": event.type,
                    "correlation_id": event.correlation_id,
                    "mission_id": event.mission_id,
                    "confidence": event.confidence,
                    "detail": event.content,
                })
            except Exception:
                pass
        for t in self._UI_FORWARD_TYPES:
            self.bus.subscribe_type(t, forward)

    # -- canonical input flow -------------------------------------------------------
    def process_input(self, text: str, *, project_id: str = "",
                      conversation_id: str = "", mission_id: str = "",
                      correlation_id: str = "") -> dict[str, Any]:
        """Structural input handling. Returns a routing/result envelope —
        the caller (orchestrator) executes the actual work."""
        corr = correlation_id or uuid.uuid4().hex[:16]
        started = time.monotonic()
        self.bus.publish(CognitiveEvent(
            type=EventType.OBSERVATION, source="input", destination="",
            correlation_id=corr, mission_id=mission_id,
            conversation_id=conversation_id,
            content={"event": "input_received", "preview": text[:160]}))

        decision = self.thalamus.route(
            text, project_id=project_id, correlation_id=corr,
            mission_id=mission_id)
        envelope: dict[str, Any] = {
            "correlation_id": corr,
            "route": decision.region, "kind": decision.kind,
            "needs_model": decision.needs_model,
            "fast_path": decision.fast_path,
            "fast_arg": decision.fast_arg,
            "model_id": decision.model_id, "model_role": decision.model_role,
            "memory_entries": decision.memory_entries,
            "reasons": decision.reasons,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
        if decision.fast_path and decision.fast_path != "none":
            answer = (decision.trusted_answer
                      or self.thalamus.answer_fast_path(
                          decision.fast_path, decision.fast_arg))
            if answer:
                envelope["answer"] = answer
                self.bus.publish(CognitiveEvent(
                    type=EventType.MEMORY_RESULT if decision.fast_path == "memory"
                    else EventType.MODEL_RESULT,
                    source=ev.REGION_THALAMUS, correlation_id=corr,
                    mission_id=mission_id,
                    content={"fast_path": decision.fast_path,
                             "latency_ms": envelope["latency_ms"],
                             "model_id": ""}))
        return envelope

    def answers_without_model(self, text: str, *, project_id: str = "") -> bool:
        """Cheap gate predicate: can this input be fully answered without
        invoking a model? Used by the chat readiness gate so deterministic
        and trusted-memory fast paths work on a machine with no model
        installed."""
        try:
            decision = self.thalamus.route(text, project_id=project_id)
        except Exception:
            return False
        if decision.fast_path and decision.fast_path != "none":
            if decision.trusted_answer:
                return True
            # Only bypass the readiness gate when the fast path can actually
            # produce an answer — otherwise let the gate's 409 stand.
            try:
                return bool(self.thalamus.answer_fast_path(
                    decision.fast_path, decision.fast_arg))
            except Exception:
                return False
        return not decision.needs_model and bool(decision.trusted_answer)

    # -- observability -----------------------------------------------------------------
    def trace(self, correlation_id: str = "", limit: int = 100) -> list[dict]:
        return self.bus.trace(correlation_id=correlation_id, limit=limit)

    def trace_summary(self, correlation_id: str) -> dict[str, Any]:
        return self.bus.trace_summary(correlation_id)

    def status(self) -> dict[str, Any]:
        return {
            "regions": {name: r.status() for name, r in self.regions.items()},
            "specialists": {n: s.status() for n, s in self.specialists.items()},
            "overall": self.brainstem.health.overall(),
        }

    def close(self) -> None:
        try:
            self.brainstem.stop_watchdog()
        except Exception:
            pass
        try:
            self.hippocampus.close()
        except Exception:
            pass
