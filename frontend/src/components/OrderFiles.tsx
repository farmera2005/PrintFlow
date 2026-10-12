import { useRef, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { OrderFile } from '../lib/types'
import { Alert, Button, EmptyState, cx, inputClass } from './ui'

/** The files somebody put on an order.
 *
 * A customer's model, a photo of the proof they approved, a trade buyer's
 * purchase order, the screenshot of the message where a change was agreed.
 * Until now all of that lived in whoever's inbox it arrived in, which is the
 * one place a person packing the order cannot reach.
 *
 * Two things here are deliberate and worth not undoing:
 *
 * **Downloads are links, not fetches.** An anchor straight at the endpoint
 * lets the browser stream an eighty-megabyte model to disk; pulling it through
 * JavaScript into a blob first would hold the whole thing in a tab's memory to
 * achieve the same save dialog.
 *
 * **Previews only for what the server will show inline.** The server decides
 * which types are safe to render and says so per file; this only draws a
 * thumbnail where it already said yes, rather than guessing from the name.
 */

/** What the browser is told it may hand over. Not a security control — the
 *  server has its own cap and its own rules — just a nudge away from picking
 *  something it will refuse. */
const PROMPT = '.stl,.3mf,.obj,.step,.stp,.gcode,.pdf,.png,.jpg,.jpeg,.webp,.gif,.csv,.txt,.zip'

export default function OrderFiles({
  orderId,
  files,
  onChanged,
}: {
  orderId: string
  files: OrderFile[]
  onChanged: () => void | Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const picker = useRef<HTMLInputElement>(null)

  const send = async (chosen: FileList | File[] | null) => {
    const list = Array.from(chosen ?? [])
    if (!list.length) return
    setBusy(true)
    setError(null)
    try {
      // One at a time, so the progress bar means something and so a refusal
      // names the file it is about.
      for (const file of list) {
        const form = new FormData()
        form.append('file', file)
        await api.upload(`/api/orders/${orderId}/files`, form, setProgress)
      }
      await onChanged()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
      setProgress(0)
      if (picker.current) picker.current.value = ''
    }
  }

  const remove = async (file: OrderFile) => {
    if (!window.confirm(`Delete ${file.filename}? This cannot be undone.`)) return
    try {
      await api.del(`/api/orders/${orderId}/files/${file.id}`)
      await onChanged()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const saveNote = async (file: OrderFile) => {
    try {
      await api.patch(`/api/orders/${orderId}/files/${file.id}`, { note: draft })
      setEditing(null)
      await onChanged()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  return (
    <section className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Files
        </h3>
        <span className="text-xs text-ink-500">
          Kept with the order and carried in the backup.
        </span>
      </div>

      {/* A drop target that is also a button, because both are how people
          expect to do this and neither is obvious on its own. */}
      <div
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          send(event.dataTransfer.files)
        }}
        className={cx(
          'mt-2 rounded-lg border-2 border-dashed px-3 py-4 text-center',
          dragging ? 'border-ink-500 bg-ink-50' : 'border-ink-200',
        )}
      >
        <input
          ref={picker}
          type="file"
          multiple
          accept={PROMPT}
          className="hidden"
          onChange={(event) => send(event.target.files)}
        />
        <p className="text-sm text-ink-600">
          Drop a file here, or{' '}
          <button
            type="button"
            className="font-medium text-ink-900 underline"
            onClick={() => picker.current?.click()}
            disabled={busy}
          >
            choose one
          </button>
          .
        </p>
        <p className="mt-0.5 text-xs text-ink-500">
          Models, photos, purchase orders — up to 100 MB each.
        </p>
        {busy ? (
          <div className="mx-auto mt-2 h-1.5 w-48 overflow-hidden rounded-full bg-ink-200">
            <div
              className="h-full rounded-full bg-ink-900 transition-all"
              style={{ width: `${Math.round(progress * 100)}%` }}
            />
          </div>
        ) : null}
      </div>

      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}

      {files.length === 0 ? (
        <div className="mt-2">
          <EmptyState
            title="Nothing attached yet"
            description="The model a customer sent, the proof they approved, the purchase order behind the job."
          />
        </div>
      ) : (
        <ul className="mt-2 divide-y divide-ink-200">
          {files.map((file) => (
            <li key={file.id} className="flex flex-wrap items-center gap-3 py-2">
              {file.previewable && !file.missing ? (
                <img
                  src={`/api/orders/${orderId}/files/${file.id}`}
                  alt={file.filename}
                  className="h-12 w-12 rounded object-cover ring-1 ring-ink-200"
                />
              ) : (
                <span className="flex h-12 w-12 items-center justify-center rounded bg-ink-100 text-[10px] font-semibold uppercase text-ink-500">
                  {extensionOf(file.filename)}
                </span>
              )}

              <div className="min-w-0 flex-1">
                <a
                  className="block truncate text-sm font-medium text-ink-900 underline"
                  href={`/api/orders/${orderId}/files/${file.id}`}
                  // A plain link: the browser streams it to disk rather than
                  // this tab holding the whole file in memory first.
                  download={file.filename}
                >
                  {file.filename}
                </a>
                <p className="text-xs text-ink-500">
                  {file.size_label}
                  {file.uploaded_by ? ` · ${file.uploaded_by}` : ''}
                  {file.created_at ? ` · ${formatDateTime(file.created_at)}` : ''}
                </p>
                {file.missing ? (
                  <p className="text-xs text-red-700">
                    Recorded here, but the file is not in the data directory —
                    restored from a backup that did not carry it, or deleted off
                    the disk.
                  </p>
                ) : null}

                {editing === file.id ? (
                  <div className="mt-1 flex flex-wrap items-center gap-2">
                    <input
                      className={`${inputClass} max-w-xs`}
                      value={draft}
                      autoFocus
                      placeholder="What is this file for?"
                      onChange={(event) => setDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') saveNote(file)
                        if (event.key === 'Escape') setEditing(null)
                      }}
                    />
                    <Button size="sm" variant="primary" onClick={() => saveNote(file)}>
                      Save
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing(null)}>
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <button
                    type="button"
                    className="mt-0.5 text-xs text-ink-600 underline"
                    onClick={() => {
                      setEditing(file.id)
                      setDraft(file.note ?? '')
                    }}
                  >
                    {file.note || 'Add a note'}
                  </button>
                )}
              </div>

              <Button size="sm" variant="ghost" onClick={() => remove(file)}>
                Delete
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** The extension, for the tile that stands in for a thumbnail. */
function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  const suffix = dot > 0 ? name.slice(dot + 1) : ''
  return suffix.length && suffix.length <= 5 ? suffix : 'file'
}
