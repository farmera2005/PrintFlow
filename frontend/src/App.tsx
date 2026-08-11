import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import Login from './pages/Login'
import SetupWizard from './pages/SetupWizard'
import Board from './pages/Board'
import Orders from './pages/Orders'
import Products from './pages/Products'
import Printers from './pages/Printers'
import Manufacturing from './pages/Manufacturing'
import Settings from './pages/Settings'
import SyncLog from './pages/SyncLog'
import { api } from './lib/api'
import { Spinner } from './components/ui'
import type { SetupStatus } from './lib/types'

interface Session {
  authenticated: boolean
  username?: string
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [setup, setSetup] = useState<SetupStatus | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const [me, status] = await Promise.all([
      api.get<Session>('/api/auth/me'),
      api.get<SetupStatus>('/api/setup/status'),
    ])
    setSession(me)
    setSetup(status)
  }, [])

  useEffect(() => {
    refresh()
      .catch(() => setSession({ authenticated: false }))
      .finally(() => setLoading(false))
  }, [refresh])

  if (loading || !setup) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  // First run: nobody has created the admin account yet.
  if (!setup.admin_exists) {
    return <SetupWizard status={setup} onChange={refresh} />
  }

  if (!session?.authenticated) {
    return <Login onSignedIn={refresh} />
  }

  if (!setup.setup_complete) {
    return <SetupWizard status={setup} onChange={refresh} />
  }

  return (
    <AppShell username={session.username} onSignedOut={refresh}>
      <Routes>
        <Route path="/" element={<Board />} />
        <Route path="/orders" element={<Orders />} />
        <Route path="/products" element={<Products />} />
        <Route path="/printers" element={<Printers />} />
        {/* The flat queue was replaced by the farm view; anything
            bookmarked still lands somewhere useful. */}
        <Route path="/print-queue" element={<Navigate to="/printers" replace />} />
        <Route path="/manufacturing" element={<Manufacturing />} />
        <Route path="/sync-log" element={<SyncLog />} />
        <Route path="/settings" element={<Settings onChange={refresh} />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}
