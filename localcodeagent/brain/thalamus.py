"""Thalamus — input routing, attention, and model-resource selection.

Answers, deterministically first:
- What type of request is this?
- Can trusted memory or a deterministic fast path answer without a model?
- Which region or specialist brain should own it?
- If a model is needed, which capability profile — not which name?

Model selection requests *capabilities* (coding, context length, vision,
quality tier, latency budget) and resolves them against installed local
models plus configured cloud/remote resources. The Brain Stem's resource
snapshot and health states are consulted so a dead or starving model is
never selected.
"""
from __future__ import annotations

import ast
import math
import operator
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import CognitiveEvent, EventType, Priority
from .regions import BrainRegion
from . import events as ev


@dataclass(slots=True)
class ModelRequirement:
    """Capability request — regions ask for this, never a model name."""
    coding: bool = False
    vision: bool = False
    reasoning: bool = False
    min_context: int = 0
    quality: str = "auto"            # low|auto|high|frontier
    max_latency: str = "normal"      # realtime|normal|batch
    prefer_local: bool = True


@dataclass(slots=True)
class RouteDecision:
    kind: str                        # conversation|coding|research|image|status|mission|utility
    region: str                      # owning region/specialist
    needs_model: bool = True
    fast_path: str = ""              # version|status|config|math|memory|none
    fast_arg: str = ""               # payload: config key / math expression
    requirement: ModelRequirement = field(default_factory=ModelRequirement)
    model_id: str = ""               # resolved when a model is required
    model_role: str = ""
    reasons: list[str] = field(default_factory=list)
    memory_entries: int = 0
    trusted_answer: str = ""


class Thalamus(BrainRegion):
    name = ev.REGION_THALAMUS

    def __init__(self, bus, hippocampus=None, brainstem=None, *,
                 classify_intent: Callable[[str], str] | None = None,
                 research_class: Callable[[str], str] | None = None,
                 model_catalog: Callable[[], list[dict]] | None = None,
                 model_router=None,
                 version_lookup: Callable[[], str] | None = None,
                 health_lookup: Callable[[], dict] | None = None,
                 config_lookup: Callable[[str], str] | None = None) -> None:
        super().__init__(bus)
        self.hippocampus = hippocampus
        self.brainstem = brainstem
        self._classify_intent = classify_intent or (lambda t: "conversation")
        self._research_class = research_class or (lambda t: "stable")
        self._model_catalog = model_catalog or (lambda: [])
        self._model_router = model_router
        self._version_lookup = version_lookup or (lambda: "")
        self._health_lookup = health_lookup or (lambda: {})
        self._config_lookup = config_lookup or (lambda k: "")
        # Optional Cerebellum benchmark feed — set after construction.
        self._benchmark: Callable[[str], dict] | None = None

    # -- routing ------------------------------------------------------------------
    def route(self, text: str, *, project_id: str = "",
              correlation_id: str = "", mission_id: str = "") -> RouteDecision:
        """Classify input → owning region → optional fast path → model need."""
        t = text.strip()
        low = t.lower()

        # Deterministic fast paths — no LLM, no memory write.
        fp, fp_arg = self._fast_path(t, low)
        decision = RouteDecision(
            kind="status", region=ev.REGION_THALAMUS,
            needs_model=not fp, fast_path=fp or "none", fast_arg=fp_arg,
            reasons=["deterministic fast path"] if fp else [])
        if fp:
            self._attention(decision, text, correlation_id, mission_id)
            return decision

        intent = self._classify_intent(t)
        research = self._research_class(t)

        decision.kind = {
            "coding": "coding", "code": "coding", "research": "research",
            "image": "image", "conversation": "conversation",
            "mission": "mission", "utility": "utility",
            "diagnostics": "diagnostics", "repair": "diagnostics",
        }.get(intent, intent)
        decision.region = {
            "coding": "coding_brain", "code": "coding_brain",
            "research": "research_brain", "image": "vision_brain",
            "mission": ev.REGION_PFC, "utility": ev.REGION_THALAMUS,
            "conversation": "language_brain",
            "diagnostics": "diagnostics_brain", "repair": "diagnostics_brain",
        }.get(intent, "language_brain")

        # Memory first — trusted memory answers without a model.
        if self.hippocampus is not None:
            try:
                recall = self.hippocampus.recall(
                    t, kinds={"semantic", "procedural"}, project_id=project_id,
                    limit=4, min_confidence=0.55)
            except Exception:
                recall = None
            if recall is not None:
                decision.memory_entries = len(recall.entries)
                if recall.trusted_answer:
                    decision.needs_model = False
                    decision.fast_path = "memory"
                    decision.trusted_answer = recall.trusted_answer
                    decision.reasons.append("trusted Answer Memory hit")
                    self._attention(decision, text, correlation_id, mission_id)
                    return decision

        decision.reasons.extend(self._route_reasons(intent, research))
        decision.requirement = self._requirement(intent, research, t)
        if decision.region == ev.REGION_PFC or decision.requirement.reasoning:
            decision.region = ev.REGION_PFC if intent in {"mission"} else decision.region

        # Model resolution — capability against live availability.
        pick = self._select_model(decision.requirement)
        if pick:
            decision.model_id, decision.model_role = pick
        else:
            decision.reasons.append("no model currently satisfies requirement")
        self._attention(decision, text, correlation_id, mission_id)
        return decision

    def _attention(self, decision: RouteDecision, text: str,
                   correlation_id: str, mission_id: str) -> None:
        self.publish(EventType.ATTENTION, {
            "route": decision.region, "kind": decision.kind,
            "needs_model": decision.needs_model,
            "fast_path": decision.fast_path,
            "model_id": decision.model_id,
            "memory_entries": decision.memory_entries,
            "fast_arg": decision.fast_arg,
            "input_preview": text[:160],
        }, correlation_id=correlation_id, mission_id=mission_id)

    def _route_reasons(self, intent: str, research: str) -> list[str]:
        reasons = [f"intent={intent}"]
        if research and research != "stable":
            reasons.append(f"research={research}")
        return reasons

    # -- fast paths -----------------------------------------------------------------
    _VERSION_Q = re.compile(
        r"(?:what|which|what's|whats|tell me|give me|do you know|do you have|say)"
        r"(?:\s+(?:is|the|a|an|current|latest|installed|running|your|our|exact|exactly|new))*"
        r"\s+version"
        r"(?:\s+(?:of|is|for|are|this|it|that|nexus|core|nexus core|the app|app|"
        r"software|you|yourself|running|installed|do you have|are you|on|using|"
        r"you're))*"
        r"[?.!]*")
    _STATUS_Q = re.compile(
        r"(?:(?:what's|whats|what is|check|show|give me|tell me|run|how's|"
        r"how is|display|get)\s+)?"
        r"(?:the\s+|a\s+)?"
        r"(?:system|server|backend|runtime|nexus|nexus core|current)?\s*"
        r"(?:health|status|diagnostics|vitals)"
        r"(?:\s+(?:check|report|now|please|update))*[?.!]*")
    _CONFIG_Q = re.compile(
        r"^(?:what(?:'s| is| are)?|whats|where(?:'s| is| are)?|which|show me|"
        r"show|tell me|list|my|current|the current|do you know)\b")
    # After the key noun, only location/filler vocabulary is allowed — a
    # question about "workspace rules" or "model accuracy" isn't a config
    # lookup.
    _CONFIG_TAIL = re.compile(
        r"(?:\s+(?:path|dir|directory|folder|location|root|is|are|it|set|"
        r"configured|currently|right now|at|for|of|the|this|that|app|"
        r"project|here|to|in|please|now|you|use|using))*[?.!\s]*$")
    _CONFIG_KEYS = (
        (re.compile(r"(?:\b(?:the|my|your|our|this|current|nexus'?s?|app'?s?)"
                    r"\s+workspace\b|"
                    r"\bworkspace\s+(?:path|dir|directory|folder|location|root)\b|"
                    r"\bworking (?:dir|directory|folder)\b|"
                    r"\bproject (?:dir|directory|root|folder)\b|"
                    r"\brepo(?:sitory)? root\b)"),
         "workspace"),
        (re.compile(r"\bmodels?\s+(?:dir|directory|folder|path|location|"
                    r"storage)\b"), "models_dir"),
        (re.compile(r"(?:\b(?:active|current|selected|loaded|configured|"
                    r"primary|default)\s+(?:coding\s+)?models?\b|"
                    r"\bmodels?\s+(?:is|are)\s+(?:active|loaded|configured|"
                    r"in use|selected|running|on)\b|"
                    r"\bmodels?\s+are you (?:using|running|on)\b|"
                    r"\bwhat models? are you\b)"),
         "active_model"),
    )
    _MATH_LEAD = re.compile(
        r"(?:what(?:'s| is| are)?|whats|calculate|compute|evaluate|solve|"
        r"how much is|how many is|what does|math\s*:)\s+")
    _MATH_BINOPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.BitXor: operator.pow,  # "^" reads as exponentiation in chat
    }

    def _fast_path(self, text: str, low: str) -> tuple[str, str]:
        """Deterministic answers that never need a model. Returns
        (kind, arg) — '' kind when no fast path applies."""
        if self._VERSION_Q.fullmatch(low) or low in {"version", "version?"}:
            return "version", ""
        if self._STATUS_Q.fullmatch(low) \
                or low in {"are you up", "are you alive", "diagnostics"}:
            return "status", ""
        key = self._config_key(low)
        if key:
            return "config", key
        expr = self._math_expr(low)
        if expr:
            return "math", expr
        return "", ""

    def _config_key(self, low: str) -> str:
        """Map a bounded set of config questions to a lookup key."""
        if not self._CONFIG_Q.match(low):
            return ""
        for pattern, key in self._CONFIG_KEYS:
            m = pattern.search(low)
            if m and self._CONFIG_TAIL.match(low, m.end()):
                return key
        return ""

    def _math_expr(self, low: str) -> str:
        """Extract a pure arithmetic expression from the input, or ''."""
        m = self._MATH_LEAD.match(low)
        if m:
            cand = re.sub(r"[?=.!\s]+$", "", low[m.end():])
            cand = re.sub(r"\s*(equals?|equal to|is it)\s*$", "", cand).strip()
        else:
            cand = low.rstrip(" ?.!=")
            # Bare expressions need an unmistakable operator — an unspaced
            # hyphen alone would eat phone-number-shaped strings.
            if not re.search(r"[+*/%^]|\d\s*[x×]\s*\d|\s-\s", cand):
                return ""
        expr = re.sub(r"(\d)\s*[x×]\s*(\d)", r"\1*\2", cand)
        expr = expr.replace("−", "-").replace("×", "*").replace("÷", "/")
        if not re.fullmatch(r"[0-9\s()+\-*/%.^]+", expr):
            return ""
        stripped = expr.lstrip("(").lstrip()
        if stripped.startswith("-") or stripped.startswith("+"):
            stripped = stripped[1:]
        if len(re.findall(r"\d", expr)) < 2 or not re.search(r"[+\-*/%^]", stripped):
            return ""
        return expr

    def answer_fast_path(self, kind: str, arg: str = "") -> str:
        """Deterministic response bodies for fast-path routes."""
        if kind == "version":
            v = self._version_lookup()
            return f"Nexus Core {v}" if v else ""
        if kind == "status":
            health = self._health_lookup() or {}
            states = health.get("states") or {}
            if not states:
                return "All monitored components report healthy."
            bad = {k: v for k, v in states.items()
                   if v in {"crashed", "hung", "stopped", "degraded"}}
            if not bad:
                return (f"All {len(states)} monitored components are "
                        "operating within normal parameters.")
            lines = "; ".join(f"{k}: {v}" for k, v in bad.items())
            return f"Component status: {lines}."
        if kind == "config":
            if not arg:
                return ""
            try:
                return str(self._config_lookup(arg) or "")
            except Exception:
                return ""
        if kind == "math":
            return self._answer_math(arg)
        return ""

    def _answer_math(self, expr: str) -> str:
        """Evaluate a vetted arithmetic expression — never eval()s input."""
        if not expr:
            return ""
        try:
            value = self._math_node(ast.parse(expr, mode="eval"))
        except ZeroDivisionError:
            return f"{expr} — that's a division by zero."
        except Exception:
            return ""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return f"{expr} = {value}"

    def _math_node(self, node) -> float:
        if isinstance(node, ast.Expression):
            return self._math_node(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or \
                    not isinstance(node.value, (int, float)):
                raise ValueError("non-numeric")
            return node.value
        if isinstance(node, ast.UnaryOp) and \
                isinstance(node.op, (ast.UAdd, ast.USub)):
            v = self._math_node(node.operand)
            return v if isinstance(node.op, ast.UAdd) else -v
        if isinstance(node, ast.BinOp):
            op = self._MATH_BINOPS.get(type(node.op))
            if op is None:
                raise ValueError("unsupported operator")
            left = self._math_node(node.left)
            right = self._math_node(node.right)
            if op is operator.pow and abs(right) > 1000:
                raise ValueError("exponent too large")
            result = op(left, right)
            if isinstance(result, complex) or \
                    (isinstance(result, float) and not math.isfinite(result)):
                raise ValueError("out of range")
            return result
        raise ValueError("unsupported expression")

    # -- model resolution -----------------------------------------------------------
    def _requirement(self, intent: str, research: str, text: str) -> ModelRequirement:
        req = ModelRequirement()
        if intent in {"coding", "code"}:
            req.coding = True
            req.quality = "auto"
        if intent == "research" or research in {"explicit", "volatile"}:
            req.reasoning = research == "explicit" or intent == "research"
        if intent == "image":
            req.vision = True
            req.quality = "high"
        if len(text) > 12000:
            req.min_context = 32768
        if intent == "conversation" and len(text) < 400:
            req.quality = "low"
            req.max_latency = "realtime"
        return req

    def _select_model(self, req: ModelRequirement) -> tuple[str, str] | None:
        """Resolve a capability requirement to a live model. Consults the
        Brain Stem's health/resources so dead/starving models are skipped."""
        unhealthy = set()
        if self.brainstem is not None:
            try:
                unhealthy = {n for n, s in self.brainstem.health.states().items()
                             if s in {"crashed", "hung", "stopped"}}
            except Exception:
                pass
        try:
            catalog = self._model_catalog() or []
        except Exception:
            catalog = []
        coding_roles = {"coding", "primary_coder", "fast_coder",
                        "deep_reasoner", "general"}
        # First pass — collect eligible candidates and their measured
        # recent latency so benchmark history can adjust scoring.
        candidates: list[tuple[dict, float]] = []
        for m in catalog:
            if not m.get("enabled", True) or not m.get("runnable", m.get("healthy", True)):
                continue
            mid = str(m.get("id") or "")
            if mid in unhealthy or f"model:{mid}" in unhealthy:
                continue
            bench_latency = 0.0
            if self._benchmark is not None:
                try:
                    b = self._benchmark(mid) or {}
                    if b.get("samples") and b.get("last"):
                        bench_latency = float(b["last"])
                except Exception:
                    bench_latency = 0.0
            candidates.append((m, bench_latency))
        # Median split: measured-faster-than-median models get +2, slower
        # get -2. Bounded so history informs but never overrides capability.
        known = sorted(l for _, l in candidates if l > 0)
        median = known[len(known) // 2] if known else 0.0
        best: tuple[int, dict] | None = None
        best_role = ""
        for m, bench_latency in candidates:
            mid = str(m.get("id") or "")
            roles = set(m.get("roles") or ([m.get("role")] if m.get("role") else []))
            if req.coding and not roles & coding_roles:
                continue
            if req.vision and not (m.get("vision") or roles & {"image", "vision"}):
                continue
            ctx = int(m.get("context") or m.get("context_window") or 0)
            if req.min_context and ctx and ctx < req.min_context:
                continue
            score = 0
            if m.get("healthy"):
                score += 4
            if req.quality == "low" and "utility" in roles:
                score += 6
            if req.coding:
                score += {"primary_coder": 6, "coding": 6, "fast_coder": 4,
                          "deep_reasoner": 3}.get(
                    next(iter(roles & coding_roles), ""), 2)
            if req.reasoning and roles & {"reasoning", "deep_reasoner", "coding"}:
                score += 4
            if m.get("resident") or m.get("loaded"):
                score += 3
            if req.prefer_local and not m.get("remote", False):
                score += 2
            if bench_latency > 0 and median > 0:
                if bench_latency < median:
                    score += 2
                elif bench_latency > median:
                    score -= 2
            score -= int(m.get("load_rank") or 0)
            if best is None or score > best[0]:
                best = (score, m)
                if req.coding and "primary_coder" in roles:
                    best_role = "primary_coder"
                elif req.coding and roles & coding_roles:
                    best_role = sorted(roles & coding_roles)[0]
                else:
                    best_role = sorted(roles)[0] if roles else "general"
        if best is None:
            return None
        return str(best[1].get("id")), best_role or "general"

    # -- events ----------------------------------------------------------------------
    def handle(self, event: CognitiveEvent) -> None:
        if event.type == EventType.MODEL_REQUEST:
            # Regions/specialists request capabilities, never model names.
            c = event.content
            req = ModelRequirement(
                coding=bool(c.get("coding")),
                vision=bool(c.get("vision")),
                reasoning=bool(c.get("reasoning")),
                min_context=int(c.get("min_context") or 0),
                quality=str(c.get("quality") or "auto"),
                max_latency=str(c.get("max_latency") or "normal"))
            pick = self._select_model(req)
            self.respond(event, EventType.MODEL_RESULT, {
                "model_id": pick[0] if pick else "",
                "model_role": pick[1] if pick else "",
                "satisfied": bool(pick)})
            return
        if event.type == EventType.HEALTH_EVENT:
            # A model-facing component dying may invalidate routing — trace it.
            comp = str(event.content.get("component") or "")
            if comp.startswith(("model:", "llm", "llama")):
                self.publish(EventType.ATTENTION, {
                    "route": "reroute_check", "component": comp,
                    "state": event.content.get("state", ""),
                }, correlation_id=event.correlation_id,
                    mission_id=event.mission_id)

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["catalog_models"] = len(self._model_catalog() or [])
        return base
