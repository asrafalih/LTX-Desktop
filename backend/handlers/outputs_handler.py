"""Resolve generated media files under outputs_dir for download."""

from __future__ import annotations

from pathlib import Path

from _routes._errors import HTTPError
from runtime_config.runtime_config import RuntimeConfig

ALLOWED_OUTPUT_SUFFIXES = frozenset({".mp4", ".png", ".jpg", ".jpeg", ".webp"})

_MEDIA_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def public_output_url(path: str | Path) -> str:
    return f"/api/outputs/{Path(path).name}"


def media_type_for(path: Path) -> str:
    return _MEDIA_TYPES[path.suffix.lower()]


class OutputsHandler:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    def resolve_output_file(self, filename: str) -> Path:
        name = filename.strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise HTTPError(400, "Invalid output filename")
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_OUTPUT_SUFFIXES:
            raise HTTPError(400, "Unsupported output file type")
        outputs_dir = self._config.outputs_dir.resolve()
        path = (outputs_dir / name).resolve()
        if not path.is_relative_to(outputs_dir):
            raise HTTPError(400, "Invalid output filename")
        if not path.is_file():
            raise HTTPError(404, "Output file not found")
        return path
