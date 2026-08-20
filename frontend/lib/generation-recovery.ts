import { ApiClient } from './api-client'
import { GENERATION_RECOVERY_KEY, type GenerationRecoveryContext } from '../hooks/use-generation'
import { builtinRecoveryImporters } from './generation-recovery-importers'
import {
  claimGenerationImport,
  releaseGenerationImport,
  getProjectIngestJobs,
  isIngestJobKnown,
} from './project-ingest-jobs'
import type { Asset } from '../types/project-model'

// Keyed the same way the recovery marker already is: undefined means the default "video" case
// (t2v/i2v/a2v/ic-lora/retake/extend all recover as a standalone video asset today — see
// GenSpace's own mount-recovery effect for why those four share one fallback).
export type RecoveryGenType = NonNullable<GenerationRecoveryContext['genType']> | 'video'

export interface RecoveryImporterApi {
  addAsset: (projectId: string, asset: Omit<Asset, 'id' | 'createdAt'>) => unknown
  modelsDir: string
}

export type RecoveryImporter = (
  ctx: GenerationRecoveryContext,
  result: string | string[],
  api: RecoveryImporterApi,
) => Promise<void> | void

// A marker written by an older build (before baselineId existed) would parse fine as JSON but
// have `baselineId === undefined` — and since the progress endpoint's `id` is always `string |
// null`, never `undefined`, an identity check that just compares `observedId === ctx.baselineId`
// would treat that `undefined` as "already different from whatever's live right now" and trust
// it immediately, on the very first tick, with zero confirmation. Callers must check this before
// trusting anything else in the marker.
export function hasValidBaselineId(ctx: GenerationRecoveryContext): boolean {
  return typeof ctx.baselineId === 'string' || ctx.baselineId === null
}

export function readGenerationRecoveryContext(): GenerationRecoveryContext | null {
  const saved = localStorage.getItem(GENERATION_RECOVERY_KEY)
  if (!saved) return null
  try {
    const ctx = JSON.parse(saved) as GenerationRecoveryContext
    return hasValidBaselineId(ctx) ? ctx : null
  } catch {
    return null
  }
}

// The ingest watcher writes this marker after generate has already begun. If we stored the
// live progress id as baselineId, GenSpace's mount recovery treats id === baselineId as
// "not started yet" and never shows the generating tile.
export function ingestRecoveryIdentity(
  jobId: string,
  observedProgressId: string | null,
): Pick<GenerationRecoveryContext, 'baselineId' | 'generationId'> {
  if (observedProgressId === jobId) {
    return { baselineId: null, generationId: jobId }
  }
  return { baselineId: observedProgressId }
}

// The project whose GenSpace instance is currently mounted and already handling its own
// generation lifecycle live (polling, completion effects). The background watcher backs off
// entirely for it, so two independent pollers never race to import the same completion twice.
let activeOwnerProjectId: string | null = null

export function setActiveGenerationOwner(projectId: string | null): void {
  activeOwnerProjectId = projectId
}

export function isActiveGenerationOwner(projectId: string): boolean {
  return activeOwnerProjectId === projectId
}

// Curl ingest writes a recovery marker for Stop/progress, then copies the file itself when
// video_path is filled. While that job is claimed, background recovery must not also import.
const ingestOwnedGenerationIds = new Set<string>()

export function setIngestOwnedGeneration(generationId: string, owned: boolean): void {
  if (owned) ingestOwnedGenerationIds.add(generationId)
  else ingestOwnedGenerationIds.delete(generationId)
}

// One check: is there a recovery marker, is anything registered to handle it, and if the
// generation it points at has finished, persist the result into its project. Takes an
// already-fetched progress poll (shared with useGlobalGenerationLock via
// subscribeToGenerationProgress) instead of fetching its own, so mounting both doesn't double
// the network chatter.
export async function checkAndConsumeRecovery(
  progress: Awaited<ReturnType<typeof ApiClient.getGenerationProgress>>,
  api: RecoveryImporterApi,
): Promise<void> {
  const saved = localStorage.getItem(GENERATION_RECOVERY_KEY)
  if (!saved) return

  let ctx: GenerationRecoveryContext
  try {
    ctx = JSON.parse(saved) as GenerationRecoveryContext
  } catch {
    localStorage.removeItem(GENERATION_RECOVERY_KEY)
    return
  }
  if (!hasValidBaselineId(ctx)) {
    localStorage.removeItem(GENERATION_RECOVERY_KEY)
    return
  }

  // That project's own GenSpace is mounted and already polling/importing this live.
  if (ctx.projectId === activeOwnerProjectId) return

  // A generation kind with no importer (e.g. 'enhance': there's nowhere to put a rewritten
  // prompt without an open editor) is left alone here — only that project's own mount-recovery
  // effect can handle it.
  const importer = builtinRecoveryImporters[ctx.genType ?? 'video']
  if (!importer) return

  if (!progress.ok) return
  const observedId = progress.data.id
  const status = progress.data.status

  // Project-ingest owns named generate jobs end-to-end. Never recovery-import those, even
  // after the queue entry is deleted / ownership bit cleared — a sticky Complete poll can
  // otherwise race GenSpace's activeOwner gap (projectName gens never set videoPath).
  const ingestManages = (
    (observedId != null && (
      ingestOwnedGenerationIds.has(observedId)
      || isIngestJobKnown(observedId)
    ))
    || (ctx.generationId != null && (
      ingestOwnedGenerationIds.has(ctx.generationId)
      || isIngestJobKnown(ctx.generationId)
    ))
    // Any still-listed ingest job for this generation id (including video_path filled).
    || getProjectIngestJobs().some(job =>
      job.id === observedId || job.id === ctx.generationId
    )
  )
  if (ingestManages) {
    if (ctx.generationId == null && observedId != null && observedId !== ctx.baselineId) {
      ctx = { ...ctx, generationId: observedId }
      localStorage.setItem(GENERATION_RECOVERY_KEY, JSON.stringify(ctx))
    }
    return
  }

  if (ctx.generationId == null) {
    // Not yet confirmed. Any id different from the baseline captured when this marker was
    // written proves (single global generation slot) our generation has started — regardless of
    // status, even if it's already 'complete' by the time we look (a fast generation can finish
    // between two polls). Until the id actually changes, this endpoint is still reporting
    // whatever predated this marker, which must not be trusted.
    if (observedId === ctx.baselineId) return
    ctx = { ...ctx, generationId: observedId ?? undefined }
    localStorage.setItem(GENERATION_RECOVERY_KEY, JSON.stringify(ctx))
  } else if (observedId !== ctx.generationId) {
    // Already confirmed once; a FURTHER id change means a different generation superseded ours
    // before we ever saw it finish. Nothing left to recover.
    localStorage.removeItem(GENERATION_RECOVERY_KEY)
    return
  }

  if (status === 'running') return // still going — check again next tick

  if (status === 'complete' && progress.data.result != null) {
    const resultPath = typeof progress.data.result === 'string'
      ? progress.data.result
      : progress.data.result[0]
    const importKeys = [
      ...(ctx.generationId ? [`generation:${ctx.generationId}`] : []),
      ...(resultPath ? [`result:${resultPath}`] : []),
    ]
    if (importKeys.length === 0 || !claimGenerationImport(importKeys)) {
      localStorage.removeItem(GENERATION_RECOVERY_KEY)
      return
    }
    try {
      await importer(ctx, progress.data.result, api)
    } catch {
      releaseGenerationImport(importKeys)
      // Leave the marker in place so the next tick (or that project's own mount effect) can
      // retry — a failed copy/import must not silently drop the result.
      return
    }
  }

  localStorage.removeItem(GENERATION_RECOVERY_KEY)
}
