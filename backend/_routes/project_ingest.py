"""Route handlers for /api/project-ingest."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api_types import ProjectIngestJobDetailResponse, ProjectIngestListResponse, StatusResponse
from state import get_state_service
from app_handler import AppHandler

router = APIRouter(prefix="/api", tags=["project-ingest"])


@router.get("/project-ingest", response_model=ProjectIngestListResponse)
def route_list_project_ingest(
    handler: AppHandler = Depends(get_state_service),
) -> ProjectIngestListResponse:
    handler.video_generation.reconcile_project_ingest()
    return handler.project_ingest.list_jobs()


@router.get("/project-ingest/{job_id}", response_model=ProjectIngestJobDetailResponse)
def route_get_project_ingest(
    job_id: str,
    handler: AppHandler = Depends(get_state_service),
) -> ProjectIngestJobDetailResponse:
    handler.video_generation.reconcile_project_ingest()
    return handler.project_ingest.get_job(job_id)


@router.delete("/project-ingest/{job_id}", response_model=StatusResponse)
def route_delete_project_ingest(
    job_id: str,
    handler: AppHandler = Depends(get_state_service),
) -> StatusResponse:
    handler.video_generation.mark_ingest_deleted(job_id)
    return StatusResponse(status="ok")
