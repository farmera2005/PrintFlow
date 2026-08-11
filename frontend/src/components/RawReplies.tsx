import { useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { BambuddyRawListing } from '../lib/types'
import { Alert, Button } from './ui'

/** What Bambuddy actually replied, verbatim.
 *
 * Always reachable, not only when something is visibly broken. Every one of
 * these instances is self-hosted and none of them are quite the same shape, so
 * an answer that is subtly wrong — a folder in the wrong place, a printer card
 * with no temperature on it — cannot be diagnosed from the outside at all. It
 * can be shown, and that turns a round of guessing into one screenshot.
 */
export default function RawReplies({
  endpoint,
  label = 'Show what Bambuddy sent',
}: {
  endpoint: string
  label?: string
}) {
  const [raw, setRaw] = useState<BambuddyRawListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const show = () => {
    setBusy(true)
    api
      .get<BambuddyRawListing>(endpoint)
      .then((data) => {
        setRaw(data)
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }

  return (
    <div className="space-y-2">
      {!raw ? (
        <Button size="sm" variant="ghost" onClick={show} disabled={busy}>
          {busy ? 'Asking…' : label}
        </Button>
      ) : (
        <Button size="sm" variant="ghost" onClick={() => setRaw(null)}>
          Hide what Bambuddy sent
        </Button>
      )}
      {error ? <Alert tone="error">{error}</Alert> : null}
      {raw?.probes.map((probe) => (
        <div key={probe.endpoint + (probe.body ?? '')} className="space-y-1">
          <p className="text-xs text-ink-500">
            <span className="font-mono">{probe.endpoint}</span>
            {probe.error
              ? ` failed: ${probe.error}`
              : ` returned ${probe.kind}` +
                (probe.keys?.length ? ` with keys: ${probe.keys.join(', ')}` : '') +
                ` — ${probe.rows_found} row${probe.rows_found === 1 ? '' : 's'} read` +
                (probe.total !== null && probe.total !== undefined
                  ? ` of ${probe.total}`
                  : '') +
                '.'}
          </p>
          {probe.body ? (
            <pre className="max-h-48 overflow-auto rounded-md bg-ink-900 p-2 text-[11px] leading-snug text-ink-100">
              {probe.body}
              {probe.body_truncated ? '\n… (truncated)' : ''}
            </pre>
          ) : null}
        </div>
      ))}
    </div>
  )
}
