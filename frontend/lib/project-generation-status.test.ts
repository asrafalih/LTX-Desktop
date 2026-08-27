import { describe, expect, it } from 'vitest'
import { resolveProjectGenerationStatus } from './project-generation-status'

describe('resolveProjectGenerationStatus', () => {
  const jobs = [
    { id: 'job-a', projectName: 'Alpha', video_path: '' },
    { id: 'job-b', projectName: 'Beta', video_path: '' },
  ] as const

  it('shows generating only for the project whose ingest job is running', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-a',
      projectName: 'Alpha',
      ingestJobs: jobs,
      progress: { status: 'running', id: 'job-a' },
      recoveryProjectId: 'proj-a',
    })).toBe('generating')

    expect(resolveProjectGenerationStatus({
      projectId: 'proj-b',
      projectName: 'Beta',
      ingestJobs: jobs,
      progress: { status: 'running', id: 'job-a' },
      recoveryProjectId: 'proj-a',
    })).toBe('queued')
  })

  it('shows queued when the project only has pending ingest jobs', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-b',
      projectName: 'Beta',
      ingestJobs: jobs,
      progress: { status: 'idle', id: null },
      recoveryProjectId: 'proj-b',
    })).toBe('queued')
  })

  it('does not treat a recovery marker alone as generating', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-b',
      projectName: 'Beta',
      ingestJobs: [{ id: 'job-b', projectName: 'Beta', video_path: '' }],
      progress: null,
      recoveryProjectId: 'proj-b',
    })).toBe('queued')
  })

  it('treats a confirmed recovery generationId as generating before progress poll', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-a',
      projectName: 'Alpha',
      ingestJobs: jobs,
      progress: null,
      recoveryProjectId: 'proj-a',
      recoveryGenerationId: 'job-a',
    })).toBe('generating')

    expect(resolveProjectGenerationStatus({
      projectId: 'proj-b',
      projectName: 'Beta',
      ingestJobs: jobs,
      progress: null,
      recoveryProjectId: 'proj-a',
      recoveryGenerationId: 'job-a',
    })).toBe('queued')
  })

  it('shows generating for non-ingest recovery while progress is running', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-a',
      projectName: 'Alpha',
      ingestJobs: [],
      progress: { status: 'running', id: 'gen-1' },
      recoveryProjectId: 'proj-a',
    })).toBe('generating')
  })

  it('returns null when the project has no pending work', () => {
    expect(resolveProjectGenerationStatus({
      projectId: 'proj-c',
      projectName: 'Charlie',
      ingestJobs: jobs,
      progress: { status: 'running', id: 'job-a' },
      recoveryProjectId: 'proj-a',
    })).toBeNull()
  })
})
