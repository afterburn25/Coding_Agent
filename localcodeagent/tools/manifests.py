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
    "terminal_run": {"category": "devops", "capabilities": ["terminal_run", "terminal", "execute_process"]},
    "terminal_processes": {"category": "devops", "capabilities": ["terminal_processes", "terminal"]},
    "terminal_kill": {"category": "devops", "capabilities": ["terminal_kill", "terminal"]},
    "search_code": {"category": "coding", "capabilities": ["search_code", "search_text", "repository_search"], "provider": "ripgrep"},
    "search_filename": {"category": "coding", "capabilities": ["search_filename", "repository_search"], "provider": "ripgrep"},
    "search_error": {"category": "coding", "capabilities": ["search_error", "repository_search"], "provider": "ripgrep"},
    "detect_build_system": {"category": "coding", "capabilities": ["detect_build_system", "repository_search"]},
    "build_project": {"category": "coding", "capabilities": ["build_project", "terminal_run"]},
    "run_tests": {"category": "coding", "capabilities": ["run_tests", "terminal_run"]},
    "configure_project": {"category": "coding", "capabilities": ["build_project", "terminal_run"]},
    "clean_project": {"category": "coding", "capabilities": ["build_project", "terminal_run"]},
    "code_symbols": {"category": "coding", "capabilities": ["code_symbols", "repository_search"]},
    "code_map": {"category": "coding", "capabilities": ["code_map", "repository_search"]},
    # -- data ------------------------------------------------------------------
    "data_query": {"category": "data", "capabilities": ["query_csv", "query_json", "query_database", "execute_sql", "summarize_table"], "provider": "duckdb"},
    "profile_dataset": {"category": "data", "capabilities": ["profile_dataset", "summarize_table"], "provider": "duckdb"},
    "chart_generate": {"category": "data", "capabilities": ["chart_generate", "visualization"]},
    # -- external apis / secrets ------------------------------------------------
    "api_request": {"category": "external_apis", "capabilities": ["api_request", "http_client", "external_api"], "requires_network": True},
    "secrets_list": {"category": "utilities", "capabilities": ["secrets_list"]},
    # -- media -----------------------------------------------------------------
    "media_probe": {"category": "video", "capabilities": ["probe_media", "verify_media", "media_metadata"], "provider": "ffmpeg-project"},
    "extract_audio": {"category": "video", "capabilities": ["extract_audio"], "provider": "ffmpeg-project"},
    "trim_video": {"category": "video", "capabilities": ["trim_video"], "provider": "ffmpeg-project"},
    "convert_video": {"category": "video", "capabilities": ["convert_video", "change_container"], "provider": "ffmpeg-project"},
    "create_thumbnail": {"category": "video", "capabilities": ["create_thumbnail", "extract_frames"], "provider": "ffmpeg-project"},
    "normalize_audio": {"category": "audio", "capabilities": ["normalize_audio"], "provider": "ffmpeg-project"},
    "add_subtitles": {"category": "video", "capabilities": ["add_subtitles"], "provider": "ffmpeg-project"},
    "media_transcribe": {"category": "audio", "capabilities": ["transcribe_video", "transcribe_audio", "generate_subtitles", "media_pipeline"], "provider": "nexus"},
    "speak_text": {"category": "audio", "capabilities": ["speak_text", "generate_speech", "tts"], "provider": "rhasspy"},
    # -- documents -------------------------------------------------------------
    "extract_text": {"category": "documents", "capabilities": ["extract_text", "read_document", "read_pdf", "parse_csv", "html_to_text"], "provider": "nexus"},
    "ocr_image": {"category": "documents", "capabilities": ["ocr_image", "extract_text_from_screenshot", "detect_text_regions"], "provider": "tesseract-ocr"},
    "convert_document": {"category": "documents", "capabilities": ["convert_document", "markdown_to_docx", "markdown_to_html", "docx_to_markdown"], "provider": "pandoc"},
    # -- knowledge + sandbox ----------------------------------------------------
    "knowledge_index": {"category": "research", "capabilities": ["index_knowledge", "build_rag_index", "embed_documents"], "provider": "nexus"},
    "knowledge_search": {"category": "research", "capabilities": ["search_knowledge", "local_rag", "recall_documents"], "provider": "nexus"},
    "knowledge_forget": {"category": "research", "capabilities": ["forget_knowledge", "manage_index"], "provider": "nexus"},
    "python_exec": {"category": "utilities", "capabilities": ["execute_code", "run_python", "sandbox_eval"], "provider": "nexus"},
    "list_workflows": {"category": "utilities", "capabilities": ["list_workflows", "automation"], "provider": "nexus"},
    "run_workflow": {"category": "utilities", "capabilities": ["run_workflow", "multi_step_automation", "orchestrate_tools"], "provider": "nexus"},
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
