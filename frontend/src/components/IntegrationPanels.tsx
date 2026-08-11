import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { BambuddyPrinter, IntegrationStatus } from '../lib/types'
import { Alert, Badge, Button, Field, inputClass } from './ui'

interface Diagnosis {
  summary: string
  credentials: {
    keystring: string
    shared_secret: string
    access_token: string
    user_id: string | null
    shop_id: number | null
  }
  attempts: {
    endpoint: string
    path: string
    variant: string
    status: number | null
    body: string
    ok: boolean
  }[]
  working: string[]
}

interface PanelProps {
  status: IntegrationStatus
  redirectUri?: string
  onChange: () => void | Promise<void>
}

function ConnectedHeader({
  status,
  onDisconnect,
  children,
}: {
  status: IntegrationStatus
  onDisconnect: () => void
  children?: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge className="bg-emerald-100 text-emerald-800 ring-emerald-300">Connected</Badge>
      <span className="text-xs text-ink-500">
        last OK {formatDateTime(status.last_ok_at)}
      </span>
      <div className="ml-auto flex gap-2">
        {children}
        <Button size="sm" variant="ghost" onClick={onDisconnect}>
          Disconnect
        </Button>
      </div>
    </div>
  )
}

function useDisconnect(provider: string, onChange: () => void | Promise<void>) {
  return async () => {
    if (!window.confirm(`Disconnect ${provider}? Stored credentials are deleted.`)) return
    await api.del(`/api/integrations/${provider}`)
    await onChange()
  }
}

function RedirectUriHint({ uri }: { uri?: string }) {
  if (!uri) return null
  return (
    <Alert tone="info">
      Add this exact redirect URI to your app's settings before connecting:
      <code className="mt-1 block break-all rounded bg-white/70 px-2 py-1 text-xs">
        {uri}
      </code>
    </Alert>
  )
}

// --------------------------------------------------------------------------
// Etsy
// --------------------------------------------------------------------------

/** Epoch seconds -> the YYYY-MM-DD an <input type="date"> wants, in local time. */
function toDateInput(epoch: number | null): string {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** YYYY-MM-DD -> epoch seconds at local midnight, so the day the operator
 *  picked is included in full. */
function fromDateInput(value: string): number | null {
  const ms = new Date(`${value}T00:00:00`).getTime()
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000)
}

export function EtsyPanel({ status, redirectUri, onChange }: PanelProps) {
  const [keystring, setKeystring] = useState('')
  const [sharedSecret, setSharedSecret] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [shops, setShops] = useState<{ shop_id: number; shop_name: string }[]>([])
  const [selectedShop, setSelectedShop] = useState<number | null>(null)
  const [shopName, setShopName] = useState<string | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [manualShop, setManualShop] = useState('')
  const [ordersSince, setOrdersSince] = useState<number | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(
    null,
  )
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null)
  const disconnect = useDisconnect('etsy', onChange)

  useEffect(() => {
    if (!status.connected) return
    api
      .get<{
        shops: any[]
        selected_shop_id: number | null
        selected_shop_name: string | null
        orders_since: number | null
        error: string | null
      }>('/api/integrations/etsy/shops')
      .then((data) => {
        setShops(data.shops.filter((s) => s.shop_id))
        setSelectedShop(data.selected_shop_id)
        setShopName(data.selected_shop_name ?? null)
        setOrdersSince(data.orders_since ?? null)
        setListError(data.error)
      })
      .catch((err) => setListError(errorMessage(err)))
  }, [status.connected])

  const connect = async () => {
    setBusy(true)
    setError(null)
    try {
      const data = await api.post<{ authorize_url: string }>('/api/integrations/etsy/start', {
        keystring,
        shared_secret: sharedSecret,
      })
      window.location.href = data.authorize_url
    } catch (err) {
      setError(errorMessage(err))
      setBusy(false)
    }
  }

  const chooseShop = async (shopId: number) => {
    if (!shopId) return
    setBusy(true)
    setError(null)
    try {
      let name = shops.find((s) => s.shop_id === shopId)?.shop_name ?? null
      if (!name) {
        // Best effort: a name is nice to display but must not gate the choice.
        const check = await api
          .post<{ found: boolean; shop_name: string | null }>(
            '/api/integrations/etsy/shop/lookup',
            { shop_id: shopId },
          )
          .catch(() => null)
        name = check?.shop_name ?? null
      }
      const saved = await api.post<{ orders_since: number | null }>(
        '/api/integrations/etsy/shop',
        { shop_id: shopId, shop_name: name },
      )
      setSelectedShop(shopId)
      setShopName(name)
      if (saved.orders_since) setOrdersSince(saved.orders_since)
      await onChange()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const saveOrdersSince = async (value: number | null) => {
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/integrations/etsy/orders-since', { orders_since: value })
      setOrdersSince(value)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (status.connected) {
    return (
      <div className="space-y-3">
        <ConnectedHeader status={status} onDisconnect={disconnect} />

        {shops.length ? (
          <Field label="Shop" hint="Receipts are polled from this shop.">
            <select
              className={inputClass}
              value={selectedShop ?? ''}
              onChange={(e) => chooseShop(Number(e.target.value))}
            >
              <option value="" disabled>
                Select a shop…
              </option>
              {shops.map((shop) => (
                <option key={shop.shop_id} value={shop.shop_id}>
                  {shop.shop_name} ({shop.shop_id})
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <>
            {listError ? (
              <Alert tone="warning">
                Etsy would not list your shops: {listError}
                <span className="mt-1 block text-xs">
                  This does not stop order polling — that uses a different
                  endpoint. Enter your shop ID below and use Test connection to
                  check it for real.
                </span>
              </Alert>
            ) : null}
            <Field
              label="Shop ID"
              hint="The number in your shop's URL in Etsy Shop Manager."
            >
              <div className="flex gap-2">
                <input
                  className={inputClass}
                  inputMode="numeric"
                  placeholder="12345678"
                  value={manualShop}
                  onChange={(e) => setManualShop(e.target.value.replace(/\D/g, ''))}
                />
                <Button
                  onClick={() => chooseShop(Number(manualShop))}
                  disabled={!manualShop || busy}
                >
                  Use
                </Button>
              </div>
            </Field>
          </>
        )}

        {selectedShop ? (
          <p className="text-sm text-ink-600">
            Polling shop <span className="font-mono">{selectedShop}</span>
            {shopName ? ` — ${shopName}` : ''}.
          </p>
        ) : (
          <Alert tone="warning">Pick a shop — polling stays idle until you do.</Alert>
        )}

        {selectedShop ? (
          <Field
            label="Import orders placed since"
            hint="Set to the day you picked the shop, so an established shop's back catalogue does not land on the board. Move it back to backfill older orders."
          >
            <div className="flex flex-wrap items-center gap-2">
              {/* inputClass carries w-full, so the width has to live on a wrapper. */}
              <div className="w-44">
                <input
                  className={inputClass}
                  type="date"
                  value={toDateInput(ordersSince)}
                  disabled={busy}
                  onChange={(e) => {
                    const next = fromDateInput(e.target.value)
                    if (next !== null) void saveOrdersSince(next)
                  }}
                />
              </div>
              <Button
                size="sm"
                disabled={busy || ordersSince === null}
                onClick={() => saveOrdersSince(null)}
              >
                Import everything open
              </Button>
              {ordersSince === null ? (
                <span className="text-xs text-amber-700">
                  No cutoff — every open receipt will be imported.
                </span>
              ) : null}
            </div>
          </Field>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            disabled={!selectedShop || busy}
            onClick={async () => {
              setBusy(true)
              setTestResult(null)
              try {
                setTestResult(
                  await api.post<{ ok: boolean; detail: string }>(
                    '/api/integrations/etsy/test',
                  ),
                )
              } catch (err) {
                setTestResult({ ok: false, detail: errorMessage(err) })
              } finally {
                setBusy(false)
                await onChange()
              }
            }}
          >
            {busy ? 'Testing…' : 'Test connection'}
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={async () => {
              setBusy(true)
              setDiagnosis(null)
              try {
                setDiagnosis(await api.post<Diagnosis>('/api/integrations/etsy/diagnose'))
              } catch (err) {
                setError(errorMessage(err))
              } finally {
                setBusy(false)
              }
            }}
          >
            Diagnose 403s
          </Button>
          <span className="text-xs text-ink-500">
            Test reads your receipt feed. Diagnose asks Etsy which header
            combination it accepts.
          </span>
        </div>
        {testResult ? (
          <Alert tone={testResult.ok ? 'success' : 'error'}>{testResult.detail}</Alert>
        ) : null}

        {diagnosis ? (
          <div className="space-y-2 rounded-md bg-ink-50 p-3">
            <p className="text-sm font-medium text-ink-800">{diagnosis.summary}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-xs text-ink-600">
              <dt>keystring</dt>
              <dd className="font-mono">{diagnosis.credentials.keystring}</dd>
              <dt>shared secret</dt>
              <dd className="font-mono">{diagnosis.credentials.shared_secret}</dd>
              <dt>access token</dt>
              <dd className="font-mono">{diagnosis.credentials.access_token}</dd>
            </dl>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-ink-500">
                  <tr>
                    <th className="pr-3 font-medium">Endpoint</th>
                    <th className="pr-3 font-medium">Headers</th>
                    <th className="pr-3 font-medium">Status</th>
                    <th className="font-medium">Etsy said</th>
                  </tr>
                </thead>
                <tbody>
                  {diagnosis.attempts.map((a, i) => (
                    <tr key={i} className={a.ok ? 'text-emerald-700' : 'text-ink-600'}>
                      <td className="pr-3 align-top">{a.endpoint}</td>
                      <td className="pr-3 align-top">{a.variant}</td>
                      <td className="pr-3 align-top font-mono">{a.status ?? '—'}</td>
                      <td className="align-top break-all">{a.body}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-ink-500">
              Credentials are shown truncated. Safe to copy this table.
            </p>
          </div>
        ) : null}
        {error ? <Alert tone="error">{error}</Alert> : null}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-600">
        Create an app at{' '}
        <a
          className="underline"
          href="https://www.etsy.com/developers/your-apps"
          target="_blank"
          rel="noreferrer"
        >
          etsy.com/developers
        </a>
        , then paste its keystring and shared secret. PrintFlow only ever reads from
        Etsy.
      </p>
      <RedirectUriHint uri={redirectUri} />
      <Field label="Keystring (API key)">
        <input
          className={inputClass}
          value={keystring}
          onChange={(e) => setKeystring(e.target.value)}
        />
      </Field>
      <Field
        label="Shared secret"
        hint="Required. Etsy's API rejects the keystring on its own."
      >
        <input
          className={inputClass}
          type="password"
          value={sharedSecret}
          onChange={(e) => setSharedSecret(e.target.value)}
        />
      </Field>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Button
        variant="primary"
        onClick={connect}
        disabled={busy || !keystring || !sharedSecret}
      >
        {busy ? 'Redirecting…' : 'Connect Etsy'}
      </Button>
    </div>
  )
}

// --------------------------------------------------------------------------
// QuickBooks Online
// --------------------------------------------------------------------------

interface ManufacturingSettings {
  account_id: string | null
  account_name: string | null
  offset_account_id: string | null
  offset_account_name: string | null
  payment_type: string
  doc_number_prefix: string
}

interface QboAccount {
  id: string
  name: string
  type: string
}

/** How a made-items sheet posts.
 *
 * Two accounts, and they do different jobs. QuickBooks calls the first one
 * AccountRef and treats it as the account the expense is *paid from* — it only
 * accepts a Bank account there, or a Credit Card account when the payment type
 * is CreditCard. The second is optional and absorbs value added beyond the
 * components, so that labour does not show up as money leaving a bank account.
 *
 * Neither has a default: which account is right depends on the shop's chart of
 * accounts, and picking one on their behalf would file real money somewhere
 * nobody chose. Posting stays blocked until the first is set.
 */
function ManufacturingPosting() {
  const [settings, setSettings] = useState<ManufacturingSettings | null>(null)
  const [accounts, setAccounts] = useState<QboAccount[]>([])
  const [offsetAccounts, setOffsetAccounts] = useState<QboAccount[]>([])
  const [paymentTypes, setPaymentTypes] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .get<{ settings: ManufacturingSettings; payment_types: string[] }>(
        '/api/manufacturing/settings',
      )
      .then((data) => {
        setSettings(data.settings)
        setPaymentTypes(data.payment_types)
      })
      .catch((err) => setError(errorMessage(err)))
    api
      .get<{ accounts: QboAccount[] }>('/api/integrations/qbo/accounts?role=payment')
      .then((data) => setAccounts(data.accounts))
      .catch(() => setAccounts([]))
    api
      .get<{ accounts: QboAccount[] }>('/api/integrations/qbo/accounts?role=offset')
      .then((data) => setOffsetAccounts(data.accounts))
      .catch(() => setOffsetAccounts([]))
  }, [])

  const save = async (changes: Partial<ManufacturingSettings>) => {
    setBusy(true)
    setError(null)
    try {
      const data = await api.put<{ settings: ManufacturingSettings }>(
        '/api/manufacturing/settings',
        changes,
      )
      setSettings(data.settings)
      setSaved(true)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!settings) return null

  return (
    <div className="space-y-3 rounded-md bg-ink-50 p-3">
      <p className="text-sm font-medium text-ink-800">Manufacturing postings</p>
      <Field label="Payment type" hint="How QuickBooks records the expense.">
        <select
          className={inputClass}
          value={settings.payment_type}
          disabled={busy}
          onChange={(e) => save({ payment_type: e.target.value })}
        >
          {paymentTypes.map((type) => (
            <option key={type} value={type}>
              {type}
            </option>
          ))}
        </select>
      </Field>
      <Field
        label="Paid from"
        hint={
          settings.payment_type === 'CreditCard'
            ? 'QuickBooks only accepts a credit card account here.'
            : 'QuickBooks only accepts a bank account here. A sheet costed at its BOM nets to zero and never touches it, so a clearing account works well.'
        }
      >
        <select
          className={inputClass}
          value={settings.account_id ?? ''}
          disabled={busy}
          onChange={(e) => {
            const account = accounts.find((a) => a.id === e.target.value)
            save({ account_id: account?.id ?? null, account_name: account?.name ?? null })
          }}
        >
          <option value="">Choose an account…</option>
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.name} ({account.type})
            </option>
          ))}
        </select>
      </Field>
      <Field
        label="Value added goes to (optional)"
        hint="When you cost items above their components to absorb labour or machine time, that difference is credited here. Leave it blank and it comes out of the account above instead."
      >
        <select
          className={inputClass}
          value={settings.offset_account_id ?? ''}
          disabled={busy}
          onChange={(e) => {
            const account = offsetAccounts.find((a) => a.id === e.target.value)
            save({
              offset_account_id: account?.id ?? null,
              offset_account_name: account?.name ?? null,
            })
          }}
        >
          <option value="">None — it comes out of the account above</option>
          {offsetAccounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.name} ({account.type})
            </option>
          ))}
        </select>
      </Field>
      {!accounts.length ? (
        <p className="text-xs text-ink-500">
          Could not read your chart of accounts. Reconnect QuickBooks, or reload
          this page once it is back.
        </p>
      ) : null}
      {!settings.account_id ? (
        <Alert tone="warning">
          Until a "paid from" account is chosen, made-items sheets can be built but
          not posted.
        </Alert>
      ) : null}
      {saved && settings.account_id ? (
        <p className="text-xs text-emerald-700">Saved.</p>
      ) : null}
      {error ? <Alert tone="error">{error}</Alert> : null}
    </div>
  )
}

export function QboPanel({ status, redirectUri, onChange }: PanelProps) {
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [environment, setEnvironment] = useState('production')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const disconnect = useDisconnect('qbo', onChange)

  const connect = async () => {
    setBusy(true)
    setError(null)
    try {
      const data = await api.post<{ authorize_url: string }>('/api/integrations/qbo/start', {
        client_id: clientId,
        client_secret: clientSecret,
        environment,
      })
      window.location.href = data.authorize_url
    } catch (err) {
      setError(errorMessage(err))
      setBusy(false)
    }
  }

  if (status.connected) {
    return (
      <div className="space-y-3">
        <ConnectedHeader status={status} onDisconnect={disconnect} />
        <dl className="grid grid-cols-2 gap-2 text-sm">
          <dt className="text-ink-500">Company</dt>
          <dd className="text-ink-800">{status.detail.company_name ?? '—'}</dd>
          <dt className="text-ink-500">Realm ID</dt>
          <dd className="font-mono text-xs text-ink-800">{status.detail.realm_id ?? '—'}</dd>
          <dt className="text-ink-500">Environment</dt>
          <dd className="text-ink-800">{status.detail.environment}</dd>
        </dl>
        <p className="text-xs text-ink-500">
          Order handling is read-only: PrintFlow reads QtyOnHand and never posts
          anything while importing or fulfilling orders. The one write is a
          made-items sheet on the Manufacturing tab, and only when you press Post.
        </p>
        <ManufacturingPosting />
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-600">
        Paste the client ID and secret from your Intuit developer app.
      </p>
      <RedirectUriHint uri={redirectUri} />
      <Field label="Client ID">
        <input
          className={inputClass}
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
        />
      </Field>
      <Field label="Client secret">
        <input
          className={inputClass}
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
        />
      </Field>
      <Field label="Environment">
        <select
          className={inputClass}
          value={environment}
          onChange={(e) => setEnvironment(e.target.value)}
        >
          <option value="production">Production</option>
          <option value="sandbox">Sandbox</option>
        </select>
      </Field>
      {error ? <Alert tone="error">{error}</Alert> : null}
      <Button
        variant="primary"
        onClick={connect}
        disabled={busy || !clientId || !clientSecret}
      >
        {busy ? 'Redirecting…' : 'Connect QuickBooks'}
      </Button>
    </div>
  )
}

// --------------------------------------------------------------------------
// Bambuddy
// --------------------------------------------------------------------------

interface BambuddyEndpoints {
  error: string | null
  spec_path?: string
  version?: string
  discovered: Record<string, { path: string | null; alternatives: string[] }>
  collections: { path: string; methods: string[] }[]
  current: Record<string, string>
}

const PATH_ROLES: {
  key: string
  label: string
  hint: string
  /** Carries {printer_id}, so it is never one of the listable collections. */
  templated?: boolean
}[] = [
  { key: 'printers', label: 'Printers', hint: 'The farm, as shown on the Printers screen.' },
  { key: 'archives', label: 'Archives', hint: 'Browsed when mapping a product to a print file.' },
  { key: 'queue', label: 'Queue', hint: 'Read for job status, and posted to when a plate is sent.' },
  {
    key: 'library_folders',
    label: 'Library folders',
    hint: 'Every folder, with the id of its parent. The picker builds the tree from this.',
  },
  {
    key: 'library_files',
    label: 'Library files',
    hint: 'Every file, with the id of its folder.',
  },
  {
    key: 'files',
    label: 'File manager (one folder at a time)',
    hint: 'Only used by builds that have no library collections.',
  },
  {
    key: 'printer_files',
    label: "One printer's files",
    hint: 'Files on a single machine. {printer_id} is substituted.',
    templated: true,
  },
  {
    key: 'printer_detail',
    label: 'One printer',
    hint:
      'Read only when the farm listing carries no live readings. ' +
      '{printer_id} is substituted.',
    templated: true,
  },
]

export function BambuddyPanel({ status, onChange }: PanelProps) {
  const [baseUrl, setBaseUrl] = useState(status.detail.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [pathOverrides, setPathOverrides] = useState<Record<string, string>>({})
  const [fields, setFields] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [adopted, setAdopted] = useState<Record<string, string>>({})
  const [endpoints, setEndpoints] = useState<BambuddyEndpoints | null>(null)
  const [printers, setPrinters] = useState<BambuddyPrinter[]>(status.detail.printers ?? [])
  const disconnect = useDisconnect('bambuddy', onChange)

  useEffect(() => {
    setBaseUrl(status.detail.base_url ?? '')
    setPrinters(status.detail.printers ?? [])
  }, [status.detail.base_url, status.detail.printers])

  /** Ask the instance what it serves. Used on a 404, and on demand. */
  const loadEndpoints = async () => {
    setBusy(true)
    try {
      const data = await api.post<BambuddyEndpoints>('/api/integrations/bambuddy/endpoints', {
        base_url: baseUrl,
        api_key: apiKey,
      })
      setEndpoints(data)
      setAdvanced(true)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    setAdopted({})
    try {
      const body: Record<string, unknown> = { base_url: baseUrl, api_key: apiKey }
      const chosen = Object.fromEntries(
        Object.entries(pathOverrides).filter(([, value]) => value),
      )
      if (Object.keys(chosen).length) body.paths = chosen
      if (fields.trim()) body.fields = JSON.parse(fields)
      const data = await api.post<{
        printers: BambuddyPrinter[]
        openapi: any
        adopted_paths: Record<string, string>
      }>('/api/integrations/bambuddy/config', body)
      setPrinters(data.printers)
      setAdopted(data.adopted_paths ?? {})
      await onChange()
    } catch (err) {
      const message = errorMessage(err)
      setError(message)
      // A 404 means the host is right and the path is wrong, which is the one
      // failure the operator can fix here — so hand them the real list.
      if (message.includes('404')) await loadEndpoints()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      {status.connected ? <ConnectedHeader status={status} onDisconnect={disconnect} /> : null}
      <Field label="Base URL" hint="e.g. http://192.168.1.50:8080 — the local Bambuddy instance.">
        <input
          className={inputClass}
          value={baseUrl}
          placeholder="http://bambuddy.local:8080"
          onChange={(e) => setBaseUrl(e.target.value)}
        />
      </Field>
      <Field
        label="API key"
        hint={status.connected ? 'Leave blank to keep the stored key.' : undefined}
      >
        <input
          className={inputClass}
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </Field>

      <button
        type="button"
        className="text-xs font-medium text-ink-600 underline"
        onClick={() => setAdvanced((open) => !open)}
      >
        {advanced ? 'Hide' : 'Show'} advanced endpoint settings
      </button>
      {advanced ? (
        <div className="space-y-3 rounded-md bg-ink-50 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={loadEndpoints} disabled={busy || !baseUrl}>
              {busy ? 'Reading…' : 'Read endpoints from this instance'}
            </Button>
            <span className="text-xs text-ink-500">
              Bambuddy publishes an OpenAPI description of itself. PrintFlow reads
              the paths from it rather than assuming.
            </span>
          </div>

          {endpoints?.error ? (
            <Alert tone="warning">
              Could not read the API description: {endpoints.error}
            </Alert>
          ) : null}
          {endpoints && !endpoints.error ? (
            <p className="text-xs text-ink-600">
              Read <span className="font-mono">{endpoints.spec_path}</span>
              {endpoints.version ? ` (version ${endpoints.version})` : ''} —{' '}
              {endpoints.collections.length} listable endpoints.
            </p>
          ) : null}

          {PATH_ROLES.map((role) => {
            const suggestion = endpoints?.discovered?.[role.key]?.path
            const current =
              pathOverrides[role.key] ?? endpoints?.current?.[role.key] ?? ''
            return (
              <Field
                key={role.key}
                label={`${role.label} endpoint`}
                hint={
                  suggestion && role.templated
                    ? `${role.hint} This instance suggests ${suggestion}.`
                    : role.hint
                }
              >
                {endpoints?.collections.length && !role.templated ? (
                  <select
                    className={inputClass}
                    value={current}
                    onChange={(e) =>
                      setPathOverrides((prev) => ({ ...prev, [role.key]: e.target.value }))
                    }
                  >
                    {!endpoints.collections.some((c) => c.path === current) ? (
                      <option value={current}>{current} (not in this instance)</option>
                    ) : null}
                    {endpoints.collections.map((c) => (
                      <option key={c.path} value={c.path}>
                        {c.path}
                        {suggestion === c.path ? '  ← suggested' : ''}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className={inputClass}
                    value={current}
                    placeholder={`/api/${role.key}`}
                    onChange={(e) =>
                      setPathOverrides((prev) => ({ ...prev, [role.key]: e.target.value }))
                    }
                  />
                )}
              </Field>
            )
          })}

          <Field
            label="Queue payload field names"
            hint="Only if your build names the request fields differently. JSON; omitted keys keep their defaults."
          >
            <input
              className={inputClass}
              placeholder='{"archive_id":"archive_id","plate_number":"plate"}'
              value={fields}
              onChange={(e) => setFields(e.target.value)}
            />
          </Field>
        </div>
      ) : null}

      {error ? <Alert tone="error">{error}</Alert> : null}
      {Object.keys(adopted).length ? (
        <Alert tone="success">
          Took these endpoints from the instance's own API description:{' '}
          {Object.entries(adopted)
            .map(([role, path]) => `${role} → ${path}`)
            .join(', ')}
          .
        </Alert>
      ) : null}
      <Button variant="primary" onClick={save} disabled={busy || !baseUrl}>
        {busy ? 'Validating…' : status.connected ? 'Re-validate & save' : 'Connect Bambuddy'}
      </Button>

      {printers.length ? (
        <div>
          <p className="text-xs font-medium text-ink-600">
            Printers found ({printers.length})
          </p>
          <ul className="mt-1 space-y-1">
            {printers.map((printer, index) => (
              <li key={printer.id ?? index} className="text-sm text-ink-700">
                {printer.name ?? `Printer ${printer.id}`}
                {printer.model ? (
                  <span className="text-ink-400"> · {printer.model}</span>
                ) : null}
                {printer.status ? <Badge className="ml-2">{printer.status}</Badge> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : status.connected ? (
        <p className="text-sm text-ink-500">No printers reported by this instance.</p>
      ) : null}
      {status.detail.api_version ? (
        <p className="text-xs text-ink-500">
          API version {String(status.detail.api_version)}
        </p>
      ) : null}
    </div>
  )
}

// --------------------------------------------------------------------------
// ShipStation
// --------------------------------------------------------------------------

export function ShipStationPanel({ status, onChange }: PanelProps) {
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [stores, setStores] = useState<any[]>([])
  const [selectedStore, setSelectedStore] = useState<number | null>(
    status.detail.store_id ?? null,
  )
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const disconnect = useDisconnect('shipstation', onChange)

  useEffect(() => {
    if (!status.connected) return
    api
      .get<{ stores: any[]; selected_store_id: number | null }>(
        '/api/integrations/shipstation/stores',
      )
      .then((data) => {
        setStores(data.stores)
        setSelectedStore(data.selected_store_id)
      })
      .catch((err) => setError(errorMessage(err)))
  }, [status.connected])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const data = await api.post<{ stores: any[] }>('/api/integrations/shipstation/config', {
        api_key: apiKey,
        api_secret: apiSecret,
      })
      setStores(data.stores)
      await onChange()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const chooseStore = async (storeId: number) => {
    const store = stores.find((s) => s.store_id === storeId)
    await api.post('/api/integrations/shipstation/store', {
      store_id: storeId,
      store_name: store?.store_name ?? null,
    })
    setSelectedStore(storeId)
    await onChange()
  }

  return (
    <div className="space-y-3">
      {status.connected ? <ConnectedHeader status={status} onDisconnect={disconnect} /> : null}
      {!status.connected || apiKey || apiSecret ? (
        <>
          <Field label="API key">
            <input
              className={inputClass}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </Field>
          <Field label="API secret">
            <input
              className={inputClass}
              type="password"
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
            />
          </Field>
          <Button
            variant="primary"
            onClick={save}
            disabled={busy || !apiKey || !apiSecret}
          >
            {busy ? 'Validating…' : 'Save & validate'}
          </Button>
        </>
      ) : (
        <Button size="sm" onClick={() => setApiKey(' ')}>
          Replace API credentials
        </Button>
      )}

      {stores.length ? (
        <Field
          label="Etsy store"
          hint="ShipStation imports Etsy orders through this store and pushes tracking back to Etsy."
        >
          <select
            className={inputClass}
            value={selectedStore ?? ''}
            onChange={(e) => chooseStore(Number(e.target.value))}
          >
            <option value="" disabled>
              Select a store…
            </option>
            {stores.map((store) => (
              <option key={store.store_id} value={store.store_id}>
                {store.store_name} — {store.marketplace}
              </option>
            ))}
          </select>
        </Field>
      ) : null}
      {status.connected && !selectedStore ? (
        <Alert tone="warning">Pick the ShipStation store that receives your Etsy orders.</Alert>
      ) : null}
      {error ? <Alert tone="error">{error}</Alert> : null}
    </div>
  )
}

export const PANELS: Record<string, (props: PanelProps) => JSX.Element> = {
  etsy: EtsyPanel,
  qbo: QboPanel,
  bambuddy: BambuddyPanel,
  shipstation: ShipStationPanel,
}
