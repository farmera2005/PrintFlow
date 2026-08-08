import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../lib/api'
import { PROVIDER_LABELS } from '../lib/format'
import type { IntegrationStatus } from '../lib/types'
import { Alert, Button, cx } from './ui'

const NAV = [
  { to: '/', label: 'Board', end: true },
  { to: '/products', label: 'Products' },
  { to: '/print-queue', label: 'Print Queue' },
  { to: '/sync-log', label: 'Sync Log' },
  { to: '/settings', label: 'Settings' },
]

/**
 * Persistent per-integration failure banner (§6). It polls independently of the
 * page content so a failure surfaces even while sitting on the board.
 */
export function IntegrationBanners() {
  const [statuses, setStatuses] = useState<IntegrationStatus[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const load = async () => {
    try {
      const data = await api.get<{ integrations: IntegrationStatus[] }>('/api/settings')
      setStatuses(data.integrations)
    } catch {
      /* the page itself will report a hard failure */
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 60_000)
    return () => clearInterval(timer)
  }, [])

  const retry = async (provider: string) => {
    setBusy(provider)
    setNote(null)
    try {
      const result = await api.post<{ ok: boolean; error?: string }>(
        `/api/integrations/${provider}/retry`,
      )
      setNote(
        result.ok
          ? `${PROVIDER_LABELS[provider]} retry finished.`
          : `${PROVIDER_LABELS[provider]} retry failed: ${result.error}`,
      )
      await load()
    } catch (error) {
      setNote(errorMessage(error))
    } finally {
      setBusy(null)
    }
  }

  const failing = statuses.filter((s) => s.connected && s.last_error)
  const broken = statuses.filter((s) => s.undecryptable)

  if (!failing.length && !broken.length && !note) return null

  return (
    <div className="space-y-2 px-3 pt-3 sm:px-6">
      {broken.map((status) => (
        <Alert key={status.provider} tone="error">
          <strong>{PROVIDER_LABELS[status.provider]}</strong> credentials cannot be
          decrypted — SECRET_KEY has changed. Reconnect it in Settings.
        </Alert>
      ))}
      {failing.map((status) => (
        <Alert
          key={status.provider}
          tone="warning"
          action={
            <Button
              size="sm"
              onClick={() => retry(status.provider)}
              disabled={busy === status.provider}
            >
              {busy === status.provider ? 'Retrying…' : 'Retry now'}
            </Button>
          }
        >
          <strong>{PROVIDER_LABELS[status.provider]}</strong>: {status.last_error}
        </Alert>
      ))}
      {note ? <Alert tone="info">{note}</Alert> : null}
    </div>
  )
}

export default function AppShell({
  username,
  onSignedOut,
  children,
}: {
  username?: string
  onSignedOut: () => void
  children: ReactNode
}) {
  const navigate = useNavigate()
  const [menuOpen, setMenuOpen] = useState(false)

  const signOut = async () => {
    await api.post('/api/auth/logout')
    navigate('/')
    onSignedOut()
  }

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-ink-200 bg-white">
        <div className="flex items-center gap-3 px-3 py-2.5 sm:px-6">
          <span className="text-sm font-semibold tracking-tight text-ink-900">
            PrintFlow
          </span>
          <nav className="hidden flex-1 items-center gap-1 sm:flex">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cx(
                    'rounded-md px-2.5 py-1.5 text-sm font-medium transition',
                    isActive
                      ? 'bg-ink-100 text-ink-900'
                      : 'text-ink-600 hover:bg-ink-50 hover:text-ink-900',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <span className="hidden text-xs text-ink-500 sm:inline">{username}</span>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="sm:hidden"
              onClick={() => setMenuOpen((open) => !open)}
              aria-label="Menu"
            >
              ☰
            </Button>
          </div>
        </div>
        {menuOpen ? (
          <nav className="grid gap-1 border-t border-ink-200 px-3 py-2 sm:hidden">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                onClick={() => setMenuOpen(false)}
                className={({ isActive }) =>
                  cx(
                    'rounded-md px-2.5 py-2 text-sm font-medium',
                    isActive ? 'bg-ink-100 text-ink-900' : 'text-ink-600',
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        ) : null}
      </header>
      <IntegrationBanners />
      <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
    </div>
  )
}
