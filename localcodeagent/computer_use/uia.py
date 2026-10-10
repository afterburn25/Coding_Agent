"""UI Automation bridge (Windows) — observe UI elements by name/type
instead of guessing pixels.

The bridge runs bounded PowerShell against the managed
UIAutomationClient assemblies — the same deterministic-escape-hatch
pattern as ``_resolve_lnk`` in apps.py, so no new dependency ships in
the frozen build. Every query returns structured element evidence:
name, control type, AutomationId, bounding rect, and which action
patterns the element supports.

This is the observation layer for the GUI loop:
    observe -> pick element by name -> act -> observe again -> verify.
Raw-coordinate clicking stays the documented fallback when UIA finds
no element.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

MAX_ELEMENTS = 400


def _run_ps(script: str, timeout_s: float = 20.0) -> dict[str, Any]:
    """Run a bounded PowerShell UIA query; returns {ok, data|error}."""
    if os.name != "nt":
        return {"ok": False, "error": "unsupported platform"}
    try:
        from ..procutil import no_window_flags
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=timeout_s,
            creationflags=no_window_flags(), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "UIA query timed out"}
    except Exception as exc:  # noqa: BLE001 — report, never raise
        return {"ok": False, "error": f"UIA query failed: {exc}"}
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return {"ok": False,
                "error": f"UIA query failed: {err[:200] or r.returncode}"}
    out = (r.stdout or "").strip()
    if not out:
        return {"ok": True, "data": None}
    # Payload is base64'd UTF-8 JSON — immune to console-codepage
    # mangling, control chars, and embedded quotes in element names.
    try:
        import base64
        raw = base64.b64decode(out.strip()).decode("utf-8",
                                                 errors="replace")
        return {"ok": True, "data": json.loads(raw)}
    except Exception:
        return {"ok": False,
                "error": f"unparseable UIA output: {out[:160]}"}


def _ps_escape(value: str) -> str:
    return str(value or "").replace("'", "''")[:200]


_PS_PREAMBLE = (
    "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes | "
    "Out-Null; "
)


def _hwnd_source(hwnd: int, title_substr: str) -> str:
    """PS snippet resolving the target AutomationElement root."""
    if hwnd:
        return (f"$root=[System.Windows.Automation.AutomationElement]"
                f"::FromHandle([IntPtr]{int(hwnd)});")
    needle = _ps_escape(title_substr).lower()
    return (
        "$shell=New-Object -ComObject Shell.Application; "
        "$hwnd=0; "
        f"Get-Process | Where-Object {{$_.MainWindowTitle -like '*{needle}*'"
        " -and $_.MainWindowHandle -ne 0} | "
        "Select-Object -First 1 | ForEach-Object {$hwnd=$_.MainWindowHandle}; "
        "if ($hwnd -eq 0) { [Console]::Out.WriteLine([Convert]::"
        "ToBase64String([Text.Encoding]::UTF8.GetBytes("
        "'{\"ok\":false,\"error\":\"no matching window\"}'))); exit }; "
        "$root=[System.Windows.Automation.AutomationElement]::FromHandle("
        "$hwnd);")


def ui_elements(*, hwnd: int = 0, title_substr: str = "",
                depth: int = 6, max_items: int = MAX_ELEMENTS
                ) -> dict[str, Any]:
    """Enumerate interactive descendants of a window.

    Returns {ok, window:{title,hwnd}, elements:[{name,type,id,rect,
    enabled,offscreen,patterns}]}. Elements with neither a name nor an
    AutomationId are dropped — they cannot be targeted anyway.
    """
    script = (
        _PS_PREAMBLE + _hwnd_source(hwnd, title_substr) + f"""
$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition);
$items=@(); $i=0;
foreach ($e in $all) {{
  if ($i -ge {int(max_items)}) {{ break }}
  $c=$e.Current; if ($null -eq $c) {{ continue }}
  $name=[string]$c.Name; $id=[string]$c.AutomationId;
  if (-not $name -and -not $id) {{ continue }}
  $r=$c.BoundingRectangle;
  $items+=[pscustomobject]@{{name=$name; type=$c.ControlType.ProgrammaticName;
    id=$id; x=[math]::Round([double]$r.X);
    y=[math]::Round([double]$r.Y);
    w=[math]::Round([double]$r.Width);
    h=[math]::Round([double]$r.Height);
    enabled=[bool]$c.IsEnabled; offscreen=[bool]$c.IsOffscreen}};
  $i++;
}}
$wt=$root.Current.Name;
$json=(@{{ok=$true; window=@{{title=[string]$wt;
  hwnd=[int64]$root.Current.NativeWindowHandle}};
  elements=$items; count=$i}} | ConvertTo-Json -Depth 4 -Compress);
[Console]::Out.WriteLine([Convert]::ToBase64String(
  [Text.Encoding]::UTF8.GetBytes($json)));
""")
    res = _run_ps(script, timeout_s=25.0)
    if not res.get("ok"):
        return res
    data = res.get("data") or {}
    if data.get("ok") is False:
        return {"ok": False, "error": data.get("error", "no window")}
    return {"ok": True,
            "window": data.get("window") or {},
            "elements": data.get("elements") or [],
            "count": int(data.get("count") or 0),
            "truncated": int(data.get("count") or 0) >= max_items}


def ui_find(name: str, *, hwnd: int = 0, title_substr: str = "",
            control_type: str = "") -> dict[str, Any]:
    """Find one element by name or AutomationId (case-insensitive,
    substring-tolerant). Returns {ok, element, matches}."""
    tree = ui_elements(hwnd=hwnd, title_substr=title_substr)
    if not tree.get("ok"):
        return tree
    needle = str(name or "").strip().lower()
    ctype = str(control_type or "").strip().lower()
    if not needle:
        return {"ok": False, "error": "element name required"}
    matches = []
    for el in tree.get("elements") or []:
        en = str(el.get("name") or "").lower()
        ei = str(el.get("id") or "").lower()
        if needle in en or (ei and needle in ei):
            if ctype and ctype not in str(el.get("type") or "").lower():
                continue
            matches.append(el)
    # exact name match wins over substring noise
    exact = [m for m in matches
             if str(m.get("name") or "").lower() == needle]
    chosen = (exact or matches)
    if not chosen:
        return {"ok": False, "matches": [], "count": tree.get("count", 0),
                "error": f"no element matching '{name}'"}
    return {"ok": True, "element": chosen[0],
            "matches": len(matches), "window": tree.get("window")}


def ui_act(name: str, *, hwnd: int = 0, title_substr: str = "",
           action: str = "invoke", value: str = "",
           control_type: str = "") -> dict[str, Any]:
    """Act on a named element via UIA patterns — never blind pixels.

    action: invoke | toggle | select | setvalue(value) | expand |
    collapse | focus. Returns {ok, action, element, pattern,
    rect_hint?}. When no action pattern exists the element's bounding
    rect is returned as ``rect_hint`` so the caller may click its
    center with the mouse as the documented fallback.
    """
    needle = _ps_escape(name)
    ctype = _ps_escape(control_type)
    act = str(action or "invoke").strip().lower()
    val = _ps_escape(value)
    script = (
        _PS_PREAMBLE + _hwnd_source(hwnd, title_substr) + f"""
$all=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition);
$target=$null;
foreach ($e in $all) {{
  $c=$e.Current; if ($null -eq $c) {{ continue }}
  $en=([string]$c.Name).ToLower(); $ei=([string]$c.AutomationId).ToLower();
  if ('{needle}'.ToLower() -and ($en.Contains('{needle}'.ToLower()) -or
      ($ei -and $ei.Contains('{needle}'.ToLower())))) {{
    if ('{ctype}' -and -not $c.ControlType.ProgrammaticName.ToLower().
        Contains('{ctype}')) {{ continue }}
    if ($en -eq '{needle}'.ToLower()) {{ $target=$e; break }}
    if ($null -eq $target) {{ $target=$e }}
  }}
}}
if ($null -eq $target) {{
  [Console]::Out.WriteLine([Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes(
      '{{"ok":false,"error":"no element matching"}}'))); exit }}
$r=$target.Current.BoundingRectangle;
$out=@{{ok=$false; action='{act}';
  element=@{{name=[string]$target.Current.Name;
    type=$target.Current.ControlType.ProgrammaticName;
    id=[string]$target.Current.AutomationId}};
  rect=@{{x=[math]::Round([double]$r.X);y=[math]::Round([double]$r.Y);
    w=[math]::Round([double]$r.Width);h=[math]::Round([double]$r.Height)}}}};
$pat=$null; $done=$false; $used='';
switch ('{act}') {{
  'invoke' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,[ref]$pat)) {{
      $pat.Invoke(); $done=$true; $used='InvokePattern' }}}}
  'toggle' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.TogglePattern]::Pattern,[ref]$pat)) {{
      $pat.Toggle(); $done=$true; $used='TogglePattern' }}}}
  'select' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [ref]$pat)) {{ $pat.Select(); $done=$true;
      $used='SelectionItemPattern' }}}}
  'setvalue' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern,[ref]$pat)) {{
      $pat.SetValue('{val}'); $done=$true; $used='ValuePattern';
      try {{ $out.readback=[string]$pat.Current.Value }}
      catch {{ $out.readback='' }} }}}}
  'expand' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
        [ref]$pat)) {{ $pat.Expand(); $done=$true;
      $used='ExpandCollapsePattern' }}}}
  'collapse' {{
    if ($target.TryGetCurrentPattern(
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
        [ref]$pat)) {{ $pat.Collapse(); $done=$true;
      $used='ExpandCollapsePattern' }}}}
  'focus' {{
    try {{ $target.SetFocus(); $done=$true; $used='SetFocus' }}
    catch {{ }} }}
}}
$out.ok=$done; $out.pattern=$used;
if (-not $done) {{ $out.error='no supported pattern - use rect_hint' }}
[Console]::Out.WriteLine([Convert]::ToBase64String(
  [Text.Encoding]::UTF8.GetBytes(
    ($out | ConvertTo-Json -Depth 4 -Compress))));
""")
    res = _run_ps(script, timeout_s=25.0)
    if not res.get("ok"):
        return res
    data = res.get("data") or {}
    if not data.get("ok"):
        d = dict(data)
        d.setdefault("rect_hint", d.get("rect"))
        return d
    return data


def screen_hash(*, hwnd: int = 0, title_substr: str = "") -> str:
    """Compact fingerprint of a window's element set — used by the
    loop detector to tell 'state changed' from 'clicked the same thing
    again with no effect'."""
    tree = ui_elements(hwnd=hwnd, title_substr=title_substr)
    if not tree.get("ok"):
        return ""
    import hashlib
    h = hashlib.sha256()
    h.update(str(tree.get("window", {}).get("title", "")).encode())
    for el in tree.get("elements") or []:
        h.update(f"{el.get('type')}|{el.get('name')}|"
                 f"{el.get('enabled')}|{el.get('x')},{el.get('y')},"
                 f"{el.get('w')},{el.get('h')}".encode())
    return h.hexdigest()[:24]


class LoopGuard:
    """Detects a stuck observe->act loop: the same action on the same
    target while the screen fingerprint refuses to change."""

    def __init__(self, max_repeats: int = 3):
        self.max_repeats = max_repeats
        self._history: list[tuple[str, str, str]] = []

    def check(self, action: str, target: str, state: str) -> str:
        """Returns '' when the step may proceed, else an error string."""
        self._history.append((action, target, state))
        if len(self._history) > 40:
            self._history = self._history[-40:]
        tail = [h for h in self._history[-(self.max_repeats + 1):]
                if h[0] == action and h[1] == target]
        if len(tail) > self.max_repeats:
            unchanged = len({h[2] for h in tail}) == 1
            if unchanged:
                return (f"loop detected — '{action}' on '{target}' "
                        f"repeated {len(tail)}x with no UI change")
        return ""


def element_center(rect: dict[str, Any]) -> tuple[int, int] | None:
    try:
        return (int(rect["x"]) + int(rect["w"]) // 2,
                int(rect["y"]) + int(rect["h"]) // 2)
    except (KeyError, TypeError, ValueError):
        return None


def wait_for_element(name: str, *, hwnd: int = 0, title_substr: str = "",
                     timeout_s: float = 8.0) -> dict[str, Any]:
    """Re-observe until an element appears — the post-action check."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {"ok": False, "error": "no observation"}
    while time.monotonic() < deadline:
        last = ui_find(name, hwnd=hwnd, title_substr=title_substr)
        if last.get("ok"):
            return last
        time.sleep(0.7)
    return last
