import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { PROVIDER_LABELS, formatDateTime } from '../lib/format'
import type { IntegrationStatus } from '../lib/types'
import { PANELS } from '../components/IntegrationPanels'
import SecurityPanel from '../components/SecurityPanel'
import { Alert, Badge, Button, Card, Field, inputClass } from '../components/ui'

interface SettingsResponse {
  setup_complete: boolean
  poll_intervals: Record<string, number>
  public_base_url: string | null
  integrations: IntegrationStatus[]
  jobs: { id: string; next_run_at: string | null; interval: string }[]
}

export default function Settings({ onChange }: { onChange: () => Promise<void> }) {
  const [data, setData] = useState<SettingsResponse | null>(null)
  const [redirectUris, setRedirectUris] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [banner, setBanner] = useState<{ tone: 'success' | 'error'; text: string } | null>(
    null,
  )

  const load = useCallback(async () => {
    try {
      const [settings, integrations] = await Promise.all([
        api.get<SettingsResponse>('/api/settings'),
        api.get<{ redirect_uris: Record<string, string> }>('/api/integrations'),
      ])
      setData(settings)
      setRedirectUris(integrations.redirect_uris)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // OAuth callbacks redirect back here with the outcome in the hash query.
  useEffect(() => {
    const hash = window.location.hash
    const queryStart = hash.indexOf('?')
    if (queryStart === -1) return
    const params = new URLSearchParams(hash.slice(queryStart + 1))
    const provider = params.get('provider')
    const result = params.get('result')
    if (!provider || !result) return
    setBanner(
      result === 'connected'
        ? { tone: 'success', text: `${PROVIDER_LABELS[provider]} connected.` }
        : {
            tone: 'error',
            text: `${PROVIDER_LABELS[provider]} failed: ${params.get('message') ?? 'unknown error'}`,
          },
    )
    window.history.replaceState(null, '', window.location.pathname + '#/settings')
    load()
  }, [load])

  if (!data) {
    return (
      <div className="p-6">
        {error ? <Alert tone="error">{error}</Alert> : <p className="text-sm">Loading…</p>}
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-3xl space-y-4">
        <h1 className="text-lg font-semibold text-ink-900">Settings</h1>
        {banner ? (
          <Alert tone={banner.tone === 'success' ? 'success' : 'error'}>{banner.text}</Alert>
        ) : null}
        {error ? <Alert tone="error">{error}</Alert> : null}

        <Card className="p-4">
          <h2 className="mb-3 text-sm font-semibold text-ink-900">Access &amp; security</h2>
          <SecurityPanel compact onChanged={load} />
        </Card>

        {data.integrations.map((integration) => {
          const Panel = PANELS[integration.provider]
          return (
            <Card key={integration.provider} className="p-4">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-semibold text-ink-900">
                  {PROVIDER_LABELS[integration.provider]}
                </h2>
                {integration.connected ? null : <Badge>not connected</Badge>}
                {integration.last_error ? (
                  <Badge className="bg-red-100 text-red-800 ring-red-300">
                    last error {formatDateTime(integration.last_error_at)}
                  </Badge>
                ) : null}
              </div>
              {integration.last_error ? (
                <div className="mb-3">
                  <Alert tone="warning">{integration.last_error}</Alert>
                </div>
              ) : null}
              <Panel
                status={integration}
                redirectUri={redirectUris[integration.provider]}
                onChange={async () => {
                  await load()
                  await onChange()
                }}
              />
            </Card>
          )
        })}

        <IntervalsCard data={data} onSaved={load} />
        <PasswordCard />

        <Card className="p-4">
          <h2 className="text-sm font-semibold text-ink-900">Background jobs</h2>
          <ul className="mt-2 space-y-1 text-sm text-ink-600">
            {data.jobs.length === 0 ? (
              <li className="text-ink-500">Scheduler idle — finish setup to start polling.</li>
            ) : (
              data.jobs.map((job) => (
                <li key={job.id} className="flex flex-wrap gap-2">
                  <span className="font-mono text-xs text-ink-700">{job.id}</span>
                  <span className="text-xs text-ink-400">
                    next {formatDateTime(job.next_run_at)}
                  </span>
                </li>
              ))
            )}
          </ul>
          <div className="mt-3">
            <Button
              size="sm"
              onClick={async () => {
                await api.post('/api/setup/reopen')
                await onChange()
              }}
            >
              Re-open setup wizard
            </Button>
          </div>
        </Card>
      </div>
    </div>
  )
}

function IntervalsCard({
  data,
  onSaved,
}: {
  data: SettingsResponse
  onSaved: () => Promise<void>
}) {
  const [values, setValues] = useState(data.poll_intervals)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setError(null)
    try {
      await api.post('/api/setup/intervals', values)
      setSaved(true)
      await onSaved()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const rows: [string, string][] = [
    ['etsy_minutes', 'Etsy receipt poll'],
    ['bambuddy_minutes', 'Bambuddy status reconcile'],
    ['shipstation_minutes', 'ShipStation order match'],
  ]

  return (
    <Card className="p-4">
      <h2 className="text-sm font-semibold text-ink-900">Poll intervals</h2>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        {rows.map(([key, label]) => (
          <Field key={key} label={`${label} (min)`}>
            <input
              className={inputClass}
              type="number"
              min={1}
              max={240}
              value={values[key] ?? ''}
              onChange={(e) => setValues({ ...values, [key]: Number(e.target.value) })}
            />
          </Field>
        ))}
      </div>
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {saved ? (
        <div className="mt-3">
          <Alert tone="success">Saved — the scheduler picked up the new intervals.</Alert>
        </div>
      ) : null}
      <div className="mt-3">
        <Button variant="primary" size="sm" onClick={save}>
          Save
        </Button>
      </div>
    </Card>
  )
}

function PasswordCard() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const save = async () => {
    setError(null)
    try {
      await api.post('/api/auth/password', {
        current_password: current,
        new_password: next,
      })
      setDone(true)
      setCurrent('')
      setNext('')
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  return (
    <Card className="p-4">
      <h2 className="text-sm font-semibold text-ink-900">Admin password</h2>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Field label="Current password">
          <input
            className={inputClass}
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </Field>
        <Field label="New password" hint="At least 8 characters.">
          <input
            className={inputClass}
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </Field>
      </div>
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {done ? (
        <div className="mt-3">
          <Alert tone="success">Password changed.</Alert>
        </div>
      ) : null}
      <div className="mt-3">
        <Button size="sm" onClick={save} disabled={!current || next.length < 8}>
          Change password
        </Button>
      </div>
    </Card>
  )
}
