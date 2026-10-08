"""Deterministic local-action lane — bounded computer tasks that must
EXECUTE, not narrate.

Natural-language requests like ``create a folder D:\\Nexus`` resolve to
a structured plan and run the full lifecycle:

    intent -> capability -> permission -> execute -> verify
    -> evidence -> truthful response

Anything outside the bounded grammar returns ``None`` and falls through
to the model/tools lane — this lane never guesses at a path or an
operation. Outside-workspace absolute paths are allowed only when the
user named them explicitly, and always require approval regardless of
the configured filesystem level (writing outside the declared boundary
is never a "routine" operation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class ActionPlan:
    kind: str                     # mkdir|write|delete|move|rename|copy
    tool: str                     # fs_mkdir|write_file|fs_delete|fs_move|fs_copy
    permission: str               # filesystem.write|filesystem.delete
    params: dict[str, str] = field(default_factory=dict)
    resolved: dict[str, str] = field(default_factory=dict)  # params, resolved
    outside_root: bool = False
    action_text: str = ""         # "create folder D:\Nexus"
    clarify: str = ""             # set when intent is clear but target missing
    display: str = ""             # display path for responses


# Polite prefixes that make an action request look like a question —
# "can you create", "could you please move", "please delete".
_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can|could|would|will|may)\s+(?:you|u)\s+"
    r"(?:please\s+|kindly\s+)?|^\s*please\s+", re.I)

# A path is: a quoted span, a Windows absolute path, a ~/ path, a UNC
# path, or a trailing bare name (workspace-relative).
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_ABS_RE = re.compile(
    r"(?:[a-zA-Z]:[\\/][^\s\"'<>|?*;]*|\\\\[^\s\"'<>|?*;]+|"
    r"~[\\/][^\s\"'<>|?*;]*)")
_TRAILING_WS_RE = re.compile(r"[\s.,;:!?]+$")

# <verb> ... <path-ish tail>. The path capture runs to the end of the
# phrase — trailing "with content"/"containing" is stripped for files.
_CREATE_DIR_RE = re.compile(
    r"\b(?:create|make|new)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+|"
    r"empty\s+)?(?:folder|directory|dir)\s*"
    r"(?:named|called|at|in|inside|under|for)?\s*(.*?)\s*$", re.I | re.S)
_MKDIR_RE = re.compile(r"\bmkdir\s+(.+?)\s*$", re.I | re.S)
_CREATE_FILE_RE = re.compile(
    r"\b(?:create|make|new|touch)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+|"
    r"empty\s+)?(?:file|text file|txt file|note)\s*"
    r"(?:named|called|at|in|inside|under)?\s*(.*?)\s*$", re.I | re.S)
_CREATE_REVERSED_RE = re.compile(
    r"\b(?:create|make)\s+(.+?)\s+(?:folder|directory|dir)\b\s*$",
    re.I | re.S)
_DELETE_RE = re.compile(
    r"\b(?:delete|remove|erase|rm|rmdir)\s+(?:the\s+)?"
    r"(?:file|folder|directory|dir)?\s*(.+?)\s*$", re.I | re.S)
_RENAME_RE = re.compile(
    r"\brename\s+(.+?)\s+(?:to|as|into)\s+(.+?)\s*$", re.I | re.S)
_MOVE_RE = re.compile(
    r"\bmove\s+(.+?)\s+(?:to|into|inside)\s+(.+?)\s*$", re.I | re.S)
_COPY_RE = re.compile(
    r"\b(?:copy|duplicate)\s+(.+?)\s+(?:to|into|inside|as)\s+(.+?)\s*$",
    re.I | re.S)
_WRITE_TO_RE = re.compile(
    r"\b(?:write|save)\s+[\"']([^\"']+)[\"']\s+"
    r"(?:to|into|in)\s+(.+?)\s*$", re.I | re.S)
_ONE_ARG_OP_RE = re.compile(
    r"\b(move|copy|duplicate|rename)\s+(.+?)\s*$", re.I | re.S)
# 'copy that' / 'move it' are acknowledgments, not file ops — no
# resolvable target, so they fall through instead of clarifying.
_PRONOUN_ONLY_RE = re.compile(
    r"^(?:it|that|this|them|him|her|one|something)\b", re.I)
# A path tail that contains a second action clause (verb + object type)
# means the intent classifier missed a compound — "create folder alpha
# and make dir beta" must not execute a literal path named 'alpha and
# make dir beta'. Bail to the model lane for the whole utterance.
_EMBEDDED_OP_RE = re.compile(
    r"\b(?:create|make|mkdir|delete|remove|erase|move|copy|duplicate|"
    r"rename|touch|write|save)\s+(?:a\s+|an\s+|the\s+|new\s+)?"
    r"(?:folder|directory|dir|file|note|txt|doc)\b", re.I)


def _embedded_op(raw: str) -> bool:
    return bool(_EMBEDDED_OP_RE.search(raw or ""))
_CONTENT_RE = re.compile(
    r"\s+(?:with\s+content|containing|that\s+says?|saying)\s+"
    r"[\"']?(.*?)[\"']?\s*$", re.I | re.S)
_RECURSIVE_RE = re.compile(
    r"\b(?:recursive(?:ly)?|and\s+(?:its\s+)?contents|everything\s+in"
    r"(?:side)?\s+it)\b", re.I)

# Requests that only LOOK like these patterns — guardrails against
# claiming turns that are really about something else.
_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:how do|how to|what is|what's|why|explain|show me|tell me|"
    r"should i|would it|does it|did you|have you)\b", re.I)


def _strip_prefix(text: str) -> str:
    t = str(text or "").strip()
    for _ in range(2):
        t2 = _PREFIX_RE.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return t


def _path_tail(raw: str) -> str:
    """Clean a captured path candidate — strip quotes, connectors, and
    trailing punctuation. Empty string means no usable path."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _QUOTED_RE.search(raw)
    if m:
        return m.group(1).strip()
    m = _ABS_RE.search(raw)
    if m:
        return _TRAILING_WS_RE.sub("", m.group(0)).strip()
    # Leading connectors left over from the grammar ("at", "in").
    raw = re.sub(r"^(?:named|called|at|in|inside|under|to|as)\s+", "",
                 raw, flags=re.I).strip()
    raw = _TRAILING_WS_RE.sub("", raw).strip()
    # A "path" that is really more grammar — articles/conjunctions/
    # pronouns mean the tail was not a path at all. Word tokens only:
    # "a.txt" is a filename, not the article "a".
    if not raw or re.fullmatch(
            r"(?:a|an|the|it|that|this|there|here|one|something)"
            r"(?:\s.*)?", raw, re.I):
        return ""
    if raw.lower() in {"file", "folder", "directory", "dir"}:
        # 'delete the folder' names a type, not a target.
        return ""
    if re.match(r"(?:me|him|her|us|them|my|your|his|its|our|their|"
                r"you)\b", raw, re.I):
        # Purpose clause, not a path — 'create a folder for my project'.
        return ""
    return raw


# Windows-style absolute paths (drive-letter or UNC) are absolute even
# when the host OS disagrees — on POSIX Path("D:\\x").is_absolute() is
# False, which would wrongly resolve the path INSIDE the workspace and
# skip the outside-root approval gate.
_WIN_ABS_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|\\\\)")


def _resolve(raw: str, workspace: Path,
             extra_roots: Callable | None) -> tuple[Path, bool]:
    """Absolute → itself; ~/ → home; bare → workspace-relative.
    Returns (resolved, inside_roots)."""
    from .tools.filesystem import _path_base
    raw = str(raw).strip()
    if raw.startswith("~"):
        cand = Path(raw).expanduser().resolve()
    else:
        p = Path(raw)
        if p.is_absolute():
            cand = p.resolve()
        elif _WIN_ABS_RE.match(raw):
            cand = p  # verbatim — cannot be inside this workspace
        else:
            cand = (Path(workspace) / p).resolve()
    return cand, _path_base(workspace, cand, extra_roots) is not None


_LOC_TAIL_RE = re.compile(
    r"^(.*?)\s+(?:in|inside|under|within|at)\s+(.+?)\s*$", re.I | re.S)
_WORKSPACE_WORD_RE = re.compile(
    r"^(?:the\s+)?(?:my\s+|this\s+|current\s+)?"
    r"(?:workspace|work\s*space|project|repo|repository|"
    r"(?:workspace|project|repo)\s+(?:root|folder|directory)|"
    r"folder|directory|dir)\.?\s*$", re.I)


def _split_location(name_raw: str, workspace: Path,
                    extra_roots: Callable | None
                    ) -> tuple[str, Path] | None:
    """Split 'name in <place>' into (name, parent_dir). Returns None when
    the tail is not a resolvable place — a folder legitimately named
    'work in progress' must not be split apart."""
    m = _LOC_TAIL_RE.match(name_raw or "")
    if not m:
        return None
    name = m.group(1).strip().strip("\"'")
    if not name or Path(name).is_absolute() or _WIN_ABS_RE.match(name):
        return None
    # Light clean only — the location is a place clause ('the workspace
    # root'), not a path; _path_tail would reject it outright.
    loc_raw = _TRAILING_WS_RE.sub("", m.group(2).strip().strip("\"'"))
    if not name or not loc_raw or _embedded_op(loc_raw):
        return None
    if _WORKSPACE_WORD_RE.fullmatch(loc_raw):
        return name, Path(workspace).resolve()
    parent, _inside = _resolve(loc_raw, workspace, extra_roots)
    # Outside-root parents are allowed — the plan's outside_root flag
    # still routes them through the explicit approval gate.
    if parent.is_dir():
        return name, parent
    return None


def parse_local_action(text: str, *, workspace: Path | str,
                       extra_roots: Callable | None = None
                       ) -> ActionPlan | None:
    """Map bounded computer-task phrasings to an executable plan.

    Returns ``None`` for anything outside the grammar — questions,
    code requests, ambiguous paths — so those fall to the normal lane.
    A plan with ``clarify`` set means the intent is certain but the
    target path is missing; the lane should ask, not guess.
    """
    if _FALSE_POSITIVE_RE.search(str(text or "")):
        return None
    t = _strip_prefix(text)
    if not t or len(t) > 600:
        return None
    ws = Path(workspace)

    # _mk computes outside_root — a plan is outside when ANY resolved
    # path escapes the registered roots.
    def _mk(kind: str, tool: str, perm: str, action_text: str,
            resolved: dict[str, tuple[Path, bool]],
            params: dict[str, str]) -> ActionPlan:
        res_str = {k: str(v) for k, (v, _in) in resolved.items()}
        outside = any(not _in for _, _in in resolved.values())
        p = dict(params)
        p.update(res_str)
        display = res_str.get("path") or res_str.get("dst") or ""
        return ActionPlan(
            kind=kind, tool=tool, permission=perm, params=p,
            resolved=res_str, outside_root=outside,
            action_text=action_text, display=str(display))

    # write/save with quoted content — 'write "hello" to note.txt'.
    m = _WRITE_TO_RE.search(t)
    if m:
        if _embedded_op(m.group(2)):
            return None
        content, dst_raw = m.group(1), _path_tail(m.group(2))
        if not dst_raw:
            return ActionPlan(
                kind="write", tool="write_file",
                permission="filesystem.write",
                clarify="What file should I write to?",
                action_text="write file")
        dst = _resolve(dst_raw, ws, extra_roots)
        return _mk("write", "write_file", "filesystem.write",
                   f"write to {dst_raw}", {"path": dst},
                   {"content": content,
                    "expected_size": len(content.encode("utf-8"))})

    # rename / move / copy — two-path forms first (they're unambiguous).
    for rx, kind, tool in ((_RENAME_RE, "rename", "fs_move"),
                           (_MOVE_RE, "move", "fs_move"),
                           (_COPY_RE, "copy", "fs_copy")):
        m = rx.search(t)
        if not m:
            continue
        if _embedded_op(m.group(1)) or _embedded_op(m.group(2)):
            return None
        src_raw = _path_tail(m.group(1))
        dst_raw = _path_tail(m.group(2))
        if not src_raw:
            return None
        src = _resolve(src_raw, ws, extra_roots)
        if not dst_raw:
            label = {"rename": "rename it to",
                     "move": "move it to", "copy": "copy it to"}[kind]
            return ActionPlan(
                kind=kind, tool=tool, permission="filesystem.write",
                params={"src": str(src[0])}, resolved={"src": str(src[0])},
                outside_root=not src[1],
                action_text=f"{kind} {src_raw}",
                clarify=f"What should I {label}?", display=str(src[0]))
        dst = _resolve(dst_raw, ws, extra_roots)
        return _mk(kind, tool, "filesystem.write",
                   f"{kind} {src_raw} to {dst_raw}",
                   {"src": src, "dst": dst}, {})

    # Single-operand move/copy/rename — the op is certain but the
    # destination is missing: clarify, never guess. Pronoun-only tails
    # ('copy that', 'move it') have no resolvable target → fall through.
    m = _ONE_ARG_OP_RE.search(t)
    if m:
        verb = m.group(1).lower()
        tail = m.group(2).strip()
        if _embedded_op(tail):
            return None
        if not _PRONOUN_ONLY_RE.match(tail):
            kind = "copy" if verb in ("copy", "duplicate") else \
                ("rename" if verb == "rename" else "move")
            tool = "fs_copy" if kind == "copy" else "fs_move"
            src_raw = _path_tail(tail)
            if not src_raw:
                return ActionPlan(
                    kind=kind, tool=tool, permission="filesystem.write",
                    clarify=f"What should I {kind}?", action_text=kind)
            src = _resolve(src_raw, ws, extra_roots)
            return ActionPlan(
                kind=kind, tool=tool, permission="filesystem.write",
                params={"src": str(src[0])},
                resolved={"src": str(src[0])},
                outside_root=not src[1],
                action_text=f"{kind} {src_raw}",
                clarify=f"What should I {kind} {src_raw} to?",
                display=str(src[0]))

    m = _DELETE_RE.search(t)
    if m:
        body = m.group(1)
        recursive = bool(_RECURSIVE_RE.search(body))
        body = _RECURSIVE_RE.sub("", body)
        if _embedded_op(body):
            return None
        raw = _path_tail(body)
        if not raw:
            return ActionPlan(
                kind="delete", tool="fs_delete",
                permission="filesystem.delete",
                clarify="What should I delete?", action_text="delete")
        path = _resolve(raw, ws, extra_roots)
        return _mk("delete", "fs_delete", "filesystem.delete",
                   f"delete {raw}", {"path": path},
                   {"recursive": recursive})

    m = (_CREATE_DIR_RE.search(t) or _CREATE_REVERSED_RE.search(t)
         or _MKDIR_RE.search(t))
    if m:
        if _embedded_op(m.group(1)):
            return None
        raw = _path_tail(m.group(1))
        if not raw:
            return ActionPlan(
                kind="mkdir", tool="fs_mkdir",
                permission="filesystem.write",
                clarify="Where should I create the folder?",
                action_text="create folder")
        # 'folder named X in <place>' — the location clause is a parent
        # directory, not part of the name (observed: "…in the workspace
        # root" was being folded into the folder's literal name). Split
        # the raw capture: _path_tail would collapse 'X in D:\dir' to
        # just the absolute path and lose the folder name entirely.
        split = _split_location(m.group(1), ws, extra_roots)
        target = str(split[1] / split[0]) if split else raw
        path = _resolve(target, ws, extra_roots)
        return _mk("mkdir", "fs_mkdir", "filesystem.write",
                   f"create folder {raw}", {"path": path}, {})

    m = _CREATE_FILE_RE.search(t)
    if m:
        body = m.group(1)
        content = ""
        cm = _CONTENT_RE.search(body)
        if cm:
            content = cm.group(1)
            body = body[:cm.start()]
        if _embedded_op(body):
            return None
        raw = _path_tail(body)
        if not raw:
            return ActionPlan(
                kind="write", tool="write_file",
                permission="filesystem.write",
                clarify="What file should I create?",
                action_text="create file")
        split = _split_location(body, ws, extra_roots)
        target = str(split[1] / split[0]) if split else raw
        path = _resolve(target, ws, extra_roots)
        params = {"content": content,
                  "expected_size": len(content.encode("utf-8"))}
        return _mk("write", "write_file", "filesystem.write",
                   f"create file {raw}", {"path": path}, params)

    return None


# ---------------------------------------------------------------------------
# Execution — the same lifecycle for the deterministic lane and approval
# resume: capability check -> permission -> execute -> verify -> evidence.
# ---------------------------------------------------------------------------

def execute_plan(plan: ActionPlan, *, tools, ledger=None,
                 approved: bool = False, task_id: str = "",
                 mission_id: str = "") -> dict[str, Any]:
    """Run a plan. Returns {status, text, entry, tool_result}.

    statuses: verified | awaiting_approval | denied | failed |
              clarify | unavailable
    ``text`` is always truthful — it describes what actually happened,
    never what was intended.
    """
    entry = ledger.begin(
        kind=plan.kind, action=plan.action_text,
        capability="filesystem", tool=plan.tool, params=plan.params,
        task_id=task_id, mission_id=mission_id) if ledger else None

    def _close(status: str, text: str, *, permission: str = "",
               verification: str = "", verified=None, artifact: str = "",
               failure: str = "", tool_result: str = "") -> dict:
        if ledger and entry:
            ledger.finish(
                entry["id"], status=status, permission=permission,
                verification=verification, verified=verified,
                artifact=artifact, failure=failure)
        return {"status": status, "text": text,
                "entry": entry, "tool_result": tool_result}

    if plan.clarify:
        return _close("clarify", plan.clarify)

    # Capability check — the tool must exist and be enabled.
    perm, mode = tools.permission_for(plan.tool)
    if not perm:
        return _close(
            "unavailable",
            f"I can't {plan.action_text} — the {plan.tool} tool isn't "
            "installed or enabled on this machine.")
    spec = tools.get(plan.tool)
    if spec is not None and not tools.is_enabled(plan.tool):
        return _close(
            "unavailable",
            f"I can't {plan.action_text} — {plan.tool} is disabled in "
            "the Tool Manager. Nothing was changed.")
    if mode == "deny":
        return _close(
            "denied",
            f"I can't {plan.action_text} — the {perm} permission is set "
            "to deny in the active profile. Nothing was changed.",
            permission="deny")

    # Outside the registered roots always asks — an explicit user-named
    # absolute path is not license to write anywhere unattended.
    # Idempotent pre-check — cheap read that keeps the response honest
    # ("already exists" / "already gone") without mutating anything.
    pre = Path(plan.resolved.get("path", "")) if "path" in plan.resolved \
        else None
    if plan.kind == "mkdir" and pre is not None and pre.is_dir():
        return _close("verified",
                      f"{plan.display} already exists — nothing to do.",
                      permission="policy",
                      verification="directory exists", verified=True,
                      artifact=plan.display)
    if plan.kind == "delete" and pre is not None and not pre.exists():
        return _close("verified",
                      f"{plan.display} doesn't exist — nothing deleted.",
                      permission="policy",
                      verification="target absent", verified=True,
                      artifact=plan.display)

    needs_approval = plan.outside_root or mode in ("ask", "creator")
    if needs_approval and not approved:
        scope = " (outside the workspace boundary)" \
            if plan.outside_root else ""
        return _close(
            "awaiting_approval",
            f"I can {plan.action_text}{scope}, but it needs {perm} "
            "approval first.",
            permission="awaiting_approval")

    # Execute — in-root ops go through the registry so the permission
    # manager records the decision; outside-root approved ops run the
    # same verified implementation directly.
    if plan.outside_root:
        from .action_ledger import run_filesystem, verify_filesystem
        try:
            run_filesystem(plan.kind, plan.params)
            ok, detail = verify_filesystem(plan.kind, plan.params)
            if not ok:
                return _close(
                    "unverified",
                    f"I ran the {plan.action_text} operation, but "
                    f"verification failed: {detail}.",
                    verification=detail, verified=False)
            return _close(
                "verified",
                _success_text(plan),
                permission="approved", verification=detail,
                verified=True, artifact=plan.display,
                tool_result=f"{plan.kind.upper()}_OK {plan.display}")
        except (OSError, ValueError, RuntimeError) as exc:
            return _close(
                "failed",
                f"I tried to {plan.action_text}, but it failed: "
                f"{exc}. Nothing was changed.",
                permission="approved", failure=str(exc))

    result = tools.execute(plan.tool, plan.params, approved=approved)
    if result.startswith("APPROVAL_REQUIRED"):
        return _close(
            "awaiting_approval",
            f"I can {plan.action_text}, but it needs {perm} approval "
            "first.", permission="awaiting_approval",
            tool_result=result)
    if result.startswith("PERMISSION_DENIED"):
        return _close(
            "denied",
            f"I can't {plan.action_text} — {result}. Nothing was "
            "changed.", permission="deny", tool_result=result)
    if result.startswith(("ERROR", "TOOL_", "SCOPE_DENIED")):
        return _close(
            "failed",
            f"I tried to {plan.action_text}, but it failed: "
            f"{result}. Nothing was changed.",
            failure=result, tool_result=result)
    # The tool handlers verify on disk before returning *_OK; the marker
    # is the evidence, not the prose around it.
    if "_OK" in result:
        return _close(
            "verified", _success_text(plan),
            permission="approved" if approved else "policy",
            verification=result[:160], verified=True,
            artifact=plan.display, tool_result=result)
    return _close(
        "unverified",
        f"The {plan.action_text} operation ran, but the result was "
        f"inconclusive: {result[:160]}. Treating it as unverified.",
        verification=result[:160], verified=False, tool_result=result)


def _success_text(plan: ActionPlan) -> str:
    d = plan.display
    return {
        "mkdir": f"Created {d}.",
        "write": f"Created {d}.",
        "delete": f"Deleted {d}.",
        "move": f"Moved to {d}.",
        "rename": f"Renamed to {d}.",
        "copy": f"Copied to {d}.",
    }.get(plan.kind, f"Done — {d}.")
