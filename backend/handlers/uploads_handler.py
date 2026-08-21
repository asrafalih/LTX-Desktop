"""LAN media uploads under app_data/uploads with opaque /api/uploads URLs."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import UploadFile

from _routes._errors import HTTPError
from api_types import UploadMediaResponse
from runtime_config.runtime_config import RuntimeConfig
from server_utils.media_validation import validate_audio_file, validate_image_file

logger = logging.getLogger(__name__)

UPLOAD_TTL_SECONDS = 24 * 60 * 60

_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
_AUDIO_EXTS = frozenset({".wav", ".flac", ".ogg", ".mp3", ".aac", ".m4a"})

_IMAGE_CONTENT_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
})
_AUDIO_CONTENT_TYPES = frozenset({
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/flac",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/aac",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
})

_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
}


def public_upload_url(filename: str) -> str:
    return f"/api/uploads/{filename}"


def media_type_for_upload(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


class UploadsHandler:
    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._dir = (config.app_data_dir / "uploads").resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self.sweep_expired()

    @property
    def uploads_dir(self) -> Path:
        return self._dir

    def sweep_expired(self) -> None:
        now = time.time()
        try:
            entries = list(self._dir.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > UPLOAD_TTL_SECONDS:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove expired upload: %s", path, exc_info=True)

    def resolve_upload_file(self, filename: str) -> Path:
        name = filename.strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise HTTPError(400, "Invalid upload filename")
        suffix = Path(name).suffix.lower()
        if suffix not in _IMAGE_EXTS and suffix not in _AUDIO_EXTS:
            raise HTTPError(400, "Unsupported upload file type")
        path = (self._dir / name).resolve()
        if not path.is_relative_to(self._dir):
            raise HTTPError(400, "Invalid upload filename")
        if not path.is_file():
            raise HTTPError(404, "Upload file not found")
        return path

    def upload(self, file: UploadFile, kind: str | None) -> UploadMediaResponse:
        raw_name = file.filename or ""
        content_type = (file.content_type or "").split(";")[0].strip().lower()
        resolved_kind = self._resolve_kind(kind, raw_name, content_type)
        ext = self._pick_extension(resolved_kind, raw_name, content_type)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        dest = self._dir / stored_name

        try:
            with dest.open("wb") as out:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise HTTPError(500, "Failed to store upload") from exc
        finally:
            try:
                file.file.close()
            except Exception:
                pass

        try:
            if resolved_kind == "image":
                validate_image_file(str(dest))
            else:
                validate_audio_file(str(dest))
        except HTTPError:
            dest.unlink(missing_ok=True)
            raise

        self.sweep_expired()
        return UploadMediaResponse(url=public_upload_url(stored_name))

    def _resolve_kind(self, kind: str | None, filename: str, content_type: str) -> str:
        if kind is not None:
            normalized = kind.strip().lower()
            if normalized not in {"image", "audio"}:
                raise HTTPError(400, "kind must be 'image' or 'audio'")
            return normalized

        ext = Path(filename).suffix.lower()
        if content_type in _IMAGE_CONTENT_TYPES or ext in _IMAGE_EXTS:
            if content_type in _AUDIO_CONTENT_TYPES or ext in _AUDIO_EXTS:
                raise HTTPError(400, "Ambiguous upload kind")
            return "image"
        if content_type in _AUDIO_CONTENT_TYPES or ext in _AUDIO_EXTS:
            return "audio"
        raise HTTPError(400, "Ambiguous upload kind")

    def _pick_extension(self, kind: str, filename: str, content_type: str) -> str:
        ext = Path(filename).suffix.lower()
        if kind == "image":
            if ext in _IMAGE_EXTS:
                return ".jpg" if ext == ".jpeg" else ext
            if content_type in {"image/jpeg", "image/jpg"}:
                return ".jpg"
            if content_type == "image/png":
                return ".png"
            if content_type == "image/webp":
                return ".webp"
            if content_type == "image/gif":
                return ".gif"
            if content_type == "image/bmp":
                return ".bmp"
            if content_type == "image/tiff":
                return ".tiff"
            return ".png"
        if ext in _AUDIO_EXTS:
            return ext
        if content_type in {"audio/wav", "audio/x-wav", "audio/wave"}:
            return ".wav"
        if content_type == "audio/flac":
            return ".flac"
        if content_type == "audio/ogg":
            return ".ogg"
        if content_type in {"audio/mpeg", "audio/mp3"}:
            return ".mp3"
        if content_type == "audio/aac":
            return ".aac"
        if content_type in {"audio/mp4", "audio/m4a", "audio/x-m4a"}:
            return ".m4a"
        return ".wav"
