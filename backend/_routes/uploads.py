"""Route handlers for POST/GET /api/uploads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from api_types import UploadMediaResponse
from app_handler import AppHandler
from handlers.uploads_handler import media_type_for_upload
from state import get_state_service

router = APIRouter(prefix="/api", tags=["uploads"])


@router.post("/uploads", response_model=UploadMediaResponse)
def route_upload_media(
    file: UploadFile = File(...),
    kind: str | None = Form(None),
    handler: AppHandler = Depends(get_state_service),
) -> UploadMediaResponse:
    return handler.uploads.upload(file, kind)


@router.get("/uploads/{filename}")
def route_get_upload(
    filename: str,
    handler: AppHandler = Depends(get_state_service),
) -> FileResponse:
    path = handler.uploads.resolve_upload_file(filename)
    return FileResponse(path, media_type=media_type_for_upload(path), filename=path.name)
