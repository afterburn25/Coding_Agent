"""3D scene generation — builds Blender Python scripts from a simple scene
spec and renders through the Blender manifest tool (registry-delegated).

`blender_render` accepts objects like::

    {"objects": [{"type": "cube", "location": [0,0,1], "scale": [1,1,1],
                  "color": [0.8,0.2,0.1]}],
     "output": "renders/scene.png", "resolution": [640, 480]}

and produces + executes a Blender script adding primitives, a sun light and
camera on a track quat, then rendering to PNG/Eevee.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

PRIMITIVES = {
    "cube": "bpy.ops.mesh.primitive_cube_add(size=2, location={loc})",
    "sphere": "bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location={loc})",
    "plane": "bpy.ops.mesh.primitive_plane_add(size=10, location={loc})",
    "cylinder": "bpy.ops.mesh.primitive_cylinder_add(radius=1, depth=2, location={loc})",
    "cone": "bpy.ops.mesh.primitive_cone_add(radius1=1, depth=2, location={loc})",
    "torus": "bpy.ops.mesh.primitive_torus_add(location={loc})",
}


def _vec(value: Any, default: list[float], n: int = 3) -> list[float]:
    if isinstance(value, (list, tuple)) and len(value) >= n:
        return [float(v) for v in value[:n]]
    return list(default)


def _color(value: Any) -> list[float]:
    c = _vec(value, [0.7, 0.7, 0.7, 1.0], 4 if isinstance(value, (list, tuple)) and len(value) >= 4 else 3)
    while len(c) < 4:
        c.append(1.0)
    return [max(0.0, min(1.0, float(v))) for v in c[:4]]


def build_scene_script(spec: dict[str, Any], output_abs: Path) -> str:
    """Render a scene spec into a self-contained Blender Python script."""
    objects = spec.get("objects") if isinstance(spec.get("objects"), list) else []
    resolution = _vec(spec.get("resolution"), [800, 600], 2)
    camera_at = _vec(spec.get("camera"), [7, -7, 5])
    lines: list[str] = [
        "import bpy, math, mathutils",
        "bpy.ops.wm.read_factory_settings(use_empty=True)",
        "scene = bpy.context.scene",
        f"scene.render.resolution_x = {int(resolution[0])}",
        f"scene.render.resolution_y = {int(resolution[1])}",
        "try:",
        "    scene.render.engine = 'BLENDER_EEVEE_NEXT'",
        "except Exception:",
        "    scene.render.engine = 'BLENDER_EEVEE'",
        "mats = {}",
        "def material(name, color):",
        "    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)",
        "    m.diffuse_color = color",
        "    m.use_nodes = False",
        "    return m",
    ]
    for i, obj in enumerate(objects[:200]):
        kind = str(obj.get("type", "cube")).lower()
        builder = PRIMITIVES.get(kind)
        if builder is None:
            continue
        loc = _vec(obj.get("location"), [0, 0, 1])
        scale = _vec(obj.get("scale"), [1, 1, 1])
        color = _color(obj.get("color"))
        lines += [
            builder.format(loc=loc),
            f"obj = bpy.context.active_object",
            f"obj.scale = {scale}",
            f"obj.data.materials.append(material('m{i}', {color}))",
        ]
    lines += [
        "bpy.ops.object.light_add(type='SUN', location=(4, 4, 8))",
        "bpy.context.active_object.data.energy = 4.0",
        f"bpy.ops.object.camera_add(location={camera_at})",
        "cam = bpy.context.active_object",
        "scene.camera = cam",
        "direction = mathutils.Vector((0, 0, 1)) - cam.location",
        "cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()",
        f"scene.render.filepath = {str(output_abs)!r}",
        "scene.render.image_settings.file_format = 'PNG'",
        "bpy.ops.render.render(write_still=True)",
        "print('RENDERED', scene.render.filepath)",
    ]
    return "\n".join(lines) + "\n"


def register_blender_tools(registry: ToolRegistry, workspace: Path, *, jobs=None) -> None:

    def _resolve_out(raw: str) -> Path | None:
        p = (workspace / str(raw or "")).resolve()
        if not p.is_relative_to(workspace.resolve()):
            return None
        return p

    def blender_render(args: dict[str, Any]) -> str:
        out = _resolve_out(str(args.get("output", "") or f".agent/blender/render-{int(time.time())}.png"))
        if out is None:
            return "ERROR: output must stay inside the workspace"
        spec: dict[str, Any] = {"objects": args.get("objects") or [],
                                "resolution": args.get("resolution"),
                                "camera": args.get("camera")}
        workdir = workspace / ".agent" / "blender"
        workdir.mkdir(parents=True, exist_ok=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        script = workdir / f"scene-{int(time.time())}.py"
        script.write_text(build_scene_script(spec, out), encoding="utf-8")

        job = jobs.submit("media", f"Blender render → {out.name}") if jobs is not None else None
        if job:
            jobs.update(job.id, state="running", detail="running blender --background")
        result = registry.execute("blender", {
            "script": str(script),
            "args": [],
        }, approved=bool(args.get("approved", False)))
        failed = result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL"))
        payload = {"ok": not failed, "script": str(script), "output": str(out), "detail": result[:1000]}
        if out.is_file():
            payload["rendered_bytes"] = out.stat().st_size
        if job:
            jobs.update(job.id, state="failed" if failed else "completed",
                        detail=f"render {'failed' if failed else 'finished'}: {out.name}",
                        error=result[:300] if failed else "")
        return json.dumps(payload, ensure_ascii=False)

    registry.register(ToolSpec(
        "blender_render",
        "Create and render a 3D scene with Blender — builds a Python script from a simple object spec (cube/sphere/plane/cylinder/cone/torus with location/scale/color), adds sun light + camera, renders PNG via the blender manifest tool.",
        {
            "type": "object",
            "properties": {
                "objects": {"type": "array", "items": {"type": "object"},
                            "description": "[{type, location:[x,y,z], scale:[x,y,z], color:[r,g,b(,a)]}]"},
                "output": {"type": "string", "description": "output PNG path (default .agent/blender/render-<ts>.png)"},
                "resolution": {"type": "array", "items": {"type": "integer"}, "description": "[width, height]"},
                "camera": {"type": "array", "items": {"type": "number"}, "description": "camera [x,y,z]"},
            },
        },
        "shell.execute", blender_render,
        category="3d",
        capabilities=["render_3d_scene", "blender_render", "create_3d"],
    ))
