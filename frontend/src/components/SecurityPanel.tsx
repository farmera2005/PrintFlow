import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import TunnelSection from './TunnelSection'
import { Alert, Badge, Button, Field, Spinner, inputClass } from './ui'

export interface CertificateInfo {
  common_name: string | null
  issuer: string | null
  sans: string[]
  not_before: string
  not_after: string
  days_remaining: number
  expired: boolean
  self_signed: boolean
  fingerprint_sha256: string
  key_type: string
  source: 'generated' | 'uploaded'
}

export interface TunnelInfo {
  mode: 'off' | 'named' | 'quick'
  enabled: boolean
  hostname: string | null
  has_token: boolean
  token_hint: string | null
  supervised: boolean
  status: {
    binary_available: boolean
    running: boolean
    hostname?: string | null
    public_url?: string | null
    connections?: number
    restarts?: number
    last_error?: string | null
    pid?: number | null
  }
}

export interface SecurityStatus {
  certificate: CertificateInfo | null
  tunnel: TunnelInfo
  effective_base_url: string
  base_url_source: 'tunnel_live' | 'tunnel_config' | 'manual' | 'request'
  base_url_source_label: string
  redirect_uris: Record<string, string>
  https: { running: boolean; port: number; fingerprint_sha256?: string | null; last_error?: string | null }
  https_port: number
  http_port: number
  https_redirect: boolean
  public_base_url: string | null
  suggested_hosts: string[]
  current_host: string
  suggested_base_url: string
  secret_key_source: string
  data_dir: string
  default_validity_days: number
}

function CertificateSummary({ certificate }: { certificate: CertificateInfo }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
      <dt className="text-ink-500">Issued to</dt>
      <dd className="text-ink-800">{certificate.common_name ?? '—'}</dd>
      <dt className="text-ink-500">Valid for</dt>
      <dd className="break-words text-ink-800">
        {certificate.sans.length ? certificate.sans.join(', ') : '—'}
      </dd>
      <dt className="text-ink-500">Expires</dt>
      <dd className={certificate.expired ? 'text-red-700' : 'text-ink-800'}>
        {formatDateTime(certificate.not_after)}
        {certificate.expired ? ' — expired' : ` (${certificate.days_remaining} days)`}
      </dd>
      <dt className="text-ink-500">Fingerprint</dt>
      <dd className="break-all font-mono text-xs text-ink-600">
        {certificate.fingerprint_sha256}
      </dd>
    </dl>
  )
}

export default function SecurityPanel({
  onChanged,
  compact,
}: {
  onChanged?: () => void | Promise<void>
  compact?: boolean
}) {
  const [status, setStatus] = useState<SecurityStatus | null>(null)
  const [hosts, setHosts] = useState('')
  const [validity, setValidity] = useState(825)
  const [baseUrl, setBaseUrl] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [showUpload, setShowUpload] = useState(false)
  const [certPem, setCertPem] = useState('')
  const [keyPem, setKeyPem] = useState('')

  // Throws on failure — callers decide whether that is an error to show or a
  // listener that is still coming back up.
  const fetchStatus = useCallback(async () => {
    const data = await api.get<SecurityStatus>('/api/security')
    setStatus(data)
    setValidity(data.default_validity_days)
    setBaseUrl((current) => current || data.public_base_url || data.suggested_base_url)
    setHosts((current) => {
      if (current) return current
      const seed = data.certificate?.sans?.length
        ? data.certificate.sans
        : [...new Set([data.current_host, ...data.suggested_hosts])]
      return seed.join(', ')
    })
  }, [])

  const load = useCallback(async () => {
    try {
      await fetchStatus()
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [fetchStatus])

  useEffect(() => {
    load()
  }, [load])

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    setNote(null)
    try {
      await action()
      await load()
      await onChanged?.()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  /**
   * Applying a certificate restarts the HTTPS listener we are almost certainly
   * talking over, so the next request lands mid-restart. Retry for a few
   * seconds rather than flashing a spurious network error.
   */
  const waitForListener = async () => {
    let lastError: unknown = null
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000))
      try {
        await fetchStatus()
        setError(null)
        return
      } catch (err) {
        lastError = err
      }
    }
    setError(
      `The new certificate was saved, but HTTPS has not come back: ${errorMessage(lastError)}. ` +
        'Port 8000 (HTTP) is still available.',
    )
  }

  const generate = () =>
    run('generate', async () => {
      const result = await api.post<{ result: { applied: boolean; detail: string } }>(
        '/api/security/certificate/generate',
        {
          hosts: hosts
            .split(/[,\n]/)
            .map((h) => h.trim())
            .filter(Boolean),
          validity_days: validity,
        },
      )
      setNote(result.result.detail)
      await waitForListener()
    })

  const upload = () =>
    run('upload', async () => {
      const result = await api.post<{ result: { detail: string } }>(
        '/api/security/certificate/upload',
        { cert_pem: certPem, key_pem: keyPem },
      )
      setCertPem('')
      setKeyPem('')
      setShowUpload(false)
      setNote(result.result.detail)
      await waitForListener()
    })

  const saveBaseUrl = () =>
    run('base-url', async () => {
      await api.post('/api/security/base-url', { public_base_url: baseUrl })
      setNote('Base URL saved. Register the redirect URIs shown on the Etsy and QuickBooks steps.')
    })

  const toggleRedirect = (enabled: boolean) =>
    run('redirect', async () => {
      await api.post('/api/security/https-redirect', { enabled })
    })

  if (!status) {
    return error ? (
      <Alert tone="error">{error}</Alert>
    ) : (
      <div className="flex justify-center py-6">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  const tunnelDrivesAddress =
    status.base_url_source === 'tunnel_live' || status.base_url_source === 'tunnel_config'

  return (
    <div className="space-y-5">
      {!compact ? (
        <p className="text-sm text-ink-600">
          Etsy and QuickBooks both require an <code>https</code> redirect URI, so
          PrintFlow generates its own certificate — there is nothing to run by hand.
          A self-signed one is already active; re-issue it below for the hostname you
          actually use, then connect the integrations.
        </p>
      ) : null}

      {/* --- Address & callback URLs ------------------------------------ */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-ink-800">Address & callback URLs</h3>

        <div className="rounded-md bg-ink-50 p-3">
          <p className="text-xs uppercase tracking-wide text-ink-500">
            Callbacks resolve to
          </p>
          <p className="mt-0.5 break-all font-mono text-sm text-ink-900">
            {status.effective_base_url || '—'}
          </p>
          <p className="mt-1 text-xs text-ink-500">
            Taken from {status.base_url_source_label}.
          </p>

          <p className="mt-3 text-xs text-ink-600">
            Register these exactly, in your Etsy and Intuit app settings:
          </p>
          <ul className="mt-1 space-y-1">
            {Object.entries(status.redirect_uris ?? {}).map(([provider, uri]) => (
              <li key={provider}>
                <span className="mr-1 text-xs uppercase text-ink-400">
                  {provider === 'qbo' ? 'QuickBooks' : provider}
                </span>
                <code className="break-all text-xs text-ink-800">{uri}</code>
              </li>
            ))}
          </ul>
        </div>

        {tunnelDrivesAddress ? (
          <Alert tone="info">
            The Cloudflare Tunnel sets this automatically — callbacks always come
            back through it, so nothing to fill in below.
          </Alert>
        ) : null}

        <Field
          label="Public base URL"
          hint={
            tunnelDrivesAddress
              ? 'Only used if you turn the tunnel off — the tunnel hostname takes precedence.'
              : 'How you reach PrintFlow in the browser. OAuth redirect URIs are built from this, so it must match exactly.'
          }
        >
          <input
            className={inputClass}
            value={baseUrl}
            placeholder={status.suggested_base_url}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </Field>
        {baseUrl.startsWith('http://') ? (
          <Alert tone="warning">
            Etsy and Intuit reject <code>http://</code> redirect URIs. Use the HTTPS
            address (port {status.https_port}) here.
          </Alert>
        ) : null}
        <Button size="sm" onClick={saveBaseUrl} disabled={busy === 'base-url'}>
          {busy === 'base-url' ? 'Saving…' : 'Save address'}
        </Button>
      </section>

      {/* The tunnel decides what the public address *is*, so it belongs next to
          the address field — above the certificate, which only matters for
          reaching PrintFlow directly on the LAN. */}
      <TunnelSection tunnel={status.tunnel} onApplied={load} />

      {/* --- Certificate ------------------------------------------------ */}
      <section className="space-y-3 border-t border-ink-200 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-ink-800">HTTPS certificate</h3>
          {status.https.running ? (
            <Badge className="bg-emerald-100 text-emerald-800 ring-emerald-300">
              listening on {status.https_port}
            </Badge>
          ) : (
            <Badge className="bg-amber-100 text-amber-900 ring-amber-300">not listening</Badge>
          )}
          {status.certificate?.self_signed ? <Badge>self-signed</Badge> : null}
          {status.certificate ? <Badge>{status.certificate.source}</Badge> : null}
        </div>

        {status.https.last_error ? (
          <Alert tone="error">{status.https.last_error}</Alert>
        ) : null}

        {status.certificate ? (
          <>
            <CertificateSummary certificate={status.certificate} />
            <a
              className="inline-block text-sm text-ink-700 underline"
              href="/api/security/certificate.crt"
            >
              Download certificate
            </a>
            <p className="text-xs text-ink-500">
              Install it in your browser or OS trust store to stop the warning. Only the
              public certificate is downloaded — the private key never leaves the server.
            </p>
          </>
        ) : (
          <Alert tone="warning">No certificate yet. Generate one below.</Alert>
        )}

        <div className="space-y-3 rounded-md bg-ink-50 p-3">
          <Field
            label="Hostnames and IP addresses"
            hint="Comma separated. Include every address you open PrintFlow on — a name not listed here will still warn."
          >
            <textarea
              className={`${inputClass} min-h-16`}
              value={hosts}
              onChange={(e) => setHosts(e.target.value)}
            />
          </Field>
          <div className="flex flex-wrap gap-1.5">
            {status.suggested_hosts.map((host) => (
              <button
                key={host}
                type="button"
                onClick={() =>
                  setHosts((current) =>
                    current
                      .split(/[,\n]/)
                      .map((h) => h.trim())
                      .includes(host)
                      ? current
                      : current
                        ? `${current}, ${host}`
                        : host,
                  )
                }
                className="rounded-full bg-white px-2 py-0.5 text-xs text-ink-600 ring-1 ring-ink-300 hover:bg-ink-100"
              >
                + {host}
              </button>
            ))}
          </div>
          <Field label="Valid for (days)">
            <input
              className={inputClass}
              type="number"
              min={1}
              max={3650}
              value={validity}
              onChange={(e) => setValidity(Number(e.target.value))}
            />
          </Field>
          <Button variant="primary" onClick={generate} disabled={busy === 'generate'}>
            {busy === 'generate'
              ? 'Generating…'
              : status.certificate
                ? 'Re-issue certificate'
                : 'Generate certificate'}
          </Button>
          <p className="text-xs text-ink-500">
            Applied immediately — HTTPS restarts on the new certificate. Your browser
            will warn again until you trust the new one.
          </p>
        </div>

        <button
          type="button"
          className="text-xs font-medium text-ink-600 underline"
          onClick={() => setShowUpload((open) => !open)}
        >
          {showUpload ? 'Hide' : 'Use my own certificate instead'}
        </button>
        {showUpload ? (
          <div className="space-y-3 rounded-md bg-ink-50 p-3">
            <Field label="Certificate (PEM)">
              <textarea
                className={`${inputClass} min-h-24 font-mono text-xs`}
                placeholder="-----BEGIN CERTIFICATE-----"
                value={certPem}
                onChange={(e) => setCertPem(e.target.value)}
              />
            </Field>
            <Field label="Private key (PEM, no passphrase)">
              <textarea
                className={`${inputClass} min-h-24 font-mono text-xs`}
                placeholder="-----BEGIN PRIVATE KEY-----"
                value={keyPem}
                onChange={(e) => setKeyPem(e.target.value)}
              />
            </Field>
            <Button
              onClick={upload}
              disabled={busy === 'upload' || !certPem.trim() || !keyPem.trim()}
            >
              {busy === 'upload' ? 'Validating…' : 'Upload and apply'}
            </Button>
          </div>
        ) : null}
      </section>

      {/* --- Redirect --------------------------------------------------- */}
      <section className="space-y-2 border-t border-ink-200 pt-4">
        <h3 className="text-sm font-semibold text-ink-800">Plain HTTP</h3>
        <label className="flex items-start gap-2 text-sm text-ink-700">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={status.https_redirect}
            disabled={busy === 'redirect' || !status.https.running}
            onChange={(e) => toggleRedirect(e.target.checked)}
          />
          <span>
            Redirect HTTP to HTTPS
            <span className="block text-xs text-ink-500">
              Port {status.http_port} stays open either way, so a bad certificate can
              never lock you out. The redirect is skipped automatically whenever HTTPS
              is not listening.
            </span>
          </span>
        </label>
      </section>

      <p className="text-xs text-ink-500">
        Encryption key: {status.secret_key_source === 'environment'
          ? 'from the SECRET_KEY environment variable'
          : `generated and stored in ${status.data_dir}`}
        . Back that up — stored credentials cannot be decrypted without it.
      </p>

      {note ? <Alert tone="success">{note}</Alert> : null}
      {error ? <Alert tone="error">{error}</Alert> : null}
    </div>
  )
}
