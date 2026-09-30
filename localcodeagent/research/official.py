from __future__ import annotations

import re
from urllib.parse import urlparse


OFFICIAL_DOCS: dict[str, tuple[str, ...]] = {
    "python": ("docs.python.org", "python.org"),
    "pytorch": ("pytorch.org", "docs.pytorch.org"),
    "torch": ("pytorch.org", "docs.pytorch.org"),
    "cuda": ("docs.nvidia.com", "developer.nvidia.com"),
    "nvidia": ("docs.nvidia.com", "developer.nvidia.com"),
    "react": ("react.dev",),
    "node": ("nodejs.org",),
    "node.js": ("nodejs.org",),
    "typescript": ("typescriptlang.org",),
    "cmake": ("cmake.org",),
    "qt": ("doc.qt.io", "qt.io"),
    ".net": ("learn.microsoft.com", "dotnet.microsoft.com"),
    "dotnet": ("learn.microsoft.com", "dotnet.microsoft.com"),
    "asp.net": ("learn.microsoft.com",),
    "github": ("docs.github.com", "github.com"),
    "llama.cpp": ("github.com",),
    "comfyui": ("docs.comfy.org", "github.com"),
    "qwen": ("qwenlm.github.io", "huggingface.co", "github.com"),
    "flux": ("bfl.ai", "huggingface.co", "github.com"),
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
