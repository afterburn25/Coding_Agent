from __future__ import annotations

import re
from urllib.parse import urlparse


OFFICIAL_DOCS: dict[str, tuple[str, ...]] = {
    "python": ("docs.python.org", "python.org"),
    "pytorch": ("pytorch.org", "docs.pytorch.org"),
    "torch": ("pytorch.org", "docs.pytorch.org"),
    "tensorflow": ("tensorflow.org",),
    "numpy": ("numpy.org",),
    "pandas": ("pandas.pydata.org",),
    "scipy": ("scipy.org",),
    "django": ("docs.djangoproject.com", "djangoproject.com"),
    "flask": ("flask.palletsprojects.com",),
    "fastapi": ("fastapi.tiangolo.com",),
    "cuda": ("docs.nvidia.com", "developer.nvidia.com"),
    "nvidia": ("docs.nvidia.com", "developer.nvidia.com"),
    "react": ("react.dev",),
    "vue": ("vuejs.org",),
    "angular": ("angular.dev", "angular.io"),
    "svelte": ("svelte.dev",),
    "node": ("nodejs.org",),
    "node.js": ("nodejs.org",),
    "deno": ("deno.land", "docs.deno.com"),
    "typescript": ("typescriptlang.org",),
    "rust": ("doc.rust-lang.org", "rust-lang.org"),
    "golang": ("go.dev", "golang.org"),
    "cmake": ("cmake.org",),
    "qt": ("doc.qt.io", "qt.io"),
    ".net": ("learn.microsoft.com", "dotnet.microsoft.com"),
    "dotnet": ("learn.microsoft.com", "dotnet.microsoft.com"),
    "asp.net": ("learn.microsoft.com",),
    "c#": ("learn.microsoft.com",),
    "windows": ("learn.microsoft.com", "support.microsoft.com"),
    "github": ("docs.github.com", "github.com"),
    "gitlab": ("docs.gitlab.com",),
    "docker": ("docs.docker.com",),
    "kubernetes": ("kubernetes.io",),
    "k8s": ("kubernetes.io",),
    "llama.cpp": ("github.com",),
    "llama": ("github.com", "llama.meta.com"),
    "comfyui": ("docs.comfy.org", "github.com"),
    "qwen": ("qwenlm.github.io", "huggingface.co", "github.com"),
    "flux": ("bfl.ai", "huggingface.co", "github.com"),
    "ollama": ("ollama.com", "github.com"),
    "redis": ("redis.io",),
    "postgres": ("postgresql.org",),
    "postgresql": ("postgresql.org",),
    "mysql": ("dev.mysql.com", "mysql.com"),
    "sqlite": ("sqlite.org",),
    "nginx": ("nginx.org",),
    "firefox": ("developer.mozilla.org", "mozilla.org"),
    "chrome": ("developer.chrome.com", "chromium.org"),
    "mdn": ("developer.mozilla.org",),
    "javascript": ("developer.mozilla.org", "tc39.es"),
    "tkinter": ("docs.python.org",),
    "asyncio": ("docs.python.org",),
    "azure": ("learn.microsoft.com", "azure.microsoft.com"),
    "aws": ("docs.aws.amazon.com",),
    "gcp": ("cloud.google.com",),
    "blender": ("docs.blender.org",),
    "unity": ("docs.unity3d.com", "unity.com"),
    "unreal": ("dev.epicgames.com",),
    "vscode": ("code.visualstudio.com",),
    "homebrew": ("docs.brew.sh",),
    "debian": ("debian.org", "wiki.debian.org"),
    "ubuntu": ("ubuntu.com", "help.ubuntu.com"),
    "arch linux": ("wiki.archlinux.org",),
}

# Expected official GitHub org/repo per technology (Part 24). A github.com
# result matching these is an official_repo; anything else is community.
OFFICIAL_GITHUB: dict[str, tuple[str, ...]] = {
    "python": ("python/cpython", "python"),
    "pytorch": ("pytorch/pytorch", "pytorch"),
    "tensorflow": ("tensorflow/tensorflow", "tensorflow"),
    "numpy": ("numpy/numpy", "numpy"),
    "pandas": ("pandas-dev/pandas",),
    "django": ("django/django", "django"),
    "flask": ("pallets/flask", "pallets"),
    "fastapi": ("fastapi/fastapi", "tiangolo"),
    "react": ("facebook/react", "facebook/react-native", "reactjs"),
    "vue": ("vuejs/core", "vuejs"),
    "angular": ("angular/angular", "angular"),
    "svelte": ("sveltejs/svelte", "sveltejs"),
    "node": ("nodejs/node", "nodejs"),
    "typescript": ("microsoft/TypeScript", "microsoft"),
    "rust": ("rust-lang/rust", "rust-lang"),
    "golang": ("golang/go", "golang"),
    "cmake": ("Kitware/CMake", "kitware"),
    "qt": ("qt/qt5", "qt"),
    "llama.cpp": ("ggml-org/llama.cpp", "ggerganov/llama.cpp", "ggml-org"),
    "llama": ("meta-llama/llama", "meta-llama", "ggml-org"),
    "comfyui": ("comfyanonymous/ComfyUI", "comfy-org"),
    "qwen": ("QwenLM/Qwen", "qwenlm"),
    "flux": ("black-forest-labs/flux", "black-forest-labs"),
    "ollama": ("ollama/ollama", "ollama"),
    "docker": ("docker", "moby/moby"),
    "kubernetes": ("kubernetes/kubernetes", "kubernetes"),
    "redis": ("redis/redis", "redis"),
    "postgres": ("postgres/postgres",),
    "sqlite": ("sqlite/sqlite",),
    "nginx": ("nginx/nginx",),
    "vscode": ("microsoft/vscode",),
    "whisper": ("openai/whisper", "openai"),
    "stable diffusion": ("Stability-AI", "CompVis/stable-diffusion"),
    "numpy": ("numpy/numpy",),
}


def domains_for(text: str) -> list[str]:
    lowered = text.lower()
    result: list[str] = []
    for key, domains in OFFICIAL_DOCS.items():
        if key in lowered:
            for domain in domains:
                if domain not in result:
                    result.append(domain)
    return result


def github_hints_for(text: str) -> list[str]:
    """Expected official GitHub owners/repos for technologies in the text."""
    lowered = text.lower()
    out: list[str] = []
    for key, repos in OFFICIAL_GITHUB.items():
        if key in lowered:
            for repo in repos:
                r = repo.lower()
                if r not in out:
                    out.append(r)
    return out


def package_official_domains(name: str) -> list[str]:
    """Resolve a locally-installed package's official domains from its
    own metadata (Part 23): Home-page + Project-URL entries (docs,
    documentation, repository, changelog, source)."""
    import importlib.metadata as im

    out: list[str] = []
    try:
        dist = im.distribution(name)
    except Exception:
        return out
    meta = getattr(dist, "metadata", None)
    if meta is None:
        return out
    candidates: list[str] = []
    for key in ("Home-page", "Project-URL", "Download-URL"):
        try:
            values = meta.get_all(key) or []
        except Exception:
            values = []
        candidates.extend(str(v) for v in values)
    for raw in candidates:
        # Project-URL lines look like "Documentation, https://..."
        for part in str(raw).split(","):
            part = part.strip()
            if not part.startswith("http"):
                continue
            host = host_for(part)
            if host and host not in out:
                out.append(host)
    return out


def host_for(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def looks_official(url: str, query: str = "") -> bool:
    host = host_for(url)
    if not host:
        return False
    if any(host == d or host.endswith("." + d) for d in domains_for(query)):
        return True
    return bool(re.search(r"(^|\.)docs\.", host))
