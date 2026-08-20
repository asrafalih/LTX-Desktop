import { useSyncExternalStore } from 'react'
import type { components } from '../generated/backend-openapi'
import { ApiClient } from './api-client'

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
  // Keep a stable FIFO even if a caller forgets to sort.
  jobs = [...next].sort((a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id))
  emit()
}

export async function refreshProjectIngestJobs(): Promise<void> {
  const listed = await ApiClient.listProjectIngest()
  if (listed.ok) setProjectIngestJobs(listed.data.jobs)
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

const JOB_STARTED_STORAGE_KEY = 'ltx-ingest-job-started-ms'

function readJobStartedMap(): Record<string, number> {
  try {
    const raw = localStorage.getItem(JOB_STARTED_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, number>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeJobStartedMap(map: Record<string, number>): void {
  localStorage.setItem(JOB_STARTED_STORAGE_KEY, JSON.stringify(map))
}

/** First time we observe this job actually running — used for "Generated in Xs" on the asset. */
export function markIngestJobRunStarted(jobId: string): void {
  const map = readJobStartedMap()
  if (map[jobId] != null) return
  map[jobId] = Date.now()
  writeJobStartedMap(map)
}

/** Seconds from run start (or enqueue fallback) to now; clears the start mark. */
export function takeIngestJobDurationSec(jobId: string, createdAtSec?: number): number | undefined {
  const map = readJobStartedMap()
  const startedMs = map[jobId]
  if (startedMs != null) {
    delete map[jobId]
    writeJobStartedMap(map)
    return Math.max(1, Math.round((Date.now() - startedMs) / 1000))
  }
  if (createdAtSec != null && Number.isFinite(createdAtSec)) {
    return Math.max(1, Math.round(Date.now() / 1000 - createdAtSec))
  }
  return undefined
}

export function formatGenerationDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rem = seconds % 60
  return rem > 0 ? `${minutes}m ${rem}s` : `${minutes}m`
}
