import { useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { SecurityStatus, TunnelBinary, TunnelInfo } from './SecurityPanel'
import { Alert, Badge, Button, Field, inputClass } from './ui'

interface VersionCheck {
  installed: string | null
  latest: string | null
  arch: string
  /** Null when GitHub could not be reached: "no update" and "could not ask"
   *  must not look the same on a screen. */
  update_available: boolean | null
  binary: TunnelBinary
}

/** Which cloudflared is running, and how to get a newer one.
 *
 * It is worth a panel of its own because of where the binary comes from: it is
 * downloaded when the image is *built*, and that build layer is cached on the
 * shop's machine — so rebuilding PrintFlow a year later leaves cloudflared
 * exactly as it was. Cloudflare keeps shipping releases and eventually stops
 * supporting old connectors, and the first sign of that is a tunnel that will
 * not connect on a Tuesday morning.
 *
 * So the version is shown without being asked for, and the update lands in the
 * data volume rather than in the image — it survives the next rebuild, and
 * nobody has to pass a build argument to Docker on a NAS. */
function CloudflaredVersion({
  tunnel,
  onApplied,
}: {
  tunnel: TunnelInfo
  onApplied: () => Promise<void>
}) {
  const [check, setCheck] = useState<VersionCheck | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const binary = check?.binary ?? tunnel.binary
  if (!binary?.available) return null

  const act = async (label: string, run: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    setNote(null)
    try {
      await run()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const look = () =>
    act('check', async () => {
      const found = await api.get<VersionCheck>('/api/security/tunnel/version')
      setCheck(found)
      if (found.update_available === null) {
        setNote(
          'Could not reach github.com to ask what the latest version is. ' +
            'Update anyway and PrintFlow will tell you what it got.',
        )
      } else if (!found.update_available) {
        setNote(`This is the current release (${found.installed}).`)
      }
    })

  const update = () =>
    act('update', async () => {
      const done = await api.post<{
        installed: string | null
        previous: string | null
        changed: boolean
        restarted: boolean
        binary: TunnelBinary
      }>('/api/security/tunnel/update')
      setCheck((was) => (was ? { ...was, binary: done.binary, installed: done.installed } : was))
      setNote(
        done.changed
          ? `Updated ${done.previous ?? 'the bundled binary'} → ${done.installed}.` +
            (done.restarted ? ' The tunnel restarted on the new one.' : '')
          : `Already on ${done.installed}; nothing changed.`,
      )
      await onApplied()
    })

  const revert = () =>
    act('revert', async () => {
      const done = await api.post<{ removed: boolean; binary: TunnelBinary }>(
        '/api/security/tunnel/binary/revert',
      )
      setCheck((was) => (was ? { ...was, binary: done.binary } : was))
      setNote(
        done.removed
          ? `Back on the bundled binary (${done.binary.bundled_version ?? 'unknown version'}).`
          : 'There was no downloaded copy to remove.',
      )
      await onApplied()
    })

  return (
    <div className="rounded-lg bg-ink-50 p-3 ring-1 ring-ink-200">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-ink-700">
          cloudflared{' '}
          <span className="font-mono text-ink-900">{binary.version ?? 'unknown'}</span>
          <span className="text-ink-500">
            {' '}
            · {binary.arch} · {binary.using_downloaded ? 'downloaded' : 'bundled with the image'}
          </span>
        </span>
        {check?.update_available ? (
          <Badge className="bg-amber-100 text-amber-900 ring-amber-300">
            {check.latest} available
          </Badge>
        ) : null}
        <div className="ml-auto flex flex-wrap gap-2">
          <Button size="sm" onClick={look} disabled={busy !== null}>
            {busy === 'check' ? 'Checking…' : 'Check for updates'}
          </Button>
          <Button
            size="sm"
            variant={check?.update_available ? 'primary' : undefined}
            onClick={update}
            disabled={busy !== null}
          >
            {busy === 'update' ? 'Downloading…' : 'Update cloudflared'}
          </Button>
          {binary.using_downloaded ? (
            <Button size="sm" variant="ghost" onClick={revert} disabled={busy !== null}>
              {busy === 'revert' ? 'Reverting…' : 'Use the bundled one'}
            </Button>
          ) : null}
        </div>
      </div>

      <p className="mt-1 text-xs text-ink-500">
        The bundled binary is fixed when the image is built, so rebuilding
        PrintFlow does not refresh it. Updating here downloads the current
        release into the data volume, which survives rebuilds — and PrintFlow
        runs whichever of the two is newer.
      </p>

      {binary.using_downloaded && binary.bundled_version ? (
        <p className="mt-1 text-xs text-ink-500">
          The image carries {binary.bundled_version}; this install is running the
          downloaded {binary.downloaded_version}.
        </p>
      ) : null}
      {!binary.using_downloaded && binary.downloaded_version ? (
        <p className="mt-1 text-xs text-ink-500">
          A downloaded copy ({binary.downloaded_version}) is present but older than
          the one in the image, so the image's is being used.
        </p>
      ) : null}

      {note ? <p className="mt-2 text-xs text-ink-700">{note}</p> : null}
      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
    </div>
  )
}

const MODES: { value: TunnelInfo['mode']; label: string; hint: string }[] = [
  { value: 'off', label: 'Off', hint: 'PrintFlow is reachable on your LAN only.' },
  {
    value: 'named',
    label: 'Cloudflare Tunnel',
    hint: 'A stable public hostname on your own domain. This is the one to use for OAuth callbacks.',
  },
  {
    value: 'quick',
    label: 'Quick tunnel (temporary)',
    hint: 'A throwaway trycloudflare.com address, no account needed. The hostname changes every restart, so any redirect URI you register with it stops working.',
  },
]

interface TestResult {
  url: string
  ok: boolean
  status: number | null
  diagnosis: string
  detail: string
}

export default function TunnelSection({
  tunnel,
  onApplied,
}: {
  tunnel: TunnelInfo
  onApplied: () => Promise<void>
}) {
  const [mode, setMode] = useState<TunnelInfo['mode']>(tunnel.mode)
  const [token, setToken] = useState('')
  const [hostname, setHostname] = useState(tunnel.hostname ?? '')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [logs, setLogs] = useState<string[] | null>(null)
  const [test, setTest] = useState<TestResult | null>(null)

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    try {
      await action()
      await onApplied()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const save = () =>
    run('save', async () => {
      await api.post('/api/security/tunnel', {
        mode,
        enabled: mode !== 'off',
        token: token || null,
        hostname: hostname || null,
      })
      setToken('')
    })

  const showLogs = async () => {
    try {
      const data = await api.get<{ lines: string[] }>('/api/security/tunnel/logs?limit=60')
      setLogs(data.lines)
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const liveUrl = tunnel.status.public_url ?? null
  const active = tunnel.enabled && tunnel.mode !== 'off'

  return (
    <section className="space-y-3 border-t border-ink-200 pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink-800">
          Public access — Cloudflare Tunnel
        </h3>
        {!tunnel.status.binary_available ? (
          <Badge className="bg-amber-100 text-amber-900 ring-amber-300">
            cloudflared not installed
          </Badge>
        ) : tunnel.status.running ? (
          <Badge className="bg-emerald-100 text-emerald-800 ring-emerald-300">
            running{tunnel.status.connections ? ` · ${tunnel.status.connections} edge conns` : ''}
          </Badge>
        ) : active ? (
          <Badge className="bg-amber-100 text-amber-900 ring-amber-300">starting…</Badge>
        ) : (
          <Badge>off</Badge>
        )}
      </div>

      <p className="text-sm text-ink-600">
        Etsy and QuickBooks redirect the browser back to PrintFlow after you authorise
        them, and Intuit will not accept a private address for a production app. A
        Cloudflare Tunnel gives you a real public hostname with a real certificate and
        opens no inbound ports — cloudflared only makes outbound connections.
      </p>

      {!tunnel.status.binary_available ? (
        <Alert tone="warning">
          The cloudflared binary is not in this container, so the tunnel cannot be
          started from here. Rebuild the image with network access, or run cloudflared
          yourself pointing at <code>http://&lt;host&gt;:8000</code>.
        </Alert>
      ) : null}

      <CloudflaredVersion tunnel={tunnel} onApplied={onApplied} />

      <Field label="Mode" hint={MODES.find((m) => m.value === mode)?.hint}>
        <select
          className={inputClass}
          value={mode}
          onChange={(e) => setMode(e.target.value as TunnelInfo['mode'])}
        >
          {MODES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </Field>

      {mode === 'named' ? (
        <>
          <Alert tone="info">
            <p>
              In Cloudflare Zero Trust → Networks → Tunnels, create a tunnel and add a
              Public Hostname. Under <strong>Service</strong>, set it to exactly:
            </p>
            <code className="mt-1 block rounded bg-white/70 px-2 py-1 text-xs">
              HTTP&nbsp;&nbsp;localhost:8000
            </code>
            <p className="mt-1 text-xs">
              Plain <strong>HTTP</strong>, port <strong>8000</strong>. Not HTTPS, and not
              8443 — that port serves a self-signed certificate Cloudflare will not
              trust, and you get a 502 Bad gateway on the callback. (cloudflared runs
              inside this container, so <code>localhost</code> is PrintFlow. If you run
              cloudflared as a separate container instead, use <code>http://app:8000</code>.)
            </p>
          </Alert>
          <Field
            label="Connector token"
            hint={
              tunnel.has_token
                ? `A token is stored (${tunnel.token_hint}). Leave blank to keep it.`
                : 'From the tunnel’s install command — the long string after --token.'
            }
          >
            <input
              className={inputClass}
              type="password"
              value={token}
              placeholder={tunnel.has_token ? '••••••••' : ''}
              onChange={(e) => setToken(e.target.value)}
            />
          </Field>
          <Field
            label="Public hostname"
            hint="The hostname you routed to PrintFlow, e.g. printflow.example.com."
          >
            <input
              className={inputClass}
              value={hostname}
              placeholder="printflow.example.com"
              onChange={(e) => setHostname(e.target.value)}
            />
          </Field>
        </>
      ) : null}

      {mode === 'quick' ? (
        <Alert tone="warning">
          Temporary by design. Cloudflare assigns a new hostname each time it starts, so
          any redirect URI you register will stop working on the next restart. Fine for
          testing the OAuth round-trip; do not leave it running for a live shop.
        </Alert>
      ) : null}

      {tunnel.status.last_error ? (
        <Alert tone="error">{tunnel.status.last_error}</Alert>
      ) : null}
      {error ? <Alert tone="error">{error}</Alert> : null}

      {liveUrl ? (
        <div className="rounded-md bg-emerald-50 p-3 text-sm ring-1 ring-emerald-200">
          <p className="text-emerald-900">
            Live at{' '}
            <a className="font-medium underline" href={liveUrl} target="_blank" rel="noreferrer">
              {liveUrl}
            </a>
          </p>
          <p className="mt-1 text-xs text-emerald-800">
            OAuth callback URLs use this automatically — see “Callbacks resolve to”
            above for the exact URIs to register.
          </p>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          size="sm"
          onClick={save}
          disabled={busy === 'save' || (mode === 'named' && !tunnel.has_token && !token)}
        >
          {busy === 'save' ? 'Applying…' : mode === 'off' ? 'Turn off' : 'Save & start'}
        </Button>
        {active ? (
          <>
            <Button
              size="sm"
              onClick={() => run('restart', () => api.post('/api/security/tunnel/restart'))}
              disabled={busy === 'restart'}
            >
              Restart
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => run('stop', () => api.post('/api/security/tunnel/stop'))}
              disabled={busy === 'stop'}
            >
              Stop
            </Button>
          </>
        ) : null}
        {active ? (
          <Button
            size="sm"
            onClick={() =>
              run('test', async () => {
                setTest(await api.post<TestResult>('/api/security/tunnel/test'))
              })
            }
            disabled={busy === 'test'}
          >
            {busy === 'test' ? 'Testing…' : 'Test public URL'}
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={showLogs}>
          {logs ? 'Refresh log' : 'Show log'}
        </Button>
      </div>

      {test ? (
        <Alert tone={test.ok ? 'success' : 'error'}>
          <p className="font-medium">
            {test.ok ? 'Reachable' : 'Not reachable'}
            {test.status ? ` — HTTP ${test.status}` : ''}
          </p>
          <p className="mt-0.5 text-xs">{test.detail}</p>
          <p className="mt-0.5 break-all text-xs opacity-70">Tested {test.url}</p>
        </Alert>
      ) : null}

      {logs ? (
        <pre className="max-h-56 overflow-auto rounded-md bg-ink-900 p-3 text-[11px] leading-relaxed text-ink-200">
          {logs.length ? logs.join('\n') : 'No output yet.'}
        </pre>
      ) : null}
    </section>
  )
}

export type { SecurityStatus }
