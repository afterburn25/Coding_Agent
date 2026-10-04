from __future__ import annotations

from typing import Any


def describe_image_error(exc: BaseException) -> dict[str, Any]:
    """Turn backend/runtime exceptions into stable user-facing image errors.

    The friendly message is safe to show by default. ``technical_details`` is
    intentionally separate so UIs can keep it collapsed unless debugging is
    requested.
    """
    raw = str(exc or "").strip()
    low = raw.lower()
    technical = f"{type(exc).__name__}: {raw}" if raw else type(exc).__name__

    def result(code: str, message: str) -> dict[str, str]:
        return {"code": code, "message": message, "technical_details": technical[:4000]}

    from localcodeagent.netdiag import BackendConnectionError
    if isinstance(exc, BackendConnectionError):
        out = result(f"transport_{exc.kind}", exc.friendly)
        out["diagnostic"] = exc.diagnostic()
        return out

    if any(x in low for x in ("cuda out of memory", "outofmemoryerror", "cudnn_status_alloc_failed", "hip out of memory")):
        return result("cuda_out_of_memory", "The GPU ran out of memory while processing this image. Try a lower resolution, fewer images, a smaller/quantized model, or a more aggressive VRAM cleanup mode.")
    if any(x in low for x in ("hostbuffer", "read_file_slice", "cannot allocate memory", "memoryerror", "bad alloc")):
        return result("host_buffer_read", "System RAM ran out while loading the image model. Close other programs or let Nexus unload idle chat models, then retry.")
    if any(x in low for x in ("no space left on device", "disk full", "insufficient disk")):
        return result("insufficient_disk_space", "There is not enough free disk space to complete the image operation. Free some storage and try again.")
    if any(x in low for x in ("comfyui is not installed", "comfyui was not found")):
        return result("backend_not_installed", "ComfyUI — the local image engine — is not installed yet. Nexus can install it together with the image models.")
    if any(x in low for x in ("comfyui backend is offline", "connection refused", "failed to establish a new connection", "backend offline")):
        return result("backend_offline", "The local ComfyUI image backend is offline. Start it from the Image workspace and try again.")
    if "vae" in low and any(x in low for x in ("missing", "not found", "does not exist", "invalid")):
        return result("missing_vae", "The workflow needs a VAE that is missing or invalid. Verify the selected image model installation.")
    if "lora" in low and any(x in low for x in ("not installed", "not marked compatible", "disabled", "version", "slot", "ambiguous", "strength")):
        return result("incompatible_lora", "A selected LoRA is unavailable, disabled, incompatible, or not supported by this workflow. Check the LoRA selection and workflow slots.")
    if any(x in low for x in ("not fully installed", "missing/invalid", "model is missing", "model not found", "checkpoint not found")):
        return result("missing_model", "One or more required image-model files are missing or invalid. Run Verify/Install in the Image workspace.")
    if any(x in low for x in ("workflow is missing", "workflow '" , "api workflow", "unresolved variable", "no comfyui api workflow", "does not expose lora slot")):
        return result("unsupported_workflow", "The selected image workflow is missing or not executable. Import a validated ComfyUI API workflow for this model and operation.")
    if any(x in low for x in ("missing required node", "missing required node(s)", "module not found", "modulenotfounderror", "failed dependency")):
        return result("failed_dependency", "The image backend is missing a required node or dependency. Update ComfyUI or install the required local node package.")
    if any(x in low for x in ("corrupt", "invalid header", "header too large", "safetensor", "checkpoint")) and any(x in low for x in ("error", "invalid", "corrupt", "failed")):
        return result("corrupt_checkpoint", "An image-model checkpoint appears to be corrupt or unreadable. Verify the file and use Repair if needed.")
    if isinstance(exc, TimeoutError) or "timed out" in low or "timeout" in low:
        return result("timeout", "The image operation timed out before the backend finished. Check the backend status and retry with a smaller job if needed.")

    message = raw if raw else "The image operation failed."
    if len(message) > 360:
        message = message[:357] + "..."
    return result("generation_failed", message)
