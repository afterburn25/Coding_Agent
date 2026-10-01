from __future__ import annotations

from typing import Any

# Manifest defaults for the built-in tools. `ToolRegistry.register` merges
# these onto a `ToolSpec` when the registration call did not set the field.
# External/manifest/MCP tools supply their own metadata at registration time.
BUILTIN_MANIFESTS: dict[str, dict[str, Any]] = {
    # -- coding / workspace ---------------------------------------------------
    "list_files": {"category": "coding", "capabilities": ["list_files", "workspace_browse"]},
    "read_file": {"category": "coding", "capabilities": ["read_file", "workspace_read"]},
    "write_file": {"category": "coding", "capabilities": ["write_file", "workspace_write"]},
    "apply_patch": {"category": "coding", "capabilities": ["apply_patch", "transactional_edit"]},
    "search_text": {"category": "coding", "capabilities": ["search_text", "search_code", "workspace_search"]},
    "search_repo_index": {"category": "coding", "capabilities": ["search_repo_index", "search_code", "repository_index"]},
    "rebuild_repo_index": {"category": "coding", "capabilities": ["rebuild_repo_index", "repository_index"]},
    "run_shell": {"category": "devops", "capabilities": ["run_shell", "terminal", "execute_process"]},
    # -- git / github ----------------------------------------------------------
    "git_status": {"category": "git", "capabilities": ["git_status", "version_control"]},
    "git_diff": {"category": "git", "capabilities": ["git_diff", "version_control"]},
    "git_current_branch": {"category": "git", "capabilities": ["git_branch", "version_control"]},
    "git_create_branch": {"category": "git", "capabilities": ["git_branch", "git_create_branch", "version_control"]},
    "git_commit": {"category": "git", "capabilities": ["git_commit", "version_control"]},
    "git_push": {"category": "git", "capabilities": ["git_push", "version_control"], "requires_network": True},
    "github_repository": {"category": "git", "capabilities": ["github_repository", "github"], "requires_network": True, "provider": "github"},
    "github_list_issues": {"category": "git", "capabilities": ["github_issues", "github"], "requires_network": True, "provider": "github"},
    "github_create_issue": {"category": "git", "capabilities": ["github_issues", "github"], "requires_network": True, "provider": "github"},
    "github_create_pull_request": {"category": "git", "capabilities": ["github_pull_requests", "github"], "requires_network": True, "provider": "github"},
    "github_ci_status": {"category": "git", "capabilities": ["github_actions", "github"], "requires_network": True, "provider": "github"},
    # -- research / web ---------------------------------------------------------
    "research_topic": {"category": "research", "capabilities": ["research_topic", "research"], "requires_network": True},
    "search_documentation": {"category": "research", "capabilities": ["search_documentation", "research"], "requires_network": True},
    "search_repository": {"category": "research", "capabilities": ["search_repository", "search_code", "research"]},
    "search_github": {"category": "research", "capabilities": ["search_github", "research", "github"], "requires_network": True, "provider": "github"},
    "search_errors": {"category": "research", "capabilities": ["search_errors", "search_error", "research"], "requires_network": True},
    "lookup_api": {"category": "research", "capabilities": ["lookup_api", "research"], "requires_network": True},
    "read_release_notes": {"category": "research", "capabilities": ["read_release_notes", "research"], "requires_network": True},
    "check_package_version": {"category": "research", "capabilities": ["check_package_version", "dependencies"]},
    "summarize_research": {"category": "research", "capabilities": ["summarize_research", "research"]},
    "get_cached_research": {"category": "research", "capabilities": ["get_cached_research", "research"]},
    "web_search": {"category": "research", "capabilities": ["web_search", "research"], "requires_network": True},
    "fetch_url": {"category": "research", "capabilities": ["fetch_url", "research"], "requires_network": True},
    # -- browser ------------------------------------------------------------------
    "browser_run": {"category": "browsers", "capabilities": ["browser_run", "browser_automation"], "requires_network": True, "provider": "playwright"},
    # -- images ------------------------------------------------------------------
    "generate_image": {"category": "images", "capabilities": ["generate_image", "text_to_image"], "requires_gpu": True, "provider": "comfyui"},
    "edit_image": {"category": "images", "capabilities": ["edit_image", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "inpaint_image": {"category": "images", "capabilities": ["inpaint", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "outpaint_image": {"category": "images", "capabilities": ["outpaint", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "remove_background": {"category": "images", "capabilities": ["remove_background", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "upscale_image": {"category": "images", "capabilities": ["upscale", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "create_image_variations": {"category": "images", "capabilities": ["variations", "image_edit"], "requires_gpu": True, "provider": "comfyui"},
    "list_image_models": {"category": "images", "capabilities": ["image_models"], "provider": "comfyui"},
    "load_subject_profile": {"category": "images", "capabilities": ["subject_profile"], "provider": "comfyui"},
    "verify_image_models": {"category": "images", "capabilities": ["image_models", "verify"], "provider": "comfyui"},
    "install_image_model": {"category": "images", "capabilities": ["image_models", "install"], "requires_network": True, "provider": "comfyui"},
    "list_loras": {"category": "images", "capabilities": ["loras"], "provider": "comfyui"},
}


def manifest_defaults(name: str) -> dict[str, Any]:
    return dict(BUILTIN_MANIFESTS.get(name, {}))
