"""Video generation orchestration handler."""

from __future__ import annotations

import logging
import os

from frame_math import AutoDurationSpec, compute_num_frames, snap_up_to_multiple
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from PIL import Image

from api_types import (
    GenerateVideoCancelledResponse,
    GenerateVideoCompleteResponse,
    GenerateVideoModelsSpecsResponse,
    GenerateVideoQueuedResponse,
    GenerateVideoRequest,
    GenerateVideoResponse,
    ImageConditioningInput,
    LoraEntry,
    LTXLocalModelId,
    LTXVideoGenResolution,
    VideoCameraMotion,
)
from runtime_config.ltx_capabilities import LtxAspectRatio, api_caps, local_caps, pixels_for, supports
from runtime_config.models_scanner import resolve_lora_ref
from _routes._errors import HTTPError
from api_model_specs import (
    FORCED_API_MODEL_MAP,
    build_generate_video_model_specs_response,
    get_local_video_generation_model_specs,
    supported_duration_range,
    validate_generate_video_request,
)
from handlers.outputs_handler import public_output_url
from handlers.base import StateHandlerBase
from server_utils.heartbeat import log_heartbeat
from handlers.generate_queue_store import GenerateJobStatus, GenerateQueueStore
from handlers.generation_handler import GenerationHandler
from handlers.pipelines_handler import PipelinesHandler
from handlers.project_ingest_handler import ProjectIngestHandler, normalize_project_name
from handlers.prompt_enhancement_handler import PromptEnhancementHandler
from handlers.text_handler import TextHandler
from handlers.video_generate_queue import VideoGenerateQueue
from runtime_config.model_download_specs import is_duration_head_ready, resolve_active_ltx_model_id
from server_utils.media_validation import (
    normalize_optional_path,
    resolve_media_ref,
    validate_audio_file,
    validate_image_file,
)
from services.generation_interrupt import GenerationCancelledError, is_cancel_exception
from services.interfaces import LTXAPIClient
from services.ltx_api_client.ltx_api_client import LTXAPIClientError
from state.app_state_types import AppState
from state.app_settings import should_video_generate_with_ltx_api

if TYPE_CHECKING:
    from runtime_config.runtime_config import RuntimeConfig

logger = logging.getLogger(__name__)


def _complete_video_response(video_path: str | Path) -> GenerateVideoCompleteResponse:
    path_str = str(video_path)
    return GenerateVideoCompleteResponse(
        status="complete",
        video_path=path_str,
        video_url=public_output_url(path_str),
    )


def _wxh(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def _forced_api_resolution_map() -> dict[str, dict[str, str]]:
    caps = api_caps("fast")
    return {
        resolution: {
            "16:9": _wxh(pixels_for(caps, resolution, "16:9")),
            "9:16": _wxh(pixels_for(caps, resolution, "9:16")),
        }
        for resolution in caps.resolution_pixels_16_9
    }


FORCED_API_RESOLUTION_MAP: dict[str, dict[str, str]] = _forced_api_resolution_map()
FORCED_API_ALLOWED_ASPECT_RATIOS = {"16:9", "9:16"}
_LTX_INSUFFICIENT_FUNDS_MESSAGE = "Your LTX API credits are insufficient for this generation. Buy more credits and try again."


class VideoGenerationHandler(StateHandlerBase):
    def __init__(
        self,
        state: AppState,
        lock: RLock,
        generation_handler: GenerationHandler,
        pipelines_handler: PipelinesHandler,
        text_handler: TextHandler,
        prompt_enhancement_handler: PromptEnhancementHandler,
        ltx_api_client: LTXAPIClient,
        config: RuntimeConfig,
        project_ingest_handler: ProjectIngestHandler,
    ) -> None:
        super().__init__(state, lock, config)
        self._generation = generation_handler
        self._pipelines = pipelines_handler
        self._text = text_handler
        self._prompt_enhancement = prompt_enhancement_handler
        self._ltx_api_client = ltx_api_client
        self._project_ingest = project_ingest_handler
        self._queue_store = GenerateQueueStore(config.app_data_dir / "generate_queue.sqlite")
        self._queue = VideoGenerateQueue(self._run_queued_job)

    def resume_durable_queue(self) -> None:
        """Rebuild the in-memory queue from incomplete SQLite rows (call after settings load)."""
        self._resume_durable_queue()

    def resume_and_reconcile(self) -> None:
        """Resume durable jobs then drop orphaned ingest JSON. Call after load_persistent_state."""
        self._resume_durable_queue()
        self.reconcile_project_ingest()

    def _resume_durable_queue(self) -> None:
        self._queue_store.reset_running_to_queued()
        live = self._queue.live_job_ids()
        for record in self._queue_store.list_incomplete():
            if record.id in live:
                continue
            self._project_ingest.begin_from_generate(record.request, record.id)
            try:
                self._queue.enqueue(record.id, record.request)
            except HTTPError as exc:
                if exc.status_code == 429:
                    logger.error("Could not resume job %s: queue full", record.id)
                    break
                raise
            live.add(record.id)

    def reconcile_project_ingest(self) -> None:
        incomplete = {job.id: job for job in self._queue_store.list_incomplete()}
        live = self._queue.live_job_ids()
        disk_jobs = self._project_ingest.list_jobs().jobs
        disk_ids = {job.id for job in disk_jobs}

        for disk_job in disk_jobs:
            if disk_job.video_path:
                continue
            if disk_job.id not in incomplete:
                self._project_ingest.drop_if_incomplete(disk_job.id)

        for job_id, record in incomplete.items():
            if job_id not in disk_ids:
                self._project_ingest.begin_from_generate(record.request, job_id)
                disk_ids.add(job_id)
            if job_id not in live:
                try:
                    self._queue.enqueue(job_id, record.request)
                    live.add(job_id)
                except HTTPError as exc:
                    if exc.status_code == 429:
                        logger.error("Reconcile could not enqueue %s: queue full", job_id)
                    else:
                        raise

    def _fail_project_job(
        self,
        req: GenerateVideoRequest,
        generation_id: str,
        *,
        status: GenerateJobStatus,
        error: str | None = None,
    ) -> None:
        if normalize_project_name(req.projectName) is None:
            self._project_ingest.drop_if_incomplete(generation_id)
            return
        self._queue_store.set_status(generation_id, status, error=error)
        self._project_ingest.drop_if_incomplete(generation_id)

    def _complete_project_job(
        self, req: GenerateVideoRequest, generation_id: str, output_path: str | Path
    ) -> None:
        path_str = str(output_path)
        if normalize_project_name(req.projectName) is not None:
            row = self._queue_store.get(generation_id)
            # DELETE while running marks cancelled; do not resurrect as complete.
            if row is not None and row.status == "cancelled":
                return
            self._project_ingest.enqueue_from_generate(req, path_str, generation_id)
            self._queue_store.set_status(generation_id, "complete", video_path=path_str)
            return
        self._project_ingest.enqueue_from_generate(req, path_str, generation_id)

    def mark_ingest_deleted(self, job_id: str) -> None:
        """Cancel queued job or mark incomplete SQLite cancelled, then drop ingest JSON."""
        if self.cancel_queued(job_id):
            return
        row = self._queue_store.get(job_id)
        if row is not None and row.status in ("queued", "running"):
            self._queue_store.set_status(job_id, "cancelled")
        self._project_ingest.delete_job(job_id)

    def _normalize_media_path(self, value: str | None) -> str | None:
        normalized = normalize_optional_path(value)
        if normalized is None:
            return None
        return resolve_media_ref(
            normalized, uploads_dir=self.config.app_data_dir / "uploads"
        )

    def _resolve_prompt_enhancement(
        self, prompt: str, *, image_path: str | None
    ) -> tuple[str, bool]:
        """Apply the enhancer setting, returning ``(prompt, enhance_via_api)``.

        The two encoding paths enhance in different places: API encoding rewrites server-side
        inside the same /prompt-embedding call, so it only needs the flag forwarded. Local
        encoding has no such step, so the rewrite happens here — without it the model sees the
        prompt as typed, which for a version captioned in 150-220 word audio-visual paragraphs
        (2.5) lands far outside its training distribution and it invents the rest.

        Must be called before the pipeline is loaded and before start_generation: the enhancer
        needs the VRAM a resident pipeline holds, and PipelinesHandler refuses to evict one
        while a generation is running.
        """
        settings = self.state.app_settings
        enabled = (
            settings.prompt_enhancer_enabled_i2v if image_path is not None
            else settings.prompt_enhancer_enabled_t2v
        )
        if not enabled:
            return prompt, False
        if not self._text.should_use_local_encoding():
            return prompt, True
        return self._prompt_enhancement.enhance_for_generation(prompt, image_path=image_path), False

    def _active_ltx_model_id(self) -> LTXLocalModelId | None:
        return resolve_active_ltx_model_id(
            self.models_dir, self.state.app_settings.active_ltx_model_id
        )

    def _duration_head_ready(self) -> bool:
        model_id = self._active_ltx_model_id()
        return model_id is not None and is_duration_head_ready(self.models_dir, model_id)

    def _local_pixels(
        self,
        resolution: LTXVideoGenResolution,
        aspect: LtxAspectRatio,
        *,
        invalid_code: str,
    ) -> tuple[int, int]:
        model_id = self._active_ltx_model_id()
        if model_id is None:
            raise HTTPError(409, "NO_DOWNLOADED_LTX_MODEL")
        try:
            return pixels_for(local_caps(model_id), resolution, aspect)
        except KeyError as exc:
            raise HTTPError(400, invalid_code) from exc

    def get_model_specs(self) -> GenerateVideoModelsSpecsResponse:
        return build_generate_video_model_specs_response(
            local_model_id=self._active_ltx_model_id(),
            duration_head_ready=self._duration_head_ready(),
        )

    def generate(self, req: GenerateVideoRequest) -> GenerateVideoResponse:
        use_api_specs = should_video_generate_with_ltx_api(
            force_api_generations=self.config.force_api_generations,
            settings=self.state.app_settings,
        )

        end_image_path = self._normalize_media_path(req.endImagePath)
        if end_image_path is not None:
            if self._normalize_media_path(req.imagePath) is None:
                raise HTTPError(
                    400,
                    "END_IMAGE_REQUIRES_START",
                    code="END_IMAGE_REQUIRES_START",
                )
            if use_api_specs:
                raise HTTPError(
                    400,
                    "END_IMAGE_LOCAL_ONLY",
                    code="END_IMAGE_LOCAL_ONLY",
                )
            if req.duration is None:
                raise HTTPError(
                    400,
                    "END_IMAGE_REQUIRES_DURATION",
                    code="END_IMAGE_REQUIRES_DURATION",
                )

        validation_error = validate_generate_video_request(
            req,
            use_api_specs=use_api_specs,
            local_model_id=None if use_api_specs else self._active_ltx_model_id(),
            duration_head_ready=False if use_api_specs else self._duration_head_ready(),
        )
        if validation_error is not None:
            raise HTTPError(422, validation_error, code="INVALID_VIDEO_GENERATION_SPEC")

        audio_path = self._normalize_media_path(req.audioPath)
        if audio_path and req.duration is None:
            raise HTTPError(
                422,
                "Automatic duration cannot be combined with audio-to-video",
                code="INVALID_VIDEO_GENERATION_SPEC",
            )

        generation_id = self._make_generation_id()
        try:
            # projectName → non-blocking enqueue so each desktop Generate frees the HTTP
            # connection immediately (browsers only allow ~6 concurrent connections/host).
            # Curl without projectName still blocks until the video is ready.
            # Enqueue before durable writes so a 429 never leaves a SQLite/ingest row.
            if normalize_project_name(req.projectName) is not None:
                self._queue.enqueue(generation_id, req)
                try:
                    self._queue_store.insert_queued(generation_id, req)
                except Exception:
                    self._queue.cancel_queued(generation_id)
                    raise
                self._project_ingest.begin_from_generate(req, generation_id)
                return GenerateVideoQueuedResponse(status="queued", id=generation_id)
            self._project_ingest.begin_from_generate(req, generation_id)
            return self._queue.submit(generation_id, req)
        except HTTPError:
            self._project_ingest.drop_if_incomplete(generation_id)
            raise

    def cancel_queued(self, job_id: str) -> bool:
        cancelled = self._queue.cancel_queued(job_id)
        if cancelled:
            self._queue_store.set_status(job_id, "cancelled")
            self._project_ingest.drop_if_incomplete(job_id)
        return cancelled

    def _run_queued_job(self, req: GenerateVideoRequest, generation_id: str) -> GenerateVideoResponse:
        use_api_specs = should_video_generate_with_ltx_api(
            force_api_generations=self.config.force_api_generations,
            settings=self.state.app_settings,
        )
        if normalize_project_name(req.projectName) is not None:
            self._queue_store.set_status(generation_id, "running")
        if use_api_specs:
            return self._generate_forced_api(req, generation_id)

        with self._generation.reserved_generation_start():

            resolution = req.resolution
            duration = req.duration
            fps = req.fps

            audio_path = self._normalize_media_path(req.audioPath)
            if audio_path:
                assert duration is not None
                return self._generate_a2v(
                    req, duration, fps, audio_path=audio_path, generation_id=generation_id
                )

            logger.info("Resolution %s - using fast pipeline", resolution)

            width, height = self._local_pixels(
                resolution, req.aspectRatio, invalid_code="INVALID_LOCAL_RESOLUTION"
            )

            if duration is None:
                item = next(
                    candidate
                    for candidate in get_local_video_generation_model_specs(
                        self._active_ltx_model_id(),
                        duration_head_ready=self._duration_head_ready(),
                    )
                    if candidate.pipeline == req.model
                )
                min_seconds, max_seconds = supported_duration_range(
                    item, resolution=resolution, fps=fps
                )
                num_frames: int | AutoDurationSpec = AutoDurationSpec(
                    min_seconds=float(min_seconds),
                    max_seconds=float(max_seconds),
                )
            else:
                num_frames = self._compute_num_frames(duration, fps)

            image = None
            end_image = None
            image_path = self._normalize_media_path(req.imagePath)
            if image_path:
                image = self._prepare_image(image_path, width, height)
                logger.info("Image: %s -> %sx%s", image_path, width, height)

            end_path = self._normalize_media_path(req.endImagePath)
            if end_path:
                end_image = self._prepare_image(end_path, width, height)
                logger.info("End image: %s -> %sx%s", end_path, width, height)

            try:
                seed = req.seed if req.seed is not None else self._resolve_seed()
                loras = self._resolve_loras(req.loras)

                # Before the pipeline loads and before the generation is marked running: local
                # enhancement needs the VRAM a resident pipeline holds, and evicting a pipeline is
                # refused once a generation is running.
                prompt, enhance_via_api = self._resolve_prompt_enhancement(
                    req.prompt, image_path=image_path
                )

                self._generation.raise_if_cancelled()
                self._pipelines.load_gpu_pipeline("fast", loras=loras)
                self._generation.start_generation(generation_id)

                output_path = self.generate_video(
                    prompt=prompt,
                    enhance_via_api=enhance_via_api,
                    image=image,
                    height=height,
                    width=width,
                    num_frames=num_frames,
                    fps=fps,
                    seed=seed,
                    camera_motion=req.cameraMotion,
                    negative_prompt=req.negativePrompt,
                    loras=loras,
                    end_image=end_image,
                    end_image_strength=req.endImageStrength,
                )

                self._generation.complete_generation(output_path)
                self._complete_project_job(req, generation_id, output_path)
                return _complete_video_response(output_path)

            except HTTPError as e:
                self._generation.fail_generation(e.detail)
                self._fail_project_job(req, generation_id, status="failed", error=e.detail)
                raise
            except Exception as e:
                self._generation.fail_generation(str(e))
                if is_cancel_exception(e):
                    self._fail_project_job(req, generation_id, status="cancelled")
                    logger.info("Generation cancelled by user")
                    return GenerateVideoCancelledResponse(status="cancelled")

                self._fail_project_job(req, generation_id, status="failed", error=str(e))
                raise HTTPError(500, str(e)) from e

    def _resolve_loras(self, loras: list[LoraEntry]) -> list[tuple[str, float]]:
        if loras:
            model_id = self._active_ltx_model_id()
            if model_id is None:
                raise HTTPError(409, "NO_DOWNLOADED_LTX_MODEL")
            if not supports(local_caps(model_id), "user_loras"):
                raise HTTPError(
                    409,
                    "User LoRAs are not supported for the active LTX model.",
                    code="UNSUPPORTED_USER_LORAS",
                )
        try:
            return [(str(resolve_lora_ref(self.models_dir, e.ref)), e.scale) for e in loras]
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from exc

    def generate_video(
        self,
        prompt: str,
        enhance_via_api: bool,
        image: Image.Image | None,
        height: int,
        width: int,
        num_frames: int | AutoDurationSpec,
        fps: float,
        seed: int,
        camera_motion: VideoCameraMotion,
        negative_prompt: str,
        loras: list[tuple[str, float]] | None = None,
        end_image: Image.Image | None = None,
        end_image_strength: float = 0.8,
    ) -> str:
        t_total_start = time.perf_counter()
        gen_mode = "i2v" if image is not None else "t2v"
        frames_log = (
            f"auto {num_frames.min_seconds:g}-{num_frames.max_seconds:g}s"
            if isinstance(num_frames, AutoDurationSpec)
            else f"{num_frames} frames"
        )
        logger.info("[%s] Generation started (model=fast, %dx%d, %s, %d fps)", gen_mode, width, height, frames_log, int(fps))

        self._generation.raise_if_cancelled()

        total_steps = 8

        images: list[ImageConditioningInput] = []
        temp_image_paths: list[str] = []
        if image is not None:
            temp_start = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
            image.save(temp_start)
            temp_image_paths.append(temp_start)
            images.append(ImageConditioningInput(path=temp_start, frame_idx=0, strength=1.0))

        if end_image is not None:
            if not isinstance(num_frames, int):
                raise HTTPError(
                    400,
                    "END_IMAGE_REQUIRES_DURATION",
                    code="END_IMAGE_REQUIRES_DURATION",
                )
            temp_end = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
            end_image.save(temp_end)
            temp_image_paths.append(temp_end)
            images.append(
                ImageConditioningInput(
                    path=temp_end,
                    frame_idx=num_frames - 1,
                    strength=end_image_strength,
                )
            )

        output_path = self._make_output_path()

        # Appended after any rewrite the caller already applied, so the enhancer can't
        # paraphrase the camera directive away.
        enhanced_prompt = prompt + self.config.camera_motion_prompts.get(camera_motion, "")

        try:
            self._generation.update_progress("loading_model", 5, 0, total_steps)
            t_load_start = time.perf_counter()
            pipeline_state = self._pipelines.load_gpu_pipeline("fast", loras=loras)
            t_load_end = time.perf_counter()
            logger.info("[%s] Pipeline load: %.2fs", gen_mode, t_load_end - t_load_start)

            self._generation.update_progress("encoding_text", 10, 0, total_steps)
            encoding_method = "api" if not self._text.should_use_local_encoding() else "local"
            t_text_start = time.perf_counter()
            self._text.prepare_text_encoding(enhanced_prompt, enhance_prompt=enhance_via_api)
            t_text_end = time.perf_counter()
            logger.info("[%s] Text encoding (%s): %.2fs", gen_mode, encoding_method, t_text_end - t_text_start)

            self._generation.raise_if_cancelled()
            self._generation.update_progress("inference", 15, 0, total_steps)

            # Guard for the /64 two-stage grid. Half-way values round up: Python's round() is
            # half-to-even, which turned a 544 height into 512 and silently shipped a frame 32px
            # shorter (and off its stated aspect ratio) rather than the nearest legal size.
            height = snap_up_to_multiple(height, 64)
            width = snap_up_to_multiple(width, 64)

            t_inference_start = time.perf_counter()
            with log_heartbeat(f"{gen_mode} inference"):
                pipeline_state.pipeline.generate(
                    prompt=enhanced_prompt,
                    seed=seed,
                    height=height,
                    width=width,
                    num_frames=num_frames,
                    frame_rate=fps,
                    images=images,
                    output_path=str(output_path),
                )
            t_inference_end = time.perf_counter()
            logger.info("[%s] Inference: %.2fs", gen_mode, t_inference_end - t_inference_start)

            # Denoiser interrupt cannot abort VAE decode / ffmpeg; a Stop after the last
            # denoise step still finishes encode, then this check drops the file.
            if self._generation.is_generation_cancelled():
                if output_path.exists():
                    output_path.unlink()
                raise GenerationCancelledError()

            t_total_end = time.perf_counter()
            logger.info("[%s] Total generation: %.2fs (load=%.2fs, text=%.2fs, inference=%.2fs)",
                        gen_mode, t_total_end - t_total_start,
                        t_load_end - t_load_start, t_text_end - t_text_start, t_inference_end - t_inference_start)

            self._generation.update_progress("complete", 100, total_steps, total_steps)
            return str(output_path)
        finally:
            self._text.clear_api_embeddings()
            for path in temp_image_paths:
                if os.path.exists(path):
                    os.unlink(path)

    def _generate_a2v(
        self,
        req: GenerateVideoRequest,
        duration: int,
        fps: int,
        *,
        audio_path: str,
        generation_id: str,
    ) -> GenerateVideoResponse:
        model_id = self._active_ltx_model_id()
        if model_id is None:
            raise HTTPError(409, "NO_DOWNLOADED_LTX_MODEL")
        if not supports(local_caps(model_id), "a2v"):
            raise HTTPError(
                409,
                "Audio-to-video is not supported for the active LTX model.",
                code="UNSUPPORTED_A2V",
            )
        validated_audio_path = validate_audio_file(audio_path)
        audio_path_str = str(validated_audio_path)

        width, height = self._local_pixels(
            req.resolution, req.aspectRatio, invalid_code="INVALID_LOCAL_A2V_RESOLUTION"
        )

        num_frames = self._compute_num_frames(duration, fps)

        image = None
        end_image = None
        temp_image_paths: list[str] = []
        image_path = self._normalize_media_path(req.imagePath)
        if image_path:
            image = self._prepare_image(image_path, width, height)

        end_path = self._normalize_media_path(req.endImagePath)
        if end_path:
            end_image = self._prepare_image(end_path, width, height)

        seed = req.seed if req.seed is not None else self._resolve_seed()
        loras = self._resolve_loras(req.loras)

        try:
            neg = req.negativePrompt if req.negativePrompt else self.config.default_negative_prompt

            images: list[ImageConditioningInput] = []
            if image is not None:
                temp_start = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                image.save(temp_start)
                temp_image_paths.append(temp_start)
                images.append(ImageConditioningInput(path=temp_start, frame_idx=0, strength=1.0))
            if end_image is not None:
                temp_end = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
                end_image.save(temp_end)
                temp_image_paths.append(temp_end)
                images.append(
                    ImageConditioningInput(
                        path=temp_end,
                        frame_idx=num_frames - 1,
                        strength=req.endImageStrength,
                    )
                )

            # Same ordering rule as the fast path: enhance before the pipeline takes the GPU
            # (and so before start_generation, which requires a pipeline to already be loaded).
            a2v_base_prompt, a2v_enhance = self._resolve_prompt_enhancement(
                req.prompt, image_path=image_path
            )
            enhanced_prompt = a2v_base_prompt + self.config.camera_motion_prompts.get(req.cameraMotion, "")

            self._generation.raise_if_cancelled()
            a2v_state = self._pipelines.load_a2v_pipeline(loras=loras)
            self._generation.start_generation(generation_id)

            output_path = self._make_output_path()

            total_steps = 11  # distilled: 8 steps (stage 1) + 3 steps (stage 2)

            self._generation.update_progress("loading_model", 5, 0, total_steps)
            self._generation.update_progress("encoding_text", 10, 0, total_steps)
            self._text.prepare_text_encoding(enhanced_prompt, enhance_prompt=a2v_enhance)
            self._generation.raise_if_cancelled()
            self._generation.update_progress("inference", 15, 0, total_steps)

            a2v_state.pipeline.generate(
                prompt=enhanced_prompt,
                negative_prompt=neg,
                seed=seed,
                height=height,
                width=width,
                num_frames=num_frames,
                frame_rate=fps,
                num_inference_steps=total_steps,
                images=images,
                audio_path=audio_path_str,
                audio_start_time=0.0,
                audio_max_duration=None,
                output_path=str(output_path),
            )

            # Denoiser interrupt cannot abort VAE decode / ffmpeg; a Stop after the last
            # denoise step still finishes encode, then this check drops the file.
            if self._generation.is_generation_cancelled():
                if output_path.exists():
                    output_path.unlink()
                raise GenerationCancelledError()

            self._generation.update_progress("complete", 100, total_steps, total_steps)
            self._generation.complete_generation(str(output_path))
            self._complete_project_job(req, generation_id, output_path)
            return _complete_video_response(output_path)

        except HTTPError as e:
            self._generation.fail_generation(e.detail)
            self._fail_project_job(req, generation_id, status="failed", error=e.detail)
            raise
        except Exception as e:
            self._generation.fail_generation(str(e))
            if is_cancel_exception(e):
                self._fail_project_job(req, generation_id, status="cancelled")
                logger.info("Generation cancelled by user")
                return GenerateVideoCancelledResponse(status="cancelled")
            self._fail_project_job(req, generation_id, status="failed", error=str(e))
            raise HTTPError(500, str(e)) from e
        finally:
            self._text.clear_api_embeddings()
            for path in temp_image_paths:
                if os.path.exists(path):
                    os.unlink(path)

    def _prepare_image(self, image_path: str, width: int, height: int) -> Image.Image:
        validated_path = validate_image_file(image_path)
        try:
            img = Image.open(validated_path).convert("RGB")
        except Exception:
            raise HTTPError(400, f"Invalid image file: {image_path}") from None
        img_w, img_h = img.size
        target_ratio = width / height
        img_ratio = img_w / img_h
        if img_ratio > target_ratio:
            new_h = height
            new_w = int(img_w * (height / img_h))
        else:
            new_w = width
            new_h = int(img_h * (width / img_w))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        return resized.crop((left, top, left + width, top + height))

    @staticmethod
    def _make_generation_id() -> str:
        return uuid.uuid4().hex[:8]

    @staticmethod
    def _compute_num_frames(duration: int, fps: int) -> int:
        return compute_num_frames(duration, fps)

    def _make_output_path(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.outputs_dir / f"ltx2_video_{timestamp}_{self._make_generation_id()}.mp4"

    def _generate_forced_api(
        self, req: GenerateVideoRequest, generation_id: str
    ) -> GenerateVideoResponse:
        with self._generation.reserved_generation_start():

            try:
                self._generation.start_api_generation(generation_id)

                audio_path = self._normalize_media_path(req.audioPath)
                image_path = self._normalize_media_path(req.imagePath)
                has_input_audio = bool(audio_path)
                has_input_image = bool(image_path)

                self._generation.update_progress("validating_request", 5, None, None)

                api_key = self.state.app_settings.ltx_api_key.strip()
                logger.info("Forced API generation route selected (key_present=%s)", bool(api_key))
                if not api_key:
                    raise HTTPError(400, "PRO_API_KEY_REQUIRED")

                requested_model = req.model
                api_model_id = FORCED_API_MODEL_MAP.get(requested_model)
                if api_model_id is None:
                    raise HTTPError(500, "INVALID_FORCED_API_MODEL_CONFIG")

                resolution_label = req.resolution
                resolution_by_aspect = FORCED_API_RESOLUTION_MAP.get(resolution_label)
                if resolution_by_aspect is None:
                    raise HTTPError(500, "INVALID_FORCED_API_RESOLUTION_CONFIG")

                aspect_ratio = req.aspectRatio
                if aspect_ratio not in FORCED_API_ALLOWED_ASPECT_RATIOS:
                    raise HTTPError(400, "INVALID_FORCED_API_ASPECT_RATIO")

                api_resolution = resolution_by_aspect[aspect_ratio]

                prompt = req.prompt

                self._generation.raise_if_cancelled()

                if has_input_audio:
                    validated_audio_path = validate_audio_file(audio_path)
                    validated_image_path: Path | None = None
                    if image_path is not None:
                        validated_image_path = validate_image_file(image_path)

                    self._generation.update_progress("uploading_audio", 20, None, None)
                    audio_uri = self._ltx_api_client.upload_file(
                        api_key=api_key,
                        file_path=str(validated_audio_path),
                    )
                    image_uri: str | None = None
                    if validated_image_path is not None:
                        self._generation.update_progress("uploading_image", 35, None, None)
                        image_uri = self._ltx_api_client.upload_file(
                            api_key=api_key,
                            file_path=str(validated_image_path),
                        )
                    self._generation.update_progress("inference", 55, None, None)
                    video_bytes = self._ltx_api_client.generate_audio_to_video(
                        api_key=api_key,
                        prompt=prompt,
                        audio_uri=audio_uri,
                        image_uri=image_uri,
                        model=api_model_id,
                        resolution=api_resolution,
                    )
                    self._generation.update_progress("downloading_output", 85, None, None)
                elif has_input_image:
                    validated_image_path = validate_image_file(image_path)

                    duration = req.duration
                    fps = req.fps

                    generate_audio = req.audio
                    self._generation.update_progress("uploading_image", 20, None, None)
                    image_uri = self._ltx_api_client.upload_file(
                        api_key=api_key,
                        file_path=str(validated_image_path),
                    )
                    self._generation.update_progress("inference", 55, None, None)
                    video_bytes = self._ltx_api_client.generate_image_to_video(
                        api_key=api_key,
                        prompt=prompt,
                        image_uri=image_uri,
                        model=api_model_id,
                        resolution=api_resolution,
                        duration=None if duration is None else float(duration),
                        fps=float(fps),
                        generate_audio=generate_audio,
                        camera_motion=req.cameraMotion,
                    )
                    self._generation.update_progress("downloading_output", 85, None, None)
                else:
                    duration = req.duration
                    fps = req.fps

                    generate_audio = req.audio
                    self._generation.update_progress("inference", 55, None, None)
                    video_bytes = self._ltx_api_client.generate_text_to_video(
                        api_key=api_key,
                        prompt=prompt,
                        model=api_model_id,
                        resolution=api_resolution,
                        duration=None if duration is None else float(duration),
                        fps=float(fps),
                        generate_audio=generate_audio,
                        camera_motion=req.cameraMotion,
                    )
                    self._generation.update_progress("downloading_output", 85, None, None)

                self._generation.raise_if_cancelled()

                output_path = self._write_forced_api_video(video_bytes)
                if self._generation.is_generation_cancelled():
                    output_path.unlink(missing_ok=True)
                    raise GenerationCancelledError()

                self._generation.update_progress("complete", 100, None, None)
                self._generation.complete_generation(str(output_path))
                self._complete_project_job(req, generation_id, output_path)
                return _complete_video_response(output_path)
            except HTTPError as e:
                self._generation.fail_generation(e.detail)
                self._fail_project_job(req, generation_id, status="failed", error=e.detail)
                raise
            except LTXAPIClientError as e:
                mapped_error = self._map_ltx_api_generation_error(e)
                self._generation.fail_generation(mapped_error.detail)
                self._fail_project_job(
                    req, generation_id, status="failed", error=mapped_error.detail
                )
                raise mapped_error from e
            except Exception as e:
                self._generation.fail_generation(str(e))
                if is_cancel_exception(e):
                    self._fail_project_job(req, generation_id, status="cancelled")
                    logger.info("Generation cancelled by user")
                    return GenerateVideoCancelledResponse(status="cancelled")
                self._fail_project_job(req, generation_id, status="failed", error=str(e))
                raise HTTPError(500, str(e)) from e

    def _write_forced_api_video(self, video_bytes: bytes) -> Path:
        output_path = self._make_output_path()
        output_path.write_bytes(video_bytes)
        return output_path

    @staticmethod
    def _map_ltx_api_generation_error(exc: LTXAPIClientError) -> HTTPError:
        if exc.status_code == 402 and exc.provider_error_type == "insufficient_funds_error":
            return HTTPError(402, _LTX_INSUFFICIENT_FUNDS_MESSAGE, code="LTX_INSUFFICIENT_FUNDS")
        return HTTPError(exc.status_code, exc.detail)
