import { useSyncExternalStore } from 'react'
import type { components } from '../generated/backend-openapi'

export type ProjectIngestJob = components['schemas']['ProjectIngestJob']

let jobs: ProjectIngestJob[] = []
const listeners = new Set<() => void>()

// Survives React remounts — ingest + recovery must not both addAsset the same completion.
const claimedImportKeys = new Set<string>()

function emit(): void {
  for (const listener of listeners) listener()
}

export function getProjectIngestJobs(): ProjectIngestJob[] {
  return jobs
}

export function setProjectIngestJobs(next: ProjectIngestJob[]): void {
  jobs = next
  emit()
}

export function subscribeProjectIngestJobs(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function useProjectIngestJobs(): ProjectIngestJob[] {
  return useSyncExternalStore(subscribeProjectIngestJobs, getProjectIngestJobs, getProjectIngestJobs)
}

/** Returns true if this caller won the claim (first wins). Pass all related keys so
 * ingest (`generation:id`) and GenSpace (`result:path`) cannot both succeed. */
export function claimGenerationImport(keyOrKeys: string | readonly string[]): boolean {
  const keys = typeof keyOrKeys === 'string' ? [keyOrKeys] : [...keyOrKeys]
  if (keys.some(key => claimedImportKeys.has(key))) return false
  for (const key of keys) claimedImportKeys.add(key)
  return true
}

export function releaseGenerationImport(keyOrKeys: string | readonly string[]): void {
  const keys = typeof keyOrKeys === 'string' ? [keyOrKeys] : keyOrKeys
  for (const key of keys) claimedImportKeys.delete(key)
}

export function isIngestJobKnown(jobId: string | null | undefined): boolean {
  if (!jobId) return false
  return jobs.some(job => job.id === jobId)
}
