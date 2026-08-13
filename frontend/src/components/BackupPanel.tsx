import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { Alert, Button, Field, Spinner, cx, inputClass } from './ui'

/** What a backup would contain, so the button is not a leap of faith. */
interface BackupStatus {
  tables: Record<string, number>
  rows: number
  data_files: string[]
  alembic_revision: string | null
  format: number
  /** "file", "environment" or "generated". Only a key in a file can travel. */
  secret_key_source: string
  carries_secret_key: boolean
}

/** What is in a file somebody is about to restore from. */
export interface BackupPreview {
  manifest: {
    created_at?: string
    alembic_revision?: string | null
    git_sha?: string
    encrypted?: boolean
    format?: number
  }
  rows: number
  tables: Record<string, number>
  data_files: string[]
  /** Set when the file can be read but not restored by this build. */
  problem: string | null
}

/** Rows worth naming on a summary. The rest are counted but not listed —
 *  "1 app_settings" is not what anybody is checking for. */
const NAMED: [string, string][] = [
  ['orders', 'orders'],
  ['order_lines', 'order lines'],
  ['products', 'products'],
  ['print_jobs', 'plates'],
  ['made_sheets', 'made sheets'],
  ['integration_credentials', 'connections'],
  ['users', 'accounts'],
]

export function summarise(tables: Record<string, number>): string {
  const parts = NAMED.filter(([key]) => tables[key]).map(
    ([key, label]) => `${tables[key]} ${label}`,
  )
  return parts.length ? parts.join(' · ') : 'nothing yet'
}

/** Reading a file the browser already has, without a round trip. */
function useFile() {
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  return { input, file, setFile }
}

/** Restore: pick a file, look inside it, then replace everything.
 *
 *  The look-first step is not decoration. Restoring is the most destructive
 *  thing PrintFlow can do and the file is opaque — a name and a size say
 *  nothing about which day's orders are in it. So the file is opened and
 *  described before anything is replaced, and the description is what the
 *  confirmation is attached to. */
export function RestorePanel({
  endpoint,
  onRestored,
  requireTypedConfirmation,
  danger,
}: {
  /** "/api/backup" when signed in, "/api/setup/restore" during setup. */
  endpoint: string
  onRestored: (result: any) => void | Promise<void>
  requireTypedConfirmation: boolean
  /** What is about to be lost. Empty on a fresh install, where nothing is. */
  danger: string | null
}) {
  const { input, file, setFile } = useFile()
  const [passphrase, setPassphrase] = useState('')
  const [preview, setPreview] = useState<BackupPreview | null>(null)
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState<'inspect' | 'restore' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const inspectUrl = endpoint === '/api/backup' ? '/api/backup/inspect' : `${endpoint}/inspect`
  const restoreUrl = endpoint === '/api/backup' ? '/api/backup/restore' : endpoint

  const choose = (picked: File | null) => {
    setFile(picked)
    setPreview(null)
    setConfirm('')
    setError(null)
  }

  const inspect = async () => {
    if (!file) return
    setBusy('inspect')
    setError(null)
    try {
      const form = new FormData()
      form.append('archive', file)
      form.append('passphrase', passphrase)
      setPreview(await api.postForm<BackupPreview>(inspectUrl, form))
    } catch (err) {
      setError(errorMessage(err))
      setPreview(null)
    } finally {
      setBusy(null)
    }
  }

  const restore = async () => {
    if (!file) return
    setBusy('restore')
    setError(null)
    try {
      const form = new FormData()
      form.append('archive', file)
      form.append('passphrase', passphrase)
      if (requireTypedConfirmation) form.append('confirm', confirm)
      await onRestored(await api.postForm<any>(restoreUrl, form))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const ready =
    preview !== null &&
    preview.problem === null &&
    (!requireTypedConfirmation || confirm.trim().toLowerCase() === 'restore')

  return (
    <div className="space-y-3">
      <Field label="Backup file" hint="The .tar.gz or .pfbackup file you saved.">
        <input
          ref={input}
          type="file"
          accept=".gz,.tar.gz,.pfbackup,application/octet-stream"
          className="block w-full text-sm text-ink-700 file:mr-3 file:rounded-md file:border-0 file:bg-ink-100 file:px-3 file:py-1.5 file:text-sm file:text-ink-800 hover:file:bg-ink-200"
          onChange={(event) => choose(event.target.files?.[0] ?? null)}
        />
      </Field>

      <Field
        label="Passphrase"
        hint="Only if the backup was made with one. There is no way in without it."
      >
        <input
          className={inputClass}
          type="password"
          value={passphrase}
          onChange={(event) => {
            setPassphrase(event.target.value)
            setPreview(null)
          }}
        />
      </Field>

      <div className="flex flex-wrap gap-2">
        <Button onClick={inspect} disabled={!file || busy !== null}>
          {busy === 'inspect' ? 'Opening…' : 'Open and check'}
        </Button>
        {preview && !preview.problem ? (
          <Button variant="primary" onClick={restore} disabled={!ready || busy !== null}>
            {busy === 'restore' ? 'Restoring…' : 'Restore this backup'}
          </Button>
        ) : null}
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {preview ? (
        <div className="space-y-2 rounded-md bg-ink-50 p-3">
          <p className="text-sm text-ink-800">
            Taken {formatDateTime(preview.manifest.created_at ?? null)}
            {preview.manifest.encrypted ? ' · encrypted' : ''}
          </p>
          <p className="text-sm text-ink-700">{summarise(preview.tables)}</p>
          <p className="text-xs text-ink-500">
            {preview.rows} rows in total ·{' '}
            {preview.data_files.length
              ? `${preview.data_files.length} file${
                  preview.data_files.length === 1 ? '' : 's'
                } from the data directory, including the encryption key`
              : 'no data-directory files — stored credentials will not be readable'}
          </p>
          {preview.problem ? <Alert tone="error">{preview.problem}</Alert> : null}
        </div>
      ) : null}

      {preview && !preview.problem && requireTypedConfirmation ? (
        <div className="space-y-2">
          <Alert tone="warning">
            This replaces everything on this install{danger ? ` — ${danger}` : ''}. There
            is no undo.
          </Alert>
          <Field label='Type "restore" to confirm'>
            <input
              className={inputClass}
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              placeholder="restore"
            />
          </Field>
        </div>
      ) : null}
    </div>
  )
}

/** Backup & restore, for the Settings screen. */
export default function BackupPanel({ onRestored }: { onRestored: () => void | Promise<void> }) {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    api
      .get<BackupStatus>('/api/backup/status')
      .then(setStatus)
      .catch((err) => setError(errorMessage(err)))
  }, [])

  const download = async () => {
    setBusy(true)
    setError(null)
    try {
      // Downloaded through fetch rather than a link so the passphrase travels
      // in the body: in a query string it would sit in the browser's history
      // and in every proxy log between here and the shop.
      const blob = await api.postBlob('/api/backup/download', { passphrase })
      const url = URL.createObjectURL(blob.body)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = blob.filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-ink-900">Backup</h2>
        <p className="mt-1 text-sm text-ink-600">
          One file with everything in it: every order, product, plate and figure,
          plus the key your stored credentials are encrypted with. Keep it
          somewhere other than this machine — a backup on the machine that died
          is not a backup.
        </p>
        {status ? (
          <p className="mt-2 text-xs text-ink-500">
            Right now that is {summarise(status.tables)} — {status.rows} rows —
            and {status.data_files.length} file
            {status.data_files.length === 1 ? '' : 's'} from the data directory.
          </p>
        ) : (
          <Spinner className="mt-2 h-4 w-4" />
        )}
      </div>

      <Field
        label="Passphrase (optional)"
        hint={
          passphrase
            ? 'The file will be unreadable without this. There is no recovery — if you lose it, the backup is gone.'
            : 'Without one, anybody holding the file holds your Etsy, QuickBooks and ShipStation credentials in the clear.'
        }
      >
        <input
          className={inputClass}
          type="password"
          value={passphrase}
          onChange={(event) => setPassphrase(event.target.value)}
        />
      </Field>

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={download} disabled={busy || !status}>
          {busy ? 'Building…' : 'Download backup'}
        </Button>
        {!passphrase ? (
          <span className="text-xs text-amber-800">
            Unencrypted — contains your credentials in readable form.
          </span>
        ) : null}
      </div>

      {/* The one way a backup can be quietly incomplete. When SECRET_KEY is
          set in the environment there is no key file to carry, so the archive
          holds credentials nothing can decrypt — and the restore looks fine
          until every integration fails at once. Better said now. */}
      {status && !status.carries_secret_key ? (
        <Alert tone="warning">
          {status.secret_key_source === 'environment' ? (
            <>
              This install takes <code>SECRET_KEY</code> from its environment, so
              the backup cannot carry it — the credentials in the file are
              encrypted with a key that is in your compose file, not in here.
              Keep that value with the backup. Restoring without it comes up
              with every connection unreadable.
            </>
          ) : (
            <>
              No encryption key was found in the data directory, so the stored
              credentials in this backup will not be readable after a restore.
              Everything else — orders, products, plates, money — restores
              normally.
            </>
          )}
        </Alert>
      ) : null}

      <div className="border-t border-ink-200 pt-4">
        <button
          type="button"
          className={cx(
            'text-sm font-semibold',
            open ? 'text-ink-900' : 'text-ink-700 underline decoration-ink-300',
          )}
          onClick={() => setOpen((was) => !was)}
        >
          {open ? 'Restore from a backup' : 'Restore from a backup…'}
        </button>
        {open ? (
          <div className="mt-3">
            <RestorePanel
              endpoint="/api/backup"
              requireTypedConfirmation
              danger={
                status
                  ? `the ${summarise(status.tables)} currently here`
                  : 'everything currently here'
              }
              onRestored={async () => {
                await onRestored()
                // The accounts came from the backup, so the session that did
                // this may belong to a user who no longer exists. Reloading is
                // the honest end of a restore rather than leaving a screen
                // that is describing a shop that has been replaced.
                window.location.reload()
              }}
            />
          </div>
        ) : null}
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}
    </div>
  )
}
