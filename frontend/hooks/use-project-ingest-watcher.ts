import { useEffect, useRef } from 'react'
import type { GenerationSettings } from '../components/SettingsPanel'
import { useProjects } from '../contexts/ProjectContext'
import { ApiClient } from '../lib/api-client'
import { addVisualAssetToProject } from '../lib/asset-copy'
import {
  ingestRecoveryIdentity,
  isActiveGenerationOwner,
  setIngestOwnedGeneration,
} from '../lib/generation-recovery'
import { logger } from '../lib/logger'
import type { VideoGenerationPipeline } from '../lib/video-generation-model-specs'
import type { components } from '../generated/backend-openapi'
import { GENERATION_RECOVERY_KEY, type GenerationRecoveryContext } from './use-generation'

type ProjectIngestJob = components['schemas']['ProjectIngestJob']

const POLL_MS = 2500

function settingsFromJob(job: ProjectIngestJob): GenerationSettings {
  return {
    model: job.model as VideoGenerationPipeline,
    duration: job.duration ?? null,
    videoResolution: job.resolution,
    fps: job.fps,
    audio: job.audio,
    cameraMotion: 'none',
    imageResolution: job.resolution,
    imageAspectRatio: '16:9',
    imageSteps: 4,
  }
}

export function useProjectIngestWatcher(): void {
  const { projectIds, getProject, createProject, addAsset } = useProjects()
  const projectIdsRef = useRef(projectIds)
  const getProjectRef = useRef(getProject)
  const createProjectRef = useRef(createProject)
  const addAssetRef = useRef(addAsset)
  const drainingRef = useRef(false)
  const createdByNameRef = useRef(new Map<string, string>())
  const markerWrittenForJobRef = useRef(new Set<string>())

  projectIdsRef.current = projectIds
  getProjectRef.current = getProject
  createProjectRef.current = createProject
  addAssetRef.current = addAsset

  useEffect(() => {
    let cancelled = false

    const findOrCreateProjectId = (name: string): string => {
      const needle = name.trim().toLowerCase()
      const alreadyCreated = createdByNameRef.current.get(needle)
      if (alreadyCreated) return alreadyCreated
      for (const id of projectIdsRef.current) {
        const project = getProjectRef.current(id)
        if (project && project.name.trim().toLowerCase() === needle) {
          return id
        }
      }
      const created = createProjectRef.current(name.trim())
      createdByNameRef.current.set(needle, created.id)
      return created.id
    }

    const attachRunningJob = async (job: ProjectIngestJob): Promise<void> => {
      const projectId = findOrCreateProjectId(job.projectName)

      const before = await ApiClient.getGenerationProgress()
      if (!before.ok) {
        logger.error(`Project ingest: skipping recovery marker, failed to fetch baseline (${before.error})`)
        return
      }
      if (cancelled) return

      const identity = ingestRecoveryIdentity(job.id, before.data.id ?? null)
      const existing = localStorage.getItem(GENERATION_RECOVERY_KEY)

      if (markerWrittenForJobRef.current.has(job.id) && existing) {
        try {
          const ctx = JSON.parse(existing) as GenerationRecoveryContext
          if (ctx.generationId == null && identity.generationId != null) {
            localStorage.setItem(GENERATION_RECOVERY_KEY, JSON.stringify({ ...ctx, ...identity }))
          }
        } catch {
          // leave a corrupt marker for the recovery watcher to drop
        }
        return
      }

      if (existing != null) return

      const ctx: GenerationRecoveryContext = {
        projectId,
        prompt: job.prompt,
        settings: settingsFromJob(job),
        model: job.model,
        canCancel: true,
        ...identity,
      }
      localStorage.setItem(GENERATION_RECOVERY_KEY, JSON.stringify(ctx))
      markerWrittenForJobRef.current.add(job.id)
      setIngestOwnedGeneration(job.id, true)
    }

    const importJob = async (job: ProjectIngestJob): Promise<boolean> => {
      const projectId = findOrCreateProjectId(job.projectName)
      if (isActiveGenerationOwner(projectId)) {
        return true
      }
      const copied = await addVisualAssetToProject(job.video_path, projectId, 'video')
      if (!copied) {
        logger.error(`Project ingest: could not copy video for job ${job.id}`)
        return false
      }
      const duration = job.duration ?? undefined
      addAssetRef.current(projectId, {
        type: 'video',
        path: copied.path,
        bigThumbnailPath: copied.bigThumbnailPath,
        smallThumbnailPath: copied.smallThumbnailPath,
        width: copied.width,
        height: copied.height,
        prompt: job.prompt,
        resolution: job.resolution,
        duration,
        generationParams: {
          mode: 'text-to-video',
          prompt: job.prompt,
          model: job.model,
          duration: job.duration ?? null,
          resolution: job.resolution,
          fps: job.fps,
          audio: job.audio,
          cameraMotion: 'none',
        },
        takes: [{
          path: copied.path,
          bigThumbnailPath: copied.bigThumbnailPath,
          smallThumbnailPath: copied.smallThumbnailPath,
          width: copied.width,
          height: copied.height,
          createdAt: Date.now(),
        }],
        activeTakeIndex: 0,
      })
      return true
    }

    const drain = async () => {
      if (cancelled || drainingRef.current) return
      drainingRef.current = true
      try {
        const listed = await ApiClient.listProjectIngest()
        if (!listed.ok || cancelled) return
        for (const job of listed.data.jobs) {
          if (cancelled) return
          if (!job.video_path) {
            await attachRunningJob(job)
            continue
          }
          const projectId = findOrCreateProjectId(job.projectName)
          const imported = await importJob(job)
          if (!imported) continue
          const deleted = await ApiClient.deleteProjectIngest(job.id)
          if (!deleted.ok) {
            logger.error(`Project ingest: imported job ${job.id} but failed to delete the queue entry`)
            continue
          }
          setIngestOwnedGeneration(job.id, false)
          markerWrittenForJobRef.current.delete(job.id)
          if (isActiveGenerationOwner(projectId)) continue
          const saved = localStorage.getItem(GENERATION_RECOVERY_KEY)
          if (saved) {
            try {
              const ctx = JSON.parse(saved) as GenerationRecoveryContext
              if (ctx.generationId === job.id) {
                localStorage.removeItem(GENERATION_RECOVERY_KEY)
              }
            } catch {
              // leave a corrupt marker for the recovery watcher to drop
            }
          }
        }
      } finally {
        drainingRef.current = false
      }
    }

    void drain()
    const timer = window.setInterval(() => { void drain() }, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])
}
