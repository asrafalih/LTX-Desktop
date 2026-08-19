import { useEffect, useRef } from 'react'
import { useProjects } from '../contexts/ProjectContext'
import { ApiClient } from '../lib/api-client'
import { addVisualAssetToProject } from '../lib/asset-copy'
import { logger } from '../lib/logger'
import type { components } from '../generated/backend-openapi'

type ProjectIngestJob = components['schemas']['ProjectIngestJob']

const POLL_MS = 2500

export function useProjectIngestWatcher(): void {
  const { projectIds, getProject, createProject, addAsset } = useProjects()
  const projectIdsRef = useRef(projectIds)
  const getProjectRef = useRef(getProject)
  const createProjectRef = useRef(createProject)
  const addAssetRef = useRef(addAsset)
  const drainingRef = useRef(false)
  const createdByNameRef = useRef(new Map<string, string>())

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

    const importJob = async (job: ProjectIngestJob): Promise<boolean> => {
      const projectId = findOrCreateProjectId(job.projectName)
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
          const imported = await importJob(job)
          if (!imported) continue
          const deleted = await ApiClient.deleteProjectIngest(job.id)
          if (!deleted.ok) {
            logger.error(`Project ingest: imported job ${job.id} but failed to delete the queue entry`)
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
