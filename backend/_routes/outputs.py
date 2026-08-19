"""Route handler for GET /api/outputs/{filename}."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from handlers.outputs_handler import media_type_for
from state import get_state_service
from app_handler import AppHandler

router = APIRouter(prefix="/api", tags=["outputs"])


@router.get("/outputs/{filename}")
def route_get_output(
    filename: str,
    handler: AppHandler = Depends(get_state_service),
) -> FileResponse:
    path = handler.outputs.resolve_output_file(filename)
    return FileResponse(path, media_type=media_type_for(path), filename=path.name)
