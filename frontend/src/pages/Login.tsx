import { useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { Alert, Button, Card, Field, inputClass } from '../components/ui'

export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/auth/login', { username, password })
      onSignedIn()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold text-ink-900">PrintFlow</h1>
        <p className="mt-1 text-sm text-ink-500">Sign in to continue.</p>
        <form className="mt-5 space-y-4" onSubmit={submit}>
          <Field label="Username">
            <input
              className={inputClass}
              value={username}
              autoComplete="username"
              autoFocus
              onChange={(e) => setUsername(e.target.value)}
            />
          </Field>
          <Field label="Password">
            <input
              className={inputClass}
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          {error ? <Alert tone="error">{error}</Alert> : null}
          <Button type="submit" variant="primary" className="w-full" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </Button>
        </form>
      </Card>
    </div>
  )
}
