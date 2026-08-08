import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { JOB_STATUS_CLASSES, formatDateTime } from '../lib/format'
import type { QueueJob } from '../lib/types'
import { Alert, Badge, Button, Card, EmptyState, Spinner, cx } from '../components/ui'

const FILTERS = ['open', 'all', 'failed', 'done'] as const
type Filter = (typeof FILTERS)[number]

export default function PrintQueue() {
  const [jobs, setJobs] = useState<QueueJob[] | null>(null)
  const [filter, setFilter] = useState<Filter>('open')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ jobs: QueueJob[] }>('/api/print-jobs')
      setJobs(data.jobs)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 30_000)
    return () => clearInterval(timer)
  }, [load])

  const act = async (jobId: string, action: 'requeue' | 'cancel') => {
    setBusy(jobId)
    try {
      await api.post(`/api/print-jobs/${jobId}/${action}`)
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const dispatchAll = async () => {
    setBusy('dispatch')
    try {
      await api.post('/api/print-jobs/dispatch')
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const visible = (jobs ?? []).filter((job) => {
    if (filter === 'all') return true
    if (filter === 'failed') return job.status === 'failed' || job.status === 'cancelled'
    if (filter === 'done') return job.status === 'done'
    return ['pending', 'queued', 'printing'].includes(job.status)
  })

  const pendingCount = (jobs ?? []).filter((job) => job.status === 'pending').length

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Print Queue</h1>
          <div className="flex gap-1">
            {FILTERS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setFilter(option)}
                className={cx(
                  'rounded-md px-2.5 py-1 text-xs font-medium capitalize',
                  filter === option
                    ? 'bg-ink-900 text-white'
                    : 'bg-white text-ink-600 ring-1 ring-ink-300',
                )}
              >
                {option}
              </button>
            ))}
          </div>
          {pendingCount > 0 ? (
            <Button
              variant="primary"
              className="ml-auto"
              onClick={dispatchAll}
              disabled={busy === 'dispatch'}
            >
              {busy === 'dispatch'
                ? 'Sending…'
                : `Send ${pendingCount} pending to Bambuddy`}
            </Button>
          ) : null}
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!jobs ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            title="Nothing in the queue"
            description="Print jobs are created automatically when an order needs more units than QuickBooks has on hand."
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {visible.map((job) => (
              <div key={job.id} className="flex flex-wrap items-center gap-2 px-4 py-3">
                <Badge className={JOB_STATUS_CLASSES[job.status]}>{job.status}</Badge>
                <span className="font-mono text-sm text-ink-900">
                  #{job.order_number ?? '—'}
                </span>
                <span className="font-mono text-sm text-ink-700">{job.sku ?? '—'}</span>
                <span className="min-w-0 flex-1 truncate text-sm text-ink-500">
                  {job.product_name}
                </span>
                <span className="text-xs text-ink-500">
                  plate {job.plate_number ?? '—'} · {job.units_expected} units
                </span>
                {job.bambuddy_queue_id ? (
                  <span className="text-xs text-ink-400">queue #{job.bambuddy_queue_id}</span>
                ) : null}
                <span className="text-xs text-ink-400">
                  {formatDateTime(job.completed_at ?? job.queued_at ?? job.created_at)}
                </span>
                {job.error ? (
                  <p className="w-full text-xs text-red-700">{job.error}</p>
                ) : null}
                <div className="flex gap-1.5">
                  {['failed', 'cancelled'].includes(job.status) ? (
                    <Button
                      size="sm"
                      onClick={() => act(job.id, 'requeue')}
                      disabled={busy === job.id}
                    >
                      Re-queue
                    </Button>
                  ) : null}
                  {['pending', 'queued', 'printing'].includes(job.status) ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => act(job.id, 'cancel')}
                      disabled={busy === job.id}
                    >
                      Cancel
                    </Button>
                  ) : null}
                </div>
              </div>
            ))}
          </Card>
        )}
      </div>
    </div>
  )
}
