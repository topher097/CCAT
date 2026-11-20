/** Typed job-center helpers (§7.1.9; types from schema.d.ts). */

import { api } from './client'
import type { Schemas } from './client'

export type Job = Schemas['JobView']
export type JobCreate = Schemas['JobCreate']

/** Every job status the worker writes. */
export const JOB_STATUSES = [
  'queued',
  'running',
  'cancel_requested',
  'succeeded',
  'failed',
  'cancelled',
] as const

/** Every enqueueable job type. */
export const JOB_TYPES = [
  'acquire',
  'screen',
  'match',
  'generate',
  'stats',
  'clarify_generate',
  'clarify_export',
  'reconcile',
] as const

export type Progress = {
  phase?: string
  completed?: number
  failed?: number
  total?: number | null
}

/** Read the loosely typed progress snapshot. */
export function progressOf(job: Job): Progress {
  return (job.progress ?? {}) as Progress
}

/** Whether the job is still active (queued/running/cancelling). */
export function isActive(job: Job): boolean {
  return ['queued', 'running', 'cancel_requested'].includes(job.status)
}

/** Partial failure: some work failed while other work completed. */
export function isPartialFailure(job: Job): boolean {
  const progress = progressOf(job)
  return Number(progress.failed ?? 0) > 0 && Number(progress.completed ?? 0) > 0
}

/** Human label for a job status. */
export function jobStatusLabel(status: string): string {
  return status.replaceAll('_', ' ')
}

/** GET the season's jobs (newest first). */
export function listJobs(season: string): Promise<Job[]> {
  return api.get<Job[]>(
    `/api/v1/seasons/${encodeURIComponent(season)}/jobs`,
  )
}

/** GET one job plus its event tail. */
export function getJob(jobId: number | string): Promise<Job> {
  return api.get<Job>(`/api/v1/jobs/${encodeURIComponent(String(jobId))}`)
}

/** Enqueue one job (202 + queued view). */
export function enqueueJob(
  season: string,
  jobType: JobCreate['job_type'],
  options: Record<string, unknown>,
): Promise<Job> {
  return api.post<Job>(
    `/api/v1/seasons/${encodeURIComponent(season)}/jobs`,
    { job_type: jobType, options },
  )
}

/** Request cancellation (coordinator/admin). */
export function cancelJob(jobId: number | string): Promise<Job> {
  return api.post<Job>(
    `/api/v1/jobs/${encodeURIComponent(String(jobId))}/cancel`,
  )
}

/** Same-origin SSE URL for one job's progress stream. */
export function jobEventsStreamUrl(jobId: number | string): string {
  return `/api/v1/jobs/${encodeURIComponent(String(jobId))}/events/stream`
}
