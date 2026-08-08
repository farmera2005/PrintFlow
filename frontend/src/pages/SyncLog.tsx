import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { AuditEntry, SyncLogEntry } from '../lib/types'
import { Alert, Badge, Button, Card, EmptyState, Spinner, cx } from '../components/ui'

export default function SyncLog() {
  const [tab, setTab] = useState<'sync' | 'audit'>('sync')
  const [entries, setEntries] = useState<SyncLogEntry[] | null>(null)
  const [audit, setAudit] = useState<AuditEntry[] | null>(null)
  const [onlyFailures, setOnlyFailures] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      if (tab === 'sync') {
        const data = await api.get<{ entries: SyncLogEntry[] }>(
          `/api/sync-log?only_failures=${onlyFailures}`,
        )
        setEntries(data.entries)
      } else {
        const data = await api.get<{ entries: AuditEntry[] }>('/api/audit-log')
        setAudit(data.entries)
      }
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [tab, onlyFailures])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Activity</h1>
          <div className="flex gap-1">
            {(['sync', 'audit'] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setTab(option)}
                className={cx(
                  'rounded-md px-2.5 py-1 text-xs font-medium',
                  tab === option
                    ? 'bg-ink-900 text-white'
                    : 'bg-white text-ink-600 ring-1 ring-ink-300',
                )}
              >
                {option === 'sync' ? 'Sync log' : 'Audit trail'}
              </button>
            ))}
          </div>
          {tab === 'sync' ? (
            <label className="flex items-center gap-1.5 text-xs text-ink-600">
              <input
                type="checkbox"
                checked={onlyFailures}
                onChange={(e) => setOnlyFailures(e.target.checked)}
              />
              Failures only
            </label>
          ) : null}
          <Button size="sm" className="ml-auto" onClick={load}>
            Refresh
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {tab === 'sync' ? (
          !entries ? (
            <div className="flex justify-center py-10">
              <Spinner className="h-6 w-6" />
            </div>
          ) : entries.length === 0 ? (
            <EmptyState
              title="No background runs recorded yet"
              description="Polls start once setup is complete and the integrations are connected."
            />
          ) : (
            <Card className="divide-y divide-ink-200">
              {entries.map((entry) => (
                <div key={entry.id} className="flex flex-wrap items-center gap-2 px-4 py-2.5">
                  <Badge
                    className={
                      entry.ok === null
                        ? 'bg-slate-100 text-slate-700 ring-slate-300'
                        : entry.ok
                          ? 'bg-emerald-100 text-emerald-800 ring-emerald-300'
                          : 'bg-red-100 text-red-800 ring-red-300'
                    }
                  >
                    {entry.ok === null ? 'running' : entry.ok ? 'ok' : 'failed'}
                  </Badge>
                  <span className="font-mono text-xs text-ink-700">{entry.job}</span>
                  <span className="text-xs text-ink-400">
                    {formatDateTime(entry.started_at)}
                  </span>
                  <span className="min-w-0 flex-1 break-words text-sm text-ink-600">
                    {entry.detail}
                  </span>
                </div>
              ))}
            </Card>
          )
        ) : !audit ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : audit.length === 0 ? (
          <EmptyState
            title="No manual overrides yet"
            description="Every manual state change, product link and label purchase is recorded here."
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {audit.map((entry) => (
              <div key={entry.id} className="flex flex-wrap items-center gap-2 px-4 py-2.5">
                <Badge>{entry.action}</Badge>
                <span className="text-xs text-ink-500">{entry.entity_type}</span>
                <span className="text-xs text-ink-400">
                  {formatDateTime(entry.created_at)}
                </span>
                <span className="text-xs text-ink-500">{entry.actor}</span>
                {entry.detail && Object.keys(entry.detail).length ? (
                  <span className="min-w-0 flex-1 break-words font-mono text-xs text-ink-600">
                    {JSON.stringify(entry.detail)}
                  </span>
                ) : null}
              </div>
            ))}
          </Card>
        )}
      </div>
    </div>
  )
}
