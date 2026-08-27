export type ProjectGenerationCardStatus = 'generating' | 'queued' | null

export type ProjectGenerationStatusJob = {
  id: string
  projectName: string
  video_path: string
}

export type ProjectGenerationProgressSnapshot = {
  status: string
  id: string | null
} | null

function matchesProjectName(jobName: string, projectName: string): boolean {
  return jobName.trim().toLowerCase() === projectName.trim().toLowerCase()
}

/**
 * Home project cards must mirror GenSpace: only the project whose video is actually
 * running shows "Generating". Pending ingest jobs that are waiting on the global slot
 * show "Queued".
 */
export function resolveProjectGenerationStatus(args: {
  projectId: string
  projectName: string
  ingestJobs: readonly ProjectGenerationStatusJob[]
  progress: ProjectGenerationProgressSnapshot
  recoveryProjectId: string | null
  recoveryGenerationId?: string | null
}): ProjectGenerationCardStatus {
  const pendingForProject = args.ingestJobs.filter(
    job => !job.video_path && matchesProjectName(job.projectName, args.projectName),
  )

  const runningId =
    args.progress?.status === 'running' ? args.progress.id : null

  if (runningId != null && pendingForProject.some(job => job.id === runningId)) {
    return 'generating'
  }

  // Marker already confirmed this project's ingest job is the live generation — cover the
  // gap before the shared progress poll delivers status=running.
  const recoveryGenerationId = args.recoveryGenerationId ?? null
  if (
    recoveryGenerationId != null
    && args.recoveryProjectId === args.projectId
    && pendingForProject.some(job => job.id === recoveryGenerationId)
  ) {
    return 'generating'
  }

  // Non-ingest generations (no projectName queue entry) still write a recovery marker.
  if (
    runningId != null
    && args.recoveryProjectId === args.projectId
  ) {
    const owningIngest = args.ingestJobs.find(job => job.id === runningId)
    if (!owningIngest || matchesProjectName(owningIngest.projectName, args.projectName)) {
      return 'generating'
    }
  }

  if (pendingForProject.length > 0) {
    return 'queued'
  }

  return null
}
