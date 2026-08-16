import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api, errorMessage } from '../lib/api'
import { PROVIDER_LABELS } from '../lib/format'
import type { IntegrationStatus } from '../lib/types'
import {
  ActivityIcon,
  BoardIcon,
  CollapseIcon,
  CalculatorIcon,
  ManufacturingIcon,
  MenuIcon,
  PrinterIcon,
  OrdersIcon,
  ProductsIcon,
  SettingsIcon,
} from './NavIcons'
import { Alert, Button, cx } from './ui'

const NAV = [
  { to: '/', label: 'Board', end: true, Icon: BoardIcon },
  { to: '/orders', label: 'Orders', Icon: OrdersIcon },
  { to: '/products', label: 'Products', Icon: ProductsIcon },
  { to: '/printers', label: 'Printers', Icon: PrinterIcon },
  { to: '/manufacturing', label: 'Manufacturing', Icon: ManufacturingIcon },
  { to: '/calculator', label: 'Calculator', Icon: CalculatorIcon },
  { to: '/sync-log', label: 'Sync Log', Icon: ActivityIcon },
  { to: '/settings', label: 'Settings', Icon: SettingsIcon },
]

const COLLAPSE_KEY = 'printflow.sidebar.collapsed'

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

function SidebarNav({
  collapsed,
  onNavigate,
}: {
  collapsed: boolean
  onNavigate?: () => void
}) {
  return (
    <nav className="flex-1 space-y-0.5 px-2 py-3">
      {NAV.map(({ to, label, end, Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          title={collapsed ? label : undefined}
          className={({ isActive }) =>
            cx(
              'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition',
              collapsed && 'justify-center px-0',
              isActive
                ? 'bg-ink-800 text-white'
                : 'text-ink-300 hover:bg-ink-800/60 hover:text-white',
            )
          }
        >
          <Icon />
          {collapsed ? <span className="sr-only">{label}</span> : <span>{label}</span>}
        </NavLink>
      ))}
    </nav>
  )
}

function SidebarFooter({
  collapsed,
  username,
  onSignOut,
}: {
  collapsed: boolean
  username?: string
  onSignOut: () => void
}) {
  return (
    <div className="border-t border-ink-800 px-2 py-3">
      {collapsed ? (
        <button
          type="button"
          onClick={onSignOut}
          title={`Sign out${username ? ` (${username})` : ''}`}
          className="mx-auto flex h-8 w-8 items-center justify-center rounded-full bg-ink-800 text-xs font-semibold uppercase text-ink-200 hover:bg-ink-700"
        >
          {(username ?? '?').slice(0, 1)}
        </button>
      ) : (
        <div className="flex items-center gap-2 px-1">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ink-800 text-xs font-semibold uppercase text-ink-200">
            {(username ?? '?').slice(0, 1)}
          </span>
          <span className="min-w-0 flex-1 truncate text-xs text-ink-400">{username}</span>
          <button
            type="button"
            onClick={onSignOut}
            className="rounded px-1.5 py-1 text-xs font-medium text-ink-300 hover:bg-ink-800 hover:text-white"
          >
            Sign out
          </button>
        </div>
      )}
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
  const location = useLocation()
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === '1',
  )

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0')
  }, [collapsed])

  // The drawer is an overlay, so anything that changes the page closes it.
  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  const signOut = async () => {
    await api.post('/api/auth/logout')
    navigate('/')
    onSignedOut()
  }

  const current = NAV.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
  )

  return (
    <div className="flex h-full">
      {/* Sidebar — static from sm up, off-canvas drawer below it. */}
      <aside
        className={cx(
          'fixed inset-y-0 left-0 z-40 flex w-60 flex-col bg-ink-900 transition-transform duration-200',
          'sm:static sm:z-auto sm:translate-x-0 sm:transition-[width]',
          drawerOpen ? 'translate-x-0' : '-translate-x-full',
          collapsed ? 'sm:w-16' : 'sm:w-56',
        )}
      >
        <div
          className={cx(
            'flex h-12 items-center gap-2 border-b border-ink-800 px-3',
            collapsed && 'sm:justify-center sm:px-0',
          )}
        >
          {collapsed ? null : (
            <span className="text-sm font-semibold tracking-tight text-white">
              PrintFlow
            </span>
          )}
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className="ml-auto hidden rounded p-1 text-ink-400 hover:bg-ink-800 hover:text-white sm:block"
          >
            <CollapseIcon collapsed={collapsed} className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={() => setDrawerOpen(false)}
            aria-label="Close menu"
            className="ml-auto rounded p-1 text-ink-400 hover:bg-ink-800 hover:text-white sm:hidden"
          >
            ✕
          </button>
        </div>

        <SidebarNav collapsed={collapsed} onNavigate={() => setDrawerOpen(false)} />
        <SidebarFooter collapsed={collapsed} username={username} onSignOut={signOut} />
      </aside>

      {drawerOpen ? (
        <div
          className="fixed inset-0 z-30 bg-ink-900/50 sm:hidden"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile-only bar: opens the drawer and names the current screen. */}
        <header className="flex h-12 items-center gap-2 border-b border-ink-200 bg-white px-2 sm:hidden">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open menu"
            className="rounded p-1.5 text-ink-600 hover:bg-ink-100"
          >
            <MenuIcon />
          </button>
          <span className="text-sm font-semibold text-ink-900">
            {current?.label ?? 'PrintFlow'}
          </span>
        </header>

        <IntegrationBanners />
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  )
}
