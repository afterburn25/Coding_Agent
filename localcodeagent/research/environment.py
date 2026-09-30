from __future__ import annotations

import json
import platform
import importlib.metadata
import re
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python >=3.11 is required by this project.
    tomllib = None


class EnvironmentInspector:
    """Inspect project manifests without executing project code or package-manager hooks."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def inspect(self) -> dict[str, Any]:
        packages: list[dict[str, str]] = []
        manifests: list[str] = []
        self._inspect_python(packages, manifests)
        self._inspect_node(packages, manifests)
        self._inspect_dotnet(packages, manifests)
        self._inspect_cpp(packages, manifests)
        self._inspect_rust_go(manifests)
        runtime_packages = self._inspect_python_runtime()
        return {
            "system": {
                "os": platform.platform(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "python_executable": sys.executable,
            },
            "packages": packages[:1000],
            "runtime_packages": runtime_packages[:2000],
            "manifests": sorted(set(manifests)),
        }


    @staticmethod
    def _inspect_python_runtime() -> list[dict[str, str]]:
        """Read installed-package metadata for this Python runtime without importing packages.

        This is safe metadata inspection only: no package module or project code is
        imported/executed. A project-specific virtualenv can still differ, so the
        source is explicitly labeled as the agent runtime.
        """
        rows: list[dict[str, str]] = []
        try:
            for dist in importlib.metadata.distributions():
                name = str(dist.metadata.get("Name") or "").strip()
                if not name:
                    continue
                rows.append({
                    "ecosystem": "python-runtime",
                    "name": name,
                    "version": str(dist.version or ""),
                    "source": "agent-python-runtime",
                })
        except Exception:
            return []
        rows.sort(key=lambda row: row["name"].lower())
        return rows

    def _inspect_python(self, out: list[dict[str, str]], manifests: list[str]) -> None:
        pyproject = self.workspace / "pyproject.toml"
        if pyproject.is_file() and tomllib:
            manifests.append(self._rel(pyproject))
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                deps = data.get("project", {}).get("dependencies", []) or []
                for dep in deps:
                    name, version = self._split_requirement(str(dep))
                    out.append({"ecosystem": "python", "name": name, "version": version, "source": self._rel(pyproject)})
                optional = data.get("project", {}).get("optional-dependencies", {}) or {}
                for group in optional.values():
                    for dep in group or []:
                        name, version = self._split_requirement(str(dep))
                        out.append({"ecosystem": "python", "name": name, "version": version, "source": self._rel(pyproject)})
            except Exception:
                pass
        for name in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt"):
            path = self.workspace / name
            if not path.is_file():
                continue
            manifests.append(name)
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                pkg, version = self._split_requirement(line)
                out.append({"ecosystem": "python", "name": pkg, "version": version, "source": name})

    @staticmethod
    def _split_requirement(raw: str) -> tuple[str, str]:
        match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*(.*)", raw)
        return (match.group(1), match.group(2).strip()) if match else (raw.strip(), "")

    def _inspect_node(self, out: list[dict[str, str]], manifests: list[str]) -> None:
        path = self.workspace / "package.json"
        if not path.is_file():
            return
        manifests.append("package.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        for group in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for name, version in (data.get(group) or {}).items():
                out.append({"ecosystem": "node", "name": str(name), "version": str(version), "source": f"package.json:{group}"})

    def _inspect_dotnet(self, out: list[dict[str, str]], manifests: list[str]) -> None:
        pattern = re.compile(r'<PackageReference[^>]+Include=["\']([^"\']+)["\'][^>]*?(?:Version=["\']([^"\']+)["\'])?', re.I)
        for path in list(self.workspace.glob("*.csproj")) + list(self.workspace.glob("**/Directory.Packages.props"))[:10]:
            if not path.is_file():
                continue
            manifests.append(self._rel(path))
            text = path.read_text(encoding="utf-8", errors="ignore")
            for name, version in pattern.findall(text):
                out.append({"ecosystem": "dotnet", "name": name, "version": version, "source": self._rel(path)})

    def _inspect_cpp(self, out: list[dict[str, str]], manifests: list[str]) -> None:
        for name in ("CMakeLists.txt", "vcpkg.json", "conanfile.txt", "conanfile.py"):
            path = self.workspace / name
            if path.is_file():
                manifests.append(name)
        vcpkg = self.workspace / "vcpkg.json"
        if vcpkg.is_file():
            try:
                data = json.loads(vcpkg.read_text(encoding="utf-8"))
                for dep in data.get("dependencies", []) or []:
                    if isinstance(dep, str):
                        out.append({"ecosystem": "vcpkg", "name": dep, "version": "", "source": "vcpkg.json"})
                    elif isinstance(dep, dict) and dep.get("name"):
                        out.append({"ecosystem": "vcpkg", "name": str(dep["name"]), "version": str(dep.get("version>=") or dep.get("version") or ""), "source": "vcpkg.json"})
            except Exception:
                pass

    def _inspect_rust_go(self, manifests: list[str]) -> None:
        for name in ("Cargo.toml", "Cargo.lock", "go.mod", "go.sum"):
            if (self.workspace / name).is_file():
                manifests.append(name)

    def find_package(self, name: str) -> list[dict[str, str]]:
        needle = name.strip().lower()
        env = self.inspect()
        rows = list(env["packages"]) + list(env.get("runtime_packages", []))
        return [p for p in rows if needle in p["name"].lower() or p["name"].lower() in needle]
