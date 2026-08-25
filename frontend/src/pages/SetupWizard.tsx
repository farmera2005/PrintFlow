import { useEffect, useMemo, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { RestorePanel } from '../components/BackupPanel'
import { PROVIDER_LABELS } from '../lib/format'
import type { SetupStatus } from '../lib/types'
import { PANELS } from '../components/IntegrationPanels'
import SecurityPanel from '../components/SecurityPanel'
import { Alert, Button, Card, Field, cx, inputClass } from '../components/ui'

// Security comes before the OAuth steps: Etsy and Intuit need an https redirect
// URI, and that URI is built from the address configured there.
const STEP_ORDER = [
  'admin',
  'security',
  'etsy',
  'wix',
  'qbo',
  'bambuddy',
  'shipstation',
  'intervals',
] as const
type StepKey = (typeof STEP_ORDER)[number]

const STEP_TITLES: Record<StepKey, string> = {
  admin: 'Create your admin account',
  security: 'Access & security',
  etsy: 'Connect Etsy',
  wix: 'Connect Wix',
  qbo: 'Connect QuickBooks Online',
  bambuddy: 'Connect Bambuddy',
  shipstation: 'Connect ShipStation',
  intervals: 'Poll intervals',
}

const STEP_CHIPS: Record<StepKey, string> = {
  admin: 'Admin',
  security: 'Security',
  etsy: 'Etsy',
  wix: 'Wix',
  qbo: 'QuickBooks',
  bambuddy: 'Bambuddy',
  shipstation: 'ShipStation',
  intervals: 'Intervals',
}

export default function SetupWizard({
  status,
  onChange,
}: {
  status: SetupStatus
  onChange: () => Promise<void>
}) {
  const [step, setStep] = useState<StepKey>(status.admin_exists ? 'etsy' : 'admin')
  const [redirectUris, setRedirectUris] = useState<Record<string, string>>({})
  const [banner, setBanner] = useState<{ tone: 'success' | 'error'; text: string } | null>(
    null,
  )

  useEffect(() => {
    if (!status.admin_exists) return
    api
      .get<{ redirect_uris: Record<string, string> }>('/api/integrations')
      .then((data) => setRedirectUris(data.redirect_uris))
      .catch(() => undefined)
  }, [status.admin_exists])

  // An OAuth round-trip lands back here with the outcome in the query string.
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
    if (STEP_ORDER.includes(provider as StepKey)) setStep(provider as StepKey)
    window.history.replaceState(null, '', window.location.pathname + '#/')
    onChange()
  }, [onChange])

  const completion = useMemo(
    () => Object.fromEntries(status.steps.map((s) => [s.key, s.complete])),
    [status.steps],
  )

  const index = STEP_ORDER.indexOf(step)
  const goNext = () => setStep(STEP_ORDER[Math.min(index + 1, STEP_ORDER.length - 1)])
  const goBack = () => setStep(STEP_ORDER[Math.max(index - 1, 0)])

  return (
    <div className="min-h-full overflow-y-auto bg-ink-100 py-8">
      <div className="mx-auto w-full max-w-2xl px-4">
        <h1 className="text-xl font-semibold text-ink-900">Set up PrintFlow</h1>
        <p className="mt-1 text-sm text-ink-600">
          Everything is configured here — there are no config files to edit. Credentials
          are encrypted before they are stored.
        </p>

        <ol className="mt-5 flex flex-wrap gap-1.5">
          {STEP_ORDER.map((key, i) => (
            <li key={key}>
              <button
                type="button"
                disabled={!status.admin_exists && key !== 'admin'}
                onClick={() => setStep(key)}
                className={cx(
                  'rounded-full px-3 py-1 text-xs font-medium transition disabled:opacity-40',
                  key === step
                    ? 'bg-ink-900 text-white'
                    : completion[key]
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-white text-ink-600 ring-1 ring-ink-300',
                )}
              >
                {completion[key] ? '✓ ' : `${i + 1}. `}
                {STEP_CHIPS[key]}
              </button>
            </li>
          ))}
        </ol>

        {banner ? (
          <div className="mt-4">
            <Alert tone={banner.tone === 'success' ? 'success' : 'error'}>
              {banner.text}
            </Alert>
          </div>
        ) : null}

        <Card className="mt-4 p-5">
          <h2 className="text-base font-semibold text-ink-900">{STEP_TITLES[step]}</h2>
          <div className="mt-4">
            {step === 'admin' ? (
              <AdminStep onDone={onChange} existing={status.admin_exists} />
            ) : step === 'security' ? (
              <SecurityPanel onChanged={onChange} />
            ) : step === 'intervals' ? (
              <IntervalsStep status={status} onDone={onChange} />
            ) : (
              <IntegrationStep
                provider={step}
                status={status}
                redirectUri={redirectUris[step]}
                onChange={onChange}
              />
            )}
          </div>
        </Card>

        <div className="mt-4 flex items-center justify-between">
          <Button onClick={goBack} disabled={index === 0}>
            Back
          </Button>
          <div className="flex gap-2">
            {index < STEP_ORDER.length - 1 ? (
              <Button onClick={goNext} disabled={!status.admin_exists}>
                {completion[step] ? 'Next' : 'Skip for now'}
              </Button>
            ) : null}
            <FinishButton status={status} onDone={onChange} />
          </div>
        </div>
      </div>
    </div>
  )
}

function AdminStep({
  onDone,
  existing,
}: {
  onDone: () => Promise<void>
  existing: boolean
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [restoring, setRestoring] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (existing) {
    return (
      <Alert tone="success">
        Admin account created. Change the password later from Settings.
      </Alert>
    )
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/setup/admin', { username, password })
      await onDone()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  // Restoring instead of starting fresh. Offered here, on the very first
  // step, because somebody rebuilding onto a new machine has no account on it
  // — and making them create one first creates an account the restore then
  // throws away, along with the password they just chose.
  if (restoring) {
    return (
      <div className="space-y-4">
        <Alert tone="info">
          Restoring puts a previous PrintFlow back exactly as it was: its
          orders, its products, its connections, and the accounts you sign in
          with. There is nothing on this install to lose yet.
        </Alert>
        <RestorePanel
          endpoint="/api/setup/restore"
          requireTypedConfirmation={false}
          danger={null}
          onRestored={async () => {
            // Everything below this screen has just been replaced, including
            // whoever is allowed to sign in. Reloading is the honest end of it.
            window.location.reload()
          }}
        />
        <Button variant="ghost" onClick={() => setRestoring(false)}>
          Back — set this install up from scratch instead
        </Button>
      </div>
    )
  }

  return (
    <form className="space-y-4" onSubmit={submit}>
      <Field label="Username">
        <input
          className={inputClass}
          value={username}
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
        />
      </Field>
      <Field label="Password" hint="At least 8 characters.">
        <input
          className={inputClass}
          type="password"
          value={password}
          autoComplete="new-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </Field>
      <Field label="Confirm password">
        <input
          className={inputClass}
          type="password"
          value={confirm}
          autoComplete="new-password"
          onChange={(e) => setConfirm(e.target.value)}
        />
      </Field>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Button
        type="submit"
        variant="primary"
        disabled={busy || username.length < 3 || password.length < 8}
      >
        {busy ? 'Creating…' : 'Create account'}
      </Button>

      <div className="border-t border-ink-200 pt-4">
        <p className="text-sm text-ink-600">
          Rebuilding a PrintFlow you already had? Restore it instead — the
          accounts come back with everything else, so there is no need to make
          one here first.
        </p>
        <Button
          className="mt-2"
          onClick={(event) => {
            // Inside a form, so it would otherwise submit it.
            event.preventDefault()
            setRestoring(true)
          }}
        >
          Restore from a backup
        </Button>
      </div>
    </form>
  )
}

function IntegrationStep({
  provider,
  status,
  redirectUri,
  onChange,
}: {
  provider: string
  status: SetupStatus
  redirectUri?: string
  onChange: () => Promise<void>
}) {
  const integration = status.integrations.find((i) => i.provider === provider)
  const Panel = PANELS[provider]
  if (!integration || !Panel) return null
  return <Panel status={integration} redirectUri={redirectUri} onChange={onChange} />
}

function IntervalsStep({
  status,
  onDone,
}: {
  status: SetupStatus
  onDone: () => Promise<void>
}) {
  const [values, setValues] = useState(status.poll_intervals)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setError(null)
    try {
      await api.post('/api/setup/intervals', values)
      setSaved(true)
      await onDone()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const rows: [string, string, string][] = [
    ['etsy_minutes', 'Etsy receipt poll', 'How often new orders are pulled in.'],
    ['wix_minutes', 'Wix order poll', 'The same, for a connected Wix site.'],
    ['bambuddy_minutes', 'Bambuddy status reconcile', 'How often print jobs advance.'],
    [
      'shipstation_minutes',
      'ShipStation order match',
      'Only runs for orders not yet matched.',
    ],
    [
      'tracking_minutes',
      'Delivery check',
      'How often shipped parcels are asked about. Each parcel is asked less '
        + 'often than this as its journey goes on.',
    ],
  ]

  return (
    <div className="space-y-4">
      {rows.map(([key, label, hint]) => (
        <Field key={key} label={`${label} (minutes)`} hint={hint}>
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
      {error ? <Alert tone="error">{error}</Alert> : null}
      {saved ? <Alert tone="success">Saved.</Alert> : null}
      <Button variant="primary" onClick={save}>
        Save intervals
      </Button>
    </div>
  )
}

function FinishButton({
  status,
  onDone,
}: {
  status: SetupStatus
  onDone: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const missing = status.steps.filter((s) => !s.complete).map((s) => s.label)

  const finish = async () => {
    if (
      missing.length &&
      !window.confirm(
        `These are not connected yet: ${missing.join(', ')}.\n\n` +
          'You can finish now and connect them later from Settings. Continue?',
      )
    ) {
      return
    }
    setBusy(true)
    try {
      await api.post('/api/setup/complete')
      await onDone()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Button variant="primary" onClick={finish} disabled={busy || !status.admin_exists}>
      {busy ? 'Finishing…' : 'Finish setup'}
    </Button>
  )
}
