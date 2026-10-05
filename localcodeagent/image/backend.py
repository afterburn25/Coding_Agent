from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Operation vocabulary shared by all image backends. A backend advertises
# the subset it can actually execute — the router never sends a job to a
# backend that didn't claim the operation.
OP_TEXT_TO_IMAGE = "text_to_image"
OP_IMAGE_EDIT = "edit_image"
OP_INPAINT = "inpaint"
OP_OUTPAINT = "outpaint"
OP_REMOVE_BACKGROUND = "remove_background"
OP_UPSCALE = "upscale"
OP_VARIATION = "variation"

ALL_OPERATIONS = {
    OP_TEXT_TO_IMAGE, OP_IMAGE_EDIT, OP_INPAINT, OP_OUTPAINT,
    OP_REMOVE_BACKGROUND, OP_UPSCALE, OP_VARIATION,
}


class ImageBackend(ABC):
    """One image-generation engine managed by Nexus.

    ``submit`` takes a backend-native payload dict — ComfyUI receives a
    rendered API workflow, InvokeAI a normalized generation spec it
    translates into its queue graph. Everything else is uniform:
    id → status polling → fetch_outputs → cancel.
    """

    name: str = "backend"
    display_name: str = "Image backend"

    @abstractmethod
    def health(self) -> tuple[bool, str]: ...

    @abstractmethod
    def inspect(self) -> dict[str, Any]: ...

    @abstractmethod
    def submit(self, workflow: dict[str, Any]) -> str: ...

    @abstractmethod
    def status(self, backend_job_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_outputs(self, backend_job_id: str, destination: Path) -> list[Path]: ...

    @abstractmethod
    def cancel(self, backend_job_id: str) -> None: ...

    def capabilities(self) -> set[str]:
        """Operations this backend can execute right now. Empty = unknown."""
        return set()

    def models(self) -> list[dict[str, Any]]:
        """Models the backend itself reports as installed/available."""
        return []

    def upload_image(self, path: Path, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError(f"{self.name} does not accept image uploads")

    def supports(self, operation: str) -> bool:
        return operation in self.capabilities()
