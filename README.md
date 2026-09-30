# Local Code Agent

A local-first ChatGPT-style coding agent with automatic model routing, managed local inference, transactional editing, task checkpoints, verification, review, and modular tools.

> **Project source of truth:** this GitHub repository. Future development sessions should begin by reading `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `SESSION_HANDOFF.md`.

## Current baseline: v0.3.0

Implemented:
- automatic model routing and escalation
- resource-aware llama.cpp runtime management
- transactional multi-file patching
- task checkpoints and undo
- approval-gated tool execution
- automatic build/test verification
- reviewer-model handoff
- persistent project memory
- lightweight repository index
- local web UI

## Active development

The next workstream adds:
1. web research / browser tooling;
2. a modular local image-generation and image-editing system;
3. ComfyUI backend integration;
4. image-model routing (Qwen-Image-2.1 quality path, FLUX.2 Klein 4B fast path);
5. image jobs, history, character/subject profiles, LoRA management, and shared VRAM coordination.

The target desktop hardware is an NVIDIA RTX 3080 12 GB with 64 GB system RAM, while keeping the architecture portable to other Windows/Linux systems.

See `PROJECT_STATUS.md` and `SESSION_HANDOFF.md` for the exact implementation state and next actions.
