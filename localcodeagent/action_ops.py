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

import json
import os
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

# Artifact handoff + GitHub delivery — 'give me the file', 'upload the
# installer to github', 'download X from the latest build'. These claim
# before the plain-URL download lane: 'download X from github' has no
# URL and would otherwise fall through to the model.
_ARTIFACT_GIVE_RE = re.compile(
    r"^\s*(?:give|hand|send|get)\s+me\s+"
    r"(?:the\s+|a\s+|an\s+|my\s+|that\s+)?(.+?)\s*$", re.I | re.S)
_ARTIFACT_ASK_RE = re.compile(
    r"^\s*where(?:'s|\s+is)\s+(?:the\s+|my\s+|that\s+)?(.+?)\s*$",
    re.I | re.S)
_GH_UPLOAD_RE = re.compile(
    r"^\s*(?:upload|publish|share)\s+(.+?)\s+(?:to|onto)\s+"
    r"(?:the\s+)?github\b\s*(.*)$", re.I | re.S)
_GH_CANCEL_UPLOAD_RE = re.compile(
    r"^\s*(?:cancel|stop|abort)\s+(?:the\s+|my\s+)?"
    r"(?:github\s+|asset\s+|release\s+)*upload\b", re.I)
_GH_PUT_RELEASE_RE = re.compile(
    r"^\s*put\s+(.+?)\s+on\s+(?:the\s+)?(?:github\s+)?release\b\s*(.*)$",
    re.I | re.S)
_GH_DOWNLOAD_RE = re.compile(
    r"^\s*(?:download|get|fetch|grab|retrieve)\s+"
    r"(?:the\s+|a\s+|an\s+|my\s+|that\s+)?(.+?)\s+from\s+"
    r"(?:the\s+)?github\b\s*(.*)$", re.I | re.S)
_GH_RUN_ART_RE = re.compile(
    r"^\s*(?:download|get|fetch|grab)\s+(?:the\s+)?(.+?)\s+"
    r"(?:artifact\s+)?from\s+(?:the\s+)?(?:latest\s+|last\s+)?"
    r"(?:successful\s+)?(?:build|ci|workflow(?:\s+run)?|"
    r"actions(?:\s+run)?|run)s?\s*$", re.I | re.S)
_GH_REPO_FILE_RE = re.compile(
    r"^\s*(?:get|fetch|download|retrieve)\s+(?:the\s+)?"
    r"([\w.\-/\\]+\.[\w.]+)\s+from\s+(?:the\s+)?"
    r"(?:repo|repository|github)\b\s*$", re.I)
_GH_RELEASE_TAG_RE = re.compile(
    r"(?:release\s+|tag\s+|v)?(v?\d[\w.-]*)", re.I)
# 'to github repo owner/name' — explicit target repo wins over the
# workspace's connected remote.
_GH_REPO_SPEC_RE = re.compile(
    r"(?:repo|repository)\s+([\w.-]+/[\w.-]+)", re.I)
# Words that make a bare 'give me X' an artifact request — a name with
# an extension or a known output noun. Everything else falls through.
_ARTIFACT_WORDS = {
    "file", "files", "artifact", "artifacts", "installer", "setup",
    "zip", "archive", "report", "log", "logs", "pdf", "doc", "docx",
    "xlsx", "csv", "json", "txt", "exe", "msi", "image", "picture",
    "photo", "screenshot", "audio", "video", "build", "package",
    "release", "spreadsheet", "presentation", "readme", "download",
    "downloads", "output", "export",
}
_ARTIFACT_PRONOUN_RE = re.compile(
    r"^(?:it|that|this|them|the\s+(?:file|artifact)s?)\s*$", re.I)


def _artifactish(tail: str) -> bool:
    """True when a 'give me X' tail plausibly names a produced file —
    an extension, an art- id, or an output noun. Bounded so 'give me a
    hint'/'hand me a wrench' stay with the model lane."""
    low = tail.strip().lower()
    if not low:
        return False
    if low.startswith("art-") or re.fullmatch(
            r"[\w.\- ]+\.[a-z0-9]{1,6}", low):
        return True
    words = set(re.findall(r"[a-z]+", low))
    return bool(words & _ARTIFACT_WORDS)

# Application lifecycle — resolve at plan time so the approval card and
# ledger carry the real executable, not just the user's words.
_APP_OPEN_RE = re.compile(
    r"^\s*(?:open|launch|start|bring\s+up)\s+(?:up\s+)?"
    r"(?:the\s+)?(.+?)\s*$", re.I | re.S)
_APP_CLOSE_RE = re.compile(
    r"^\s*(?:close|quit|exit|stop)\s+(?:the\s+)?(.+?)\s*$", re.I | re.S)
_APP_RESTART_RE = re.compile(
    r"^\s*(?:restart|relaunch|reopen|reboot)\s+(?:the\s+)?(.+?)\s*$",
    re.I | re.S)
_APP_STATUS_RE = re.compile(
    r"^\s*(?:is|check\s+(?:if|whether)|see\s+if)\s+(.+?)\s+"
    r"(?:is\s+)?(?:running|open|up)\s*[?!.]*$", re.I | re.S)
_APP_WINDOW_RE = re.compile(
    r"^\s*(minimize|maximize|minimise|maximise|restore)\s+"
    r"(?:the\s+)?(?:window\s+(?:of|for)\s+)?(.+?)\s*$", re.I | re.S)
# Tails that are conversation, not an app name — 'close the deal',
# 'start over', 'open up about…'.
# Bare idioms — the whole tail is a conversational target.
_APP_NON_TARGET_RE = re.compile(
    r"^(?:it|that|this|them|over|up|down|deal|shop|distance|gap|"
    r"mind|eyes|mouth|a\s+conversation|my\s+heart|yourself|myself)"
    r"\.?$", re.I)
# A tail that opens with a preposition/possessive is a clause, not an
# app name — 'about your feelings', 'with care', 'my day'.
_APP_CLAUSE_HEAD_RE = re.compile(
    r"^(?:about|with|on|upon|for|of|my|your|his|her|our|their|me|you|"
    r"us|the\s+way|how)\b", re.I)
# 'open the folder X' / 'open the file X' / 'open the installer' are
# filesystem/installer targets handled below — don't swallow them as
# app names.
_APP_FS_HEAD_RE = re.compile(
    r"^(?:folder|directory|dir|file|note|document|installer)\b", re.I)

# Installers — 'install <path>' or 'install <product>' (artifact must
# already exist in Downloads; we never guess a URL). 'run the installer
# [I downloaded]' picks the newest Downloads artifact.
_INSTALL_RE = re.compile(
    r"^\s*(?:install|reinstall|upgrade|update|set\s*up|setup)\s+"
    r"(?:the\s+|this\s+|that\s+)?(.+?)\s*$", re.I | re.S)
_RUN_INSTALLER_RE = re.compile(
    r"^\s*(?:run|open|start|execute|launch)\s+(?:the\s+|my\s+|that\s+|"
    r"this\s+)?installer\b(.*)$", re.I | re.S)
_DOWNLOADED_TAIL_RE = re.compile(
    r"^\s*(?:(?:i|we)\s+(?:just\s+)?downloaded|from\s+downloads?|"
    r"in\s+(?:my\s+)?downloads?(?:\s+folder)?)\b(.*)$", re.I | re.S)

# Downloads — 'download https://… [to <path>]' is deterministic; a bare
# 'download Python' has no URL and falls to the model lane to research.
_DOWNLOAD_RE = re.compile(r"^\s*download\s+(.+?)\s*$", re.I | re.S)
_CANCEL_DL_RE = re.compile(
    r"^\s*cancel\s+(?:the\s+|my\s+|that\s+)?(?:file\s+)?download\b",
    re.I)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_DL_DEST_RE = re.compile(
    r"^(.*?)\s+(?:to|into|in|inside|under)\s+(.+?)\s*$", re.I | re.S)

# File discovery / inspection / archive — read intents are
# filesystem.read (no approval in routine profiles); archive/extract
# mutate, so they carry filesystem.write.
_FIND_CONTAINING_RE = re.compile(
    r"^\s*(?:find|locate|show)\s+(?:all\s+|any\s+)?(?:the\s+)?files?\s+"
    r"(?:containing|with|that\s+contain(?:s)?)\s+(.+?)\s*$", re.I | re.S)
_SEARCH_TEXT_RE = re.compile(
    r"^\s*(?:search|grep)\s+(?:the\s+|inside\s+|within\s+)?"
    r"(?:files?|workspace|code|codebase|project)\s+for\s+(.+?)\s*$",
    re.I | re.S)
_FIND_FILE_RE = re.compile(
    r"^\s*(?:find|locate|search\s+for|look\s+for)\s+(?:all\s+|any\s+)?"
    r"(?:the\s+)?(?:file|files|folder|folders|directories|dir|dirs)"
    r"\b\s*(.*?)\s*$", re.I | re.S)
_FIND_BARE_RE = re.compile(r"^\s*(?:find|locate)\s+(.+?)\s*$", re.I | re.S)
_INSPECT_RE = re.compile(
    r"^\s*(?:inspect|stat|info\s+on|file\s+info(?:rmation)?\s+(?:for|on|"
    r"of)|details?\s+(?:for|on|of))\s+(?:the\s+)?"
    r"(?:file|folder|directory|dir)?\s*(.+?)\s*$", re.I | re.S)
_CHECKSUM_RE = re.compile(
    r"^\s*(?:checksum|check\s*sum|sha\s*-?\s*256|hash)\s+"
    r"(?:of\s+|for\s+)?(?:the\s+)?(?:file\s+)?(.+?)\s*$", re.I | re.S)
_ARCHIVE_RE = re.compile(
    r"^\s*(?:zip|archive|compress)\s+(?:up\s+)?(?:the\s+)?"
    r"(?:file|folder|directory|dir|files)?\s*(.*?)\s*$", re.I | re.S)
_EXTRACT_RE = re.compile(
    r"^\s*(?:unzip|extract|unarchive|decompress|expand)\s+(?:the\s+)?"
    r"(?:archive|zip|file|contents\s+of)?\s*(.*?)\s*$", re.I | re.S)
_TO_TAIL_RE = re.compile(
    r"^(.*?)\s+(?:to|into|as|called|named)\s+(.+?)\s*$", re.I | re.S)
# 'find <glob>' claims only pattern-shaped tails — 'find my keys' is
# not a filesystem search.
_GLOBISH_RE = re.compile(r"[~\w.\-\\/*?]+")


def _app_plan(kind: str, name_raw: str, *, state: str = "",
              workspace: Path | None = None
              ) -> ActionPlan | None:
    """Build an app-control plan. Returns None when the tail cannot
    possibly be an application target (conversation/file ops)."""
    name = _TRAILING_WS_RE.sub("", (name_raw or "").strip().strip("\"'"))
    if not name or _APP_NON_TARGET_RE.match(name) or \
            _APP_CLAUSE_HEAD_RE.match(name) or _embedded_op(name):
        return None
    if len(name) > 200:
        return None
    # 'open notes.txt' / 'open D:\doc.pdf' — an existing file opens with
    # its default handler through the shell (launch_verified handles
    # non-.exe targets via os.startfile).
    if kind == "launch" and workspace is not None:
        cand, _in = _resolve(name, workspace, None)
        if cand.is_file() and cand.suffix.lower() != ".exe":
            return ActionPlan(
                kind="launch", tool="computer_app_launch",
                permission="application.launch",
                params={"app": str(cand)},
                resolved={}, action_text=f"open {name}",
                display=cand.name)
    tool = {"launch": "computer_app_launch",
            "close": "computer_app_close",
            "restart": "computer_app_restart",
            "status": "computer_app_status",
            "window": "computer_app_window"}[kind]
    perm = ("application.launch" if kind == "launch"
            else "application.manage" if kind in ("close", "restart")
            else "desktop.view" if kind == "status"
            else "desktop.control")
    from .computer_use import apps
    resolved = apps.resolve_app(name)
    display = resolved["name"] if resolved.get("ok") else name
    verb = {"launch": "open", "close": "close", "restart": "restart",
            "status": "check whether", "window": ""}[kind]
    if kind == "window":
        action = f"{state or 'manage'} window for {name}"
    else:
        action = f"{verb} {name}" if verb else f"manage {name}"
    params: dict[str, str] = {"app": name}
    if state:
        params["state"] = state
    return ActionPlan(kind=kind, tool=tool, permission=perm,
                      params=params, resolved={},
                      action_text=action, display=display,
                      clarify="")


def _install_plan(target_raw: str, workspace: Path,
                  extra_roots: Callable | None,
                  *, from_downloads: bool = False) -> ActionPlan:
    """Resolve 'install X' to an existing installer artifact.

    Order: explicit existing file → Downloads match by product tokens →
    newest Downloads artifact when no name was given → clarify. The
    plan never fabricates a path — no artifact, no plan to execute.
    """
    from .computer_use import apps
    target = (target_raw or "").strip().strip("\"'")
    target = _TRAILING_WS_RE.sub("", target)
    path = ""
    hint = ""
    if target:
        cand, _inside = _resolve(target, workspace, extra_roots)
        if cand.is_file():
            path = str(cand)
            hint = cand.stem
        elif _WIN_ABS_RE.match(target) or cand.is_absolute() and \
                (os.sep in target or "/" in target):
            # Named an explicit path that doesn't exist — let the tool
            # report the truth rather than guessing another file.
            path = str(cand)
            hint = cand.stem
        else:
            found = apps.find_installer(target)
            if found.get("ok"):
                path = found["path"]
                hint = target
            else:
                return ActionPlan(
                    kind="install", tool="computer_install",
                    permission="packages.install",
                    clarify=(f"I don't see a '{target}' installer in "
                             "your Downloads folder — download it first "
                             "or give me the file path."),
                    action_text=f"install {target}", display=target)
    else:
        found = apps.latest_installer()
        if not found.get("ok"):
            return ActionPlan(
                kind="install", tool="computer_install",
                permission="packages.install",
                clarify=("I couldn't find an installer in your Downloads "
                         "folder — which file should I run?"),
                action_text="run the installer")
        path = found["path"]
        hint = found.get("name", "")
    name = Path(path).name if path else target
    return ActionPlan(
        kind="install", tool="computer_install",
        permission="packages.install",
        params={"path": path, "name_hint": hint},
        resolved={"path": path},
        action_text=f"install {name}", display=name)


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
            # Users type Windows-style separators on any host —
            # 'docs\inner.zip' is nested, not a literal backslash name.
            cand = (Path(workspace) / raw.replace("\\", "/")).resolve()
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

    # Application lifecycle intents — checked before filesystem ops so
    # 'open Notepad' claims the app lane. Non-app tails ('close the
    # deal', 'open the folder x') are rejected by _app_plan and the
    # 'open folder/file …' head guard.
    m = _APP_STATUS_RE.match(t)
    if m:
        plan = _app_plan("status", m.group(1))
        if plan:
            return plan
    m = _APP_WINDOW_RE.match(t)
    if m:
        state = {"minimise": "minimize", "maximise": "maximize"}.get(
            m.group(1).lower(), m.group(1).lower())
        plan = _app_plan("window", m.group(2), state=state)
        if plan:
            return plan
    m = _APP_RESTART_RE.match(t)
    if m:
        plan = _app_plan("restart", m.group(1))
        if plan:
            return plan
    # 'cancel/stop the upload' must claim before the app-close lane
    # ('stop the github upload' is not 'close the app').
    m = _GH_CANCEL_UPLOAD_RE.match(t)
    if m:
        return ActionPlan(
            kind="github_upload_cancel", tool="github_cancel_upload",
            permission="github.read", params={},
            action_text="cancel the GitHub upload", display="upload")
    m = _APP_CLOSE_RE.match(t)
    if m:
        plan = _app_plan("close", m.group(1))
        if plan:
            return plan
    m = _APP_OPEN_RE.match(t)
    if m and not _APP_FS_HEAD_RE.match(m.group(1).strip()):
        plan = _app_plan("launch", m.group(1), workspace=ws)
        if plan:
            return plan

    # GitHub delivery — upload / download / repo-file fetch. Placed
    # before the download lane: 'download X from github' has no URL.
    m = _GH_UPLOAD_RE.match(t) or _GH_PUT_RELEASE_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", (m.group(1) or "").strip())
        rest = (m.group(2) or "").strip()
        repo_m = _GH_REPO_SPEC_RE.search(rest)
        # Strip the repo spec before tag search — 'afterburn25' would
        # otherwise satisfy the leading 'v?\d' tag fragment.
        rest_nt = _GH_REPO_SPEC_RE.sub("", rest) if repo_m else rest
        tag_m = _GH_RELEASE_TAG_RE.search(rest_nt)
        tag = tag_m.group(1) if tag_m else ""
        name = "" if _ARTIFACT_PRONOUN_RE.match(tail) else \
            re.sub(r"^(?:the|a|an|my)\s+", "", tail, flags=re.I)
        if not name:
            name = "latest"
        params = {"name": name}
        if tag:
            params["tag"] = tag
        if repo_m:
            params["repo"] = repo_m.group(1)
        return ActionPlan(
            kind="github_upload", tool="github_upload_release_asset",
            permission="github.write", params=params,
            action_text=f"upload {name} to github"
                        + (f" (release {tag})" if tag else ""),
            display=name)
    m = _GH_RUN_ART_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", (m.group(1) or "").strip())
        name = "" if _ARTIFACT_PRONOUN_RE.match(tail) else \
            re.sub(r"^(?:the|a|an|my)\s+|artifact\s*$", "",
                   tail, flags=re.I)
        return ActionPlan(
            kind="github_download", tool="github_download_run_artifact",
            permission="github.read",
            params={"name": name},
            action_text=f"download the {name or 'build'} artifact "
                        "from the latest successful run",
            display=name or "build artifact")
    m = _GH_DOWNLOAD_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", (m.group(1) or "").strip())
        rest = (m.group(2) or "").strip()
        repo_m = _GH_REPO_SPEC_RE.search(rest)
        rest_nt = _GH_REPO_SPEC_RE.sub("", rest) if repo_m else rest
        tag_m = _GH_RELEASE_TAG_RE.search(rest_nt)
        tag = tag_m.group(1) if tag_m else ""
        # 'get README.md from github' — a repo file, not a release asset.
        if "/" in tail or re.fullmatch(r"[\w.\-]+\.[a-z0-9]{1,5}",
                                       tail, re.I):
            if not tag:
                return ActionPlan(
                    kind="github_file", tool="github_get_repo_file",
                    permission="github.read", params={"path": tail},
                    action_text=f"get {tail} from the repository",
                    display=tail)
        name = "" if _ARTIFACT_PRONOUN_RE.match(tail) else \
            re.sub(r"^(?:the|a|an|my)\s+", "", tail, flags=re.I)
        params = {"name": name}
        if tag:
            params["tag"] = tag
        if repo_m:
            params["repo"] = repo_m.group(1)
        return ActionPlan(
            kind="github_download",
            tool="github_download_release_asset",
            permission="github.read", params=params,
            action_text=f"download {name or 'the asset'} from github"
                        + (f" release {tag}" if tag else ""),
            display=name or "release asset")
    m = _GH_REPO_FILE_RE.match(t)
    if m:
        rel = m.group(1).strip()
        return ActionPlan(
            kind="github_file", tool="github_get_repo_file",
            permission="github.read", params={"path": rel},
            action_text=f"get {rel} from the repository", display=rel)

    # Artifact handoff — 'give me the file', 'send me the zip',
    # "where's the installer". Pronouns resolve to the latest artifact.
    m = _ARTIFACT_ASK_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", (m.group(1) or "").strip())
        if _artifactish(tail):
            name = "" if _ARTIFACT_PRONOUN_RE.match(tail) else tail
            return ActionPlan(
                kind="artifact_show", tool="artifact_show",
                permission="filesystem.read",
                params={"name": name or "latest"},
                action_text=f"hand over {name or 'the latest artifact'}",
                display=name or "latest artifact")
        return None
    m = _ARTIFACT_GIVE_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", (m.group(1) or "").strip())
        tail = re.sub(r"^(?:a\s+)?download\s+(?:link\s+for\s+|of\s+)",
                      "", tail, flags=re.I).strip() or tail
        if not _artifactish(tail):
            return None
        name = "" if _ARTIFACT_PRONOUN_RE.match(tail) else tail
        return ActionPlan(
            kind="artifact_show", tool="artifact_show",
            permission="filesystem.read",
            params={"name": name or "latest"},
            action_text=f"hand over {name or 'the latest artifact'}",
            display=name or "latest artifact")

    # Installers — 'install <path|product>', 'run the installer [I
    # downloaded]'. The artifact must already exist on disk; a bare
    # product name resolves only against Downloads, never a URL guess.
    m = _RUN_INSTALLER_RE.match(t)
    if m:
        tail = _DOWNLOADED_TAIL_RE.sub("", m.group(1) or "").strip()
        tail = re.sub(
            r"^(?:for|of|at)\s+", "", tail, flags=re.I).strip()
        return _install_plan(_path_tail(tail) or tail, ws, extra_roots)
    m = _INSTALL_RE.match(t)
    if m:
        tail = m.group(1)
        if _PRONOUN_ONLY_RE.match(tail) or \
                _APP_CLAUSE_HEAD_RE.match(tail):
            return None
        tail = re.sub(r"^(?:(?:the|a|an)\s+)?program\s+", "", tail,
                      flags=re.I).strip()
        if not tail or tail.lower() in (
                "program", "app", "application", "software",
                "installer", "package", "it", "this", "that"):
            return ActionPlan(
                kind="install", tool="computer_install",
                permission="packages.install",
                clarify="What should I install?", action_text="install")
        path_guess = _path_tail(tail)
        if not path_guess and re.match(r"(?:a|an)\b", tail, re.I):
            # 'set up a meeting' — conversation, not software.
            return None
        return _install_plan(path_guess or tail, ws, extra_roots)

    # Downloads — explicit URLs only; bare product names need research
    # and fall to the model lane.
    if _CANCEL_DL_RE.match(t):
        return ActionPlan(
            kind="download_cancel", tool="download_cancel",
            permission="network.read", params={},
            action_text="cancel the download", display="download")
    m = _DOWNLOAD_RE.match(t)
    if m:
        tail = m.group(1)
        um = _URL_RE.search(tail)
        if not um:
            return None
        url = um.group(0)
        rest = (tail[:um.start()] + tail[um.end():]).strip()
        # Leading filler/connectors: 'the file to X' / 'to X' / 'into X'.
        rest = re.sub(
            r"^(?:(?:the|a|an|my|this)\s+)?(?:file|archive|installer|"
            r"package|artifact)s?\s+", "", rest, flags=re.I).strip()
        dest_raw = ""
        dm = _DL_DEST_RE.match(rest) if rest else None
        if dm:
            a, b = dm.group(1).strip(), dm.group(2).strip()
            dest_raw = b or a
        elif rest:
            rest = re.sub(
                r"^(?:from|to|into|in|inside|under|at|as)\s+", "",
                rest, flags=re.I).strip()
            # 'download <url> mydir' — accept only a clean path token;
            # clause-y leftovers ('the file from') are not destinations.
            if re.fullmatch(r"[~\w.\-\\/:]+", rest):
                dest_raw = rest
        dest_raw = _TRAILING_WS_RE.sub("", dest_raw.strip("\"'"))
        params: dict[str, str] = {"url": url}
        resolved: dict[str, tuple[Path, bool]] = {}
        outside = False
        if dest_raw:
            dest = _resolve(dest_raw, ws, extra_roots)
            resolved["dest"] = dest
            outside = not dest[1]
            params["dest"] = str(dest[0])
        return ActionPlan(
            kind="download", tool="download_file",
            permission="network.read", params=params,
            resolved={k: str(v) for k, (v, _i) in resolved.items()},
            outside_root=outside,
            action_text=f"download {url}"
                        + (f" to {dest_raw}" if dest_raw else ""),
            display=Path(url.split("?")[0].rstrip("/")).name or url)

    # File discovery — content search ('find files containing X',
    # 'search files for X') then filename search ('find files named
    # *.log', 'find notes.txt').
    m = _FIND_CONTAINING_RE.match(t) or _SEARCH_TEXT_RE.match(t)
    if m:
        query = _TRAILING_WS_RE.sub("", m.group(1).strip().strip("\"'"))
        if not query:
            return ActionPlan(
                kind="search_text", tool="search_text",
                permission="filesystem.read",
                clarify="What text should I search for?",
                action_text="search files")
        return ActionPlan(
            kind="search_text", tool="search_text",
            permission="filesystem.read", params={"query": query},
            action_text=f"search files for '{query}'", display=query)
    m = _FIND_FILE_RE.match(t)
    if m:
        tail = re.sub(
            r"^(?:named|called|matching|like|with\s+names?\s+like)\s+",
            "", m.group(1), flags=re.I).strip()
        tail = _TRAILING_WS_RE.sub("", tail.strip("\"'"))
        if not tail or _PRONOUN_ONLY_RE.match(tail):
            return ActionPlan(
                kind="search", tool="search_filename",
                permission="filesystem.read",
                clarify="What filename or pattern should I look for?",
                action_text="find files")
        return ActionPlan(
            kind="search", tool="search_filename",
            permission="filesystem.read", params={"pattern": tail},
            action_text=f"find files '{tail}'", display=tail)
    m = _FIND_BARE_RE.match(t)
    if m:
        tail = _TRAILING_WS_RE.sub("", m.group(1).strip().strip("\"'"))
        looks_file = bool(_GLOBISH_RE.fullmatch(tail)) and any(
            c in tail for c in "*?.\\/:") or tail.startswith("~")
        if looks_file:
            return ActionPlan(
                kind="search", tool="search_filename",
                permission="filesystem.read", params={"pattern": tail},
                action_text=f"find files '{tail}'", display=tail)
        # 'find my keys' — not a filesystem target, fall through.

    # Inspect / checksum — 'inspect report.txt', 'hash installer.exe'.
    m = _CHECKSUM_RE.match(t)
    if m:
        body = (m.group(1) or "").strip()
        if _WORKSPACE_WORD_RE.match(body) or \
                re.search(r"\band\b", body, re.I):
            return None
        raw = _path_tail(body)
        if not raw:
            return ActionPlan(
                kind="inspect", tool="fs_stat",
                permission="filesystem.read",
                clarify="Which file should I hash?",
                action_text="checksum")
        path = _resolve(raw, ws, extra_roots)
        return _mk("inspect", "fs_stat", "filesystem.read",
                   f"checksum {raw}", {"path": path}, {"hash": "true"})
    m = _INSPECT_RE.match(t)
    if m:
        body = (m.group(1) or "").strip()
        # 'inspect the workspace' / 'inspect it and fix…' are broad or
        # compound model tasks, not single-path stat ops.
        if _WORKSPACE_WORD_RE.match(body) or \
                re.search(r"\band\b", body, re.I):
            return None
        raw = _path_tail(body)
        if not raw:
            if _PRONOUN_ONLY_RE.match(body):
                return None
            return ActionPlan(
                kind="inspect", tool="fs_stat",
                permission="filesystem.read",
                clarify="What should I inspect?", action_text="inspect")
        path = _resolve(raw, ws, extra_roots)
        return _mk("inspect", "fs_stat", "filesystem.read",
                   f"inspect {raw}", {"path": path}, {})

    # Archive / extract — 'zip reports to out.zip', 'extract x.zip'.
    m = _ARCHIVE_RE.match(t)
    if m:
        body = (m.group(1) or "").strip()
        dm = _TO_TAIL_RE.match(body)
        if dm:
            src_raw, dst_raw = _path_tail(dm.group(1)), _path_tail(dm.group(2))
        else:
            src_raw, dst_raw = _path_tail(body), ""
        if not src_raw:
            if _PRONOUN_ONLY_RE.match(body) or \
                    _APP_CLAUSE_HEAD_RE.match(body):
                return None
            return ActionPlan(
                kind="archive", tool="fs_archive",
                permission="filesystem.write",
                clarify="What should I archive?", action_text="archive")
        src = _resolve(src_raw, ws, extra_roots)
        if dst_raw:
            dst = _resolve(dst_raw, ws, extra_roots)
        else:
            default_dst = src[0].parent / (src[0].name + ".zip")
            dst = _resolve(str(default_dst), ws, extra_roots)
        return _mk("archive", "fs_archive", "filesystem.write",
                   f"archive {src_raw}", {"src": src, "dst": dst}, {})
    m = _EXTRACT_RE.match(t)
    if m:
        body = (m.group(1) or "").strip()
        dm = _TO_TAIL_RE.match(body)
        if dm:
            src_raw, dst_raw = _path_tail(dm.group(1)), _path_tail(dm.group(2))
        else:
            src_raw, dst_raw = _path_tail(body), ""
        if not src_raw:
            if _PRONOUN_ONLY_RE.match(body) or \
                    _APP_CLAUSE_HEAD_RE.match(body):
                return None
            return ActionPlan(
                kind="extract", tool="fs_extract",
                permission="filesystem.write",
                clarify="What archive should I extract?",
                action_text="extract")
        src = _resolve(src_raw, ws, extra_roots)
        if dst_raw:
            dst = _resolve(dst_raw, ws, extra_roots)
        else:
            dst = _resolve(str(src[0].parent / src[0].stem),
                           ws, extra_roots)
        return _mk("extract", "fs_extract", "filesystem.write",
                   f"extract {src_raw}", {"src": src, "dst": dst}, {})

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
                 mission_id: str = "", artifacts=None) -> dict[str, Any]:
    """Run a plan. Returns {status, text, entry, tool_result}.

    statuses: verified | awaiting_approval | denied | failed |
              clarify | unavailable
    ``text`` is always truthful — it describes what actually happened,
    never what was intended.
    ``artifacts`` (an ArtifactManager) resolves artifact ids found in
    tool results into chat download-card payloads on the outcome.
    """
    entry = ledger.begin(
        kind=plan.kind, action=plan.action_text,
        capability=_CAPABILITY.get(plan.kind, "filesystem"),
        tool=plan.tool, params=plan.params,
        task_id=task_id, mission_id=mission_id) if ledger else None

    holder = {"result": "", "cards": []}

    def _close(status: str, text: str, *, permission: str = "",
               verification: str = "", verified=None, artifact: str = "",
               failure: str = "", tool_result: str = "") -> dict:
        if ledger and entry:
            ledger.finish(
                entry["id"], status=status, permission=permission,
                verification=verification, verified=verified,
                artifact=artifact, failure=failure)
        out = {"status": status, "text": text,
               "entry": entry, "tool_result": tool_result}
        cards = holder["cards"] or _artifact_cards(
            tool_result or holder["result"], artifacts)
        if cards:
            out["artifacts"] = cards
        return out

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
    holder["result"] = result
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
    if plan.kind in _APP_KINDS:
        # App tools return a JSON evidence dict — the post-condition
        # probe (pid alive / window / exit state) rides inside it.
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            data = {}
        ok = bool(data.get("ok"))
        verified = bool(data.get("verified", ok)) or \
            bool(data.get("shell"))  # shell-opened: OS accepted, no pid
        error = str(data.get("error") or "")
        summary = _app_verify_summary(data) or \
            (f"job {data['job_id']}" if data.get("job_id") else "")
        # Installer finished but nothing new registered — honest
        # "ran but unconfirmed", not success and not failure.
        if plan.kind == "install" and ok and data.get("finished") \
                and not verified:
            return _close(
                "unverified", _app_success_text(plan, data),
                permission="approved" if approved else "policy",
                verification=summary or data.get("note", "")[:160],
                verified=False,
                artifact=str(data.get("path") or plan.display),
                tool_result=result)
        # Async kinds verify the ACTION, not the payload: a started
        # download job / a no-op close is a real, evidenced outcome.
        if ok and (verified or plan.kind in
                   ("close", "status", "download", "download_cancel")):
            return _close(
                "verified", _app_success_text(plan, data),
                permission="approved" if approved else "policy",
                verification=summary or result[:160], verified=True,
                artifact=str(data.get("path") or plan.display),
                tool_result=result)
        return _close(
            "failed",
            f"I tried to {plan.action_text}, but it failed: "
            f"{error or result[:160]}",
            permission="approved" if approved else "policy",
            verification=summary, verified=False,
            failure=error or result[:160], tool_result=result)
    if plan.kind in _GH_KINDS or plan.kind == "artifact_show":
        # Artifact/GitHub tools return a JSON evidence payload; the
        # card data rides inside it.
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            data = {}
        if plan.kind == "artifact_show":
            arts = [a for a in (data.get("artifacts") or [])
                    if isinstance(a, dict)]
            if data.get("ok") and arts:
                names = ", ".join(str(a.get("filename", "?"))
                                  for a in arts[:3])
                if len(arts) > 1:
                    text = (f"{len(arts)} artifacts match — "
                            f"{names}{'…' if len(arts) > 3 else ''}. "
                            "Cards are attached; pick one.")
                else:
                    a = arts[0]
                    if a.get("available"):
                        text = f"Here's {a.get('filename')}."
                    else:
                        text = (f"{a.get('filename')} — the local copy "
                                "is unavailable"
                                + (", but the GitHub link still works."
                                   if (a.get("remote") or {})
                                   .get("web_url") else "."))
                holder["cards"] = arts
                return _close(
                    "verified", text,
                    permission="approved" if approved else "policy",
                    verification="artifact resolved", verified=True,
                    tool_result=result)
            return _close(
                "failed",
                str(data.get("error") or "I don't have a matching "
                    "artifact — nothing has been produced yet."),
                verification=result[:160], tool_result=result)
        if plan.kind == "github_upload":
            if data.get("ok") and data.get("status") == "uploading":
                rel = data.get("release") or {}
                return _close(
                    "running",
                    f"Uploading {data.get('asset_name') or plan.display} "
                    f"({int(data.get('size') or 0):,} bytes) to "
                    f"{data.get('repository')} release "
                    f"{rel.get('tag', '')} — job {data.get('job_id', '')}. "
                    "Watch Tasks for progress; the artifact card gains "
                    "GitHub links once the upload verifies. Say 'cancel "
                    "the upload' to abort.",
                    permission="approved" if approved else "policy",
                    verification="upload job started — read-back "
                                 "verification runs on completion",
                    artifact=str(data.get("asset_name") or ""),
                    tool_result=result)
            if data.get("ok"):
                asset = data.get("asset") or {}
                rel = data.get("release") or {}
                url = str(rel.get("url") or asset.get("url") or "")
                text = (f"Published {asset.get('name') or plan.display} "
                        f"to {data.get('repository')} release "
                        f"{rel.get('tag', '')} — verified "
                        f"({asset.get('size', '?')} bytes). {url}").strip()
                return _close(
                    "verified", text,
                    permission="approved" if approved else "policy",
                    verification=f"asset {asset.get('id')} read-back "
                                 f"size={asset.get('size')}",
                    verified=True,
                    artifact=str(asset.get("name") or ""),
                    tool_result=result)
            return _close(
                "failed",
                "The GitHub upload didn't complete: "
                f"{data.get('error') or result[:160]}",
                verification=result[:160], verified=False,
                failure=str(data.get("error") or result[:160]),
                tool_result=result)
        if plan.kind == "github_upload_cancel":
            if data.get("ok"):
                return _close(
                    "cancelled",
                    "Upload cancelled — nothing was published to GitHub.",
                    verified=True, tool_result=result)
            return _close(
                "failed",
                str(data.get("error") or "No upload is running."),
                verified=False, tool_result=result)
        if plan.kind in ("github_download", "github_file"):
            if data.get("ok"):
                dest = data.get("path") or "your Downloads folder"
                if data.get("deduplicated"):
                    return _close(
                        "verified",
                        f"Already downloaded — the existing file "
                        f"verified: {dest}.",
                        permission="approved" if approved else "policy",
                        verification="dedupe match", verified=True,
                        artifact=str(dest), tool_result=result)
                return _close(
                    "verified",
                    f"Downloading {plan.display} to {dest} — job "
                    f"{data.get('job_id', '')}. It registers as an "
                    "artifact when it lands; watch Tasks for progress.",
                    permission="approved" if approved else "policy",
                    verification="download job started", verified=True,
                    artifact=str(dest), tool_result=result)
            return _close(
                "failed",
                "The GitHub download couldn't start: "
                f"{data.get('error') or result[:160]}",
                verification=result[:160], verified=False,
                failure=str(data.get("error") or result[:160]),
                tool_result=result)
    if plan.kind in _READ_KINDS:
        # Read intents are their own evidence — the tool result IS the
        # verified answer (stat output, match list).
        return _close(
            "verified", _read_text(plan, result),
            permission="approved" if approved else "policy",
            verification=result[:160], verified=True,
            artifact=plan.display, tool_result=result)
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


_APP_KINDS = {"launch", "close", "restart", "status", "window",
              "download", "download_cancel", "install"}
_READ_KINDS = {"search", "search_text", "inspect"}
_GH_KINDS = {"github_upload", "github_download", "github_file",
             "github_upload_cancel"}
_ART_ID_RE = re.compile(r"art-[0-9a-f]{12}")


def _artifact_cards(result: str, artifacts) -> list[dict]:
    """Resolve artifact ids / embedded views in a tool result into
    client-facing card payloads via the ArtifactManager."""
    if not result:
        return []
    ids: list[str] = []
    views: list[dict] = []
    try:
        data = json.loads(result)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("artifact_id"), str):
            ids.append(data["artifact_id"])
        for a in data.get("artifacts") or []:
            if isinstance(a, dict) and a.get("id"):
                if a.get("filename"):
                    views.append(a)
                else:
                    ids.append(str(a["id"]))
            elif isinstance(a, str):
                ids.append(a)
    else:
        ids.extend(_ART_ID_RE.findall(result))
    if artifacts is not None:
        for aid in ids[:8]:
            try:
                view = artifacts.client_view(aid)
            except Exception:
                view = None
            if view:
                views.append(view)
    # Dedupe, preserve order.
    out, seen = [], set()
    for v in views:
        if str(v.get("id")) not in seen:
            seen.add(str(v.get("id")))
            out.append(v)
    return out[:8]
_CAPABILITY = {k: "application" for k in
               ("launch", "close", "restart", "status", "window")}
_CAPABILITY.update({"download": "network", "download_cancel": "network",
                    "install": "application"})
_CAPABILITY.update({"artifact_show": "filesystem"})
_CAPABILITY.update({k: "github" for k in _GH_KINDS})
_CAPABILITY.update({k: "filesystem" for k in
                    ("mkdir", "write", "delete", "move", "rename",
                     "copy", "archive", "extract")})
_CAPABILITY.update({k: "filesystem" for k in _READ_KINDS})


def _app_success_text(plan: ActionPlan, data: dict) -> str:
    name = plan.display or plan.params.get("app", "the app")
    if plan.kind == "launch":
        if data.get("already_running"):
            return f"{name} is already running — brought it to the front."
        if data.get("shell"):
            return f"Opened {name}."
        return f"{name} is open."
    if plan.kind == "close":
        if data.get("already") or data.get("count") == 0:
            return f"{name} wasn't running."
        n = int(data.get("count") or 1)
        return (f"{name} closed." if n <= 1
                else f"Closed {n} {name} windows.")
    if plan.kind == "restart":
        return f"{name} restarted."
    if plan.kind == "status":
        return (f"{name} is running." if data.get("running")
                else f"{name} isn't running.")
    if plan.kind == "window":
        return f"{name} {plan.params.get('state', '')}d."
    if plan.kind == "download":
        if data.get("deduplicated"):
            return (f"Already downloaded — the existing file "
                    f"verified: {data.get('path', name)}.")
        dest = data.get("path") or "your Downloads folder"
        return (f"Downloading {name} to {dest} — job "
                f"{data.get('job_id', '')}. It resumes automatically "
                "if interrupted; watch Tasks for progress.")
    if plan.kind == "download_cancel":
        return ("Download cancelled — the partial file is kept for "
                "resume." if data.get("cancelled")
                else "No download is running.")
    if plan.kind == "install":
        installed = data.get("installed") or []
        if installed:
            names = ", ".join(str(e.get("name", "?"))
                              for e in installed[:3])
            return (f"Installed — {names} registered. "
                    f"(finished in {data.get('duration_s', '?')}s)")
        if data.get("app_resolved"):
            return (f"The installer finished — {data['app_resolved']} "
                    "is on the system.")
        if data.get("timed_out"):
            return (f"The installer is still running after "
                    f"{int(data.get('duration_s', 0))}s — it may be "
                    "waiting on a prompt or still copying files.")
        return ("The installer ran and exited, but I couldn't confirm "
                "what it installed — no new product registered. If it "
                "finished silently, check the app's Start Menu entry.")
    return "Done."


def _app_verify_summary(data: dict) -> str:
    bits = []
    if data.get("pid"):
        bits.append(f"pid {data['pid']}")
    if data.get("path"):
        bits.append(str(data["path"]))
    win = data.get("window")
    if isinstance(win, dict) and win.get("hwnd"):
        bits.append(f"window '{win.get('title', '')}' (hwnd "
                    f"{win['hwnd']})")
    installed = data.get("installed")
    if installed:
        bits.append("installed: " + ", ".join(
            str(e.get("name", "?")) for e in installed[:3]))
    return "; ".join(bits)[:200]


def _read_text(plan: ActionPlan, result: str) -> str:
    """Truthful answer text for read intents — the payload is the
    evidence, so format it instead of claiming success around it."""
    if plan.kind == "search":
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            data = {}
        files = list(data.get("files") or [])
        count = int(data.get("count") or len(files))
        if not count:
            return f"No files matched '{plan.display}'."
        shown = ", ".join(str(f) for f in files[:10])
        more = f" (+{count - 10} more)" if count > 10 else ""
        noun = "file" if count == 1 else "files"
        return f"Found {count} {noun} matching '{plan.display}': {shown}{more}"
    if plan.kind == "search_text":
        lines = [ln for ln in result.splitlines() if ln.strip()]
        if not lines or lines[0].strip() == "NO_MATCHES":
            return f"No matches for '{plan.display}'."
        head = "; ".join(ln.strip()[:80] for ln in lines[:5])
        more = f" (+{len(lines) - 5} more)" if len(lines) > 5 else ""
        return (f"{len(lines)} match(es) for '{plan.display}': "
                f"{head}{more}")
    if plan.kind == "inspect":
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            data = {}
        if not data.get("exists"):
            return f"{plan.display} doesn't exist."
        bits = [str(data.get("type", "item")),
                f"{data.get('size', 0)} bytes"]
        if data.get("children") is not None:
            bits.append(f"{data['children']} items")
        mt = data.get("mtime")
        if mt:
            import datetime
            bits.append("modified " + datetime.datetime.fromtimestamp(
                float(mt)).strftime("%Y-%m-%d %H:%M"))
        if data.get("symlink"):
            bits.append("symlink")
        if data.get("readonly"):
            bits.append("read-only")
        sha = data.get("sha256")
        if sha and isinstance(sha, str) and len(sha) >= 16:
            bits.append(f"sha256 {sha[:16]}…")
        return f"{plan.display} — {', '.join(bits)}."
    return result[:400]


def _success_text(plan: ActionPlan) -> str:
    d = plan.display
    return {
        "mkdir": f"Created {d}.",
        "write": f"Created {d}.",
        "delete": f"Deleted {d}.",
        "move": f"Moved to {d}.",
        "rename": f"Renamed to {d}.",
        "copy": f"Copied to {d}.",
        "archive": f"Archived to {d}.",
        "extract": f"Extracted to {d}.",
    }.get(plan.kind, f"Done — {d}.")
