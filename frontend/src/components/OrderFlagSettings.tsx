import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { FlagColor, OrderFlag } from '../lib/types'
import { FLAG_CLASSES, FLAG_DOTS } from './OrderFlags'
import { Alert, Button, Card, cx, inputClass } from './ui'

/* The shared input, minus its full width.
 *
 * `inputClass` is `w-full` and a flag's name is three words, so a row of
 * them would push the colours onto a line of their own. Taking the width
 * off is more honest than adding `w-40` after it and hoping the stylesheet
 * puts them in the order we want — between two width classes, the one that
 * wins is whichever Tailwind happened to emit last. */
const nameInputClass = cx(inputClass.replace('w-full ', ''), 'w-44 py-1 text-sm')

/** The shop's own vocabulary for marking an order.
 *
 * Every shop's is different — one needs "Reprint", another "Trade", another
 * "Chase the buyer" — so PrintFlow ships four and gets out of the way.
 *
 * Two things make this more than a list of strings. An order stores a flag's
 * **id**, so renaming "Rush" to "Priority" renames it on every order at once
 * rather than on none. And deleting a flag says first how many orders carry
 * it, because that is the number that makes it a decision instead of a
 * click — and because the orders keep it afterwards, so bringing the flag
 * back brings them back with it.
 */

interface FlagRow extends OrderFlag {
  /** How many orders carry it. Absent on one being invented right now. */
  orders?: number
  /** Local only, so a row with no id yet can still be a React key. */
  key: string
}

interface FlagsResponse {
  flags: (OrderFlag & { orders: number })[]
  orphans?: { id: string; orders: number }[]
  colors: FlagColor[]
  limit: number
  label_max: number
}

let nextKey = 0

export default function OrderFlagSettings() {
  const [rows, setRows] = useState<FlagRow[] | null>(null)
  const [saved, setSaved] = useState<FlagRow[]>([])
  const [meta, setMeta] = useState<FlagsResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.get<FlagsResponse>('/api/settings/order-flags')
      const loaded = data.flags.map((flag) => ({ ...flag, key: `k${nextKey++}` }))
      setRows(loaded)
      setSaved(loaded)
      setMeta(data)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!rows || !meta) {
    return (
      <Card className="p-4">
        <h2 className="text-sm font-semibold text-ink-900">Order flags</h2>
        {error ? (
          <div className="mt-2">
            <Alert tone="error">{error}</Alert>
          </div>
        ) : (
          <p className="mt-2 text-sm text-ink-500">Loading…</p>
        )}
      </Card>
    )
  }

  const set = (key: string, patch: Partial<FlagRow>) =>
    setRows((was) =>
      (was ?? []).map((row) => (row.key === key ? { ...row, ...patch } : row)),
    )

  const remove = (row: FlagRow) => {
    // Only a flag that is actually on orders needs asking about. Deleting one
    // nobody has used is not a decision, and a confirmation box for it is a
    // box people learn to click through.
    if (
      row.orders &&
      !window.confirm(
        `${row.orders} order${row.orders === 1 ? ' carries' : 's carry'} “${row.label}”. ` +
          'Deleting it stops the flag being shown on them — they keep it, so ' +
          'adding the flag back brings it back. Delete it?',
      )
    )
      return
    setRows((was) => (was ?? []).filter((entry) => entry.key !== row.key))
  }

  const dirty =
    rows.length !== saved.length ||
    rows.some((row, index) => {
      const was = saved[index]
      return (
        !was ||
        was.id !== row.id ||
        was.label !== row.label ||
        was.color !== row.color
      )
    })

  const save = async () => {
    setBusy(true)
    setNotice(null)
    try {
      const gone = saved.filter(
        (was) => !rows.some((row) => row.id && row.id === was.id),
      )
      const data = await api.put<FlagsResponse>('/api/settings/order-flags', {
        flags: rows.map((row) => ({
          // Only send an id the server gave us. A row being invented right
          // now has none, and inventing one here would be the screen
          // deciding what orders are going to be keyed on.
          id: row.id || undefined,
          label: row.label,
          color: row.color,
        })),
      })
      const fresh = data.flags.map((flag) => ({ ...flag, key: `k${nextKey++}` }))
      setRows(fresh)
      setSaved(fresh)
      setMeta(data)
      setError(null)
      const orphaned = gone.reduce((sum, row) => sum + (row.orders ?? 0), 0)
      setNotice(
        orphaned
          ? `Saved. ${orphaned} order${orphaned === 1 ? ' still carries' : 's still carry'} a ` +
            'flag you deleted — it is no longer drawn, and adding it back ' +
            'would show it again.'
          : 'Saved.',
      )
    } catch (err) {
      setError(errorMessage(err))
    }
    setBusy(false)
  }

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-sm font-semibold text-ink-900">Order flags</h2>
        <span className="text-xs text-ink-500">
          Your own words for marking an order. They show on the board card and
          in the order drawer.
        </span>
      </div>

      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {notice ? (
        <div className="mt-3">
          <Alert tone="info">{notice}</Alert>
        </div>
      ) : null}

      <ul className="mt-3 space-y-2">
        {rows.map((row) => (
          <li key={row.key} className="flex flex-wrap items-center gap-2">
            <input
              value={row.label}
              maxLength={meta.label_max}
              onChange={(event) => set(row.key, { label: event.target.value })}
              placeholder="Rush"
              aria-label="Flag name"
              className={nameInputClass}
            />
            <div className="flex items-center gap-1">
              {meta.colors.map((color) => (
                <button
                  key={color}
                  type="button"
                  aria-label={color}
                  aria-pressed={row.color === color}
                  title={color}
                  onClick={() => set(row.key, { color })}
                  className={cx(
                    'h-5 w-5 rounded-full transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-500',
                    FLAG_DOTS[color],
                    row.color === color
                      ? 'ring-2 ring-ink-900 ring-offset-1'
                      : 'opacity-50 hover:opacity-100',
                  )}
                />
              ))}
            </div>
            {/* What it will look like, next to the thing being typed. A
                colour named in a list is not the same as a colour seen on
                the word it is going to colour. */}
            <span
              className={cx(
                'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1',
                FLAG_CLASSES[row.color] ?? FLAG_CLASSES.slate,
              )}
            >
              {row.label.trim() || 'Unnamed'}
            </span>
            <span className="text-xs text-ink-400">
              {row.orders === undefined
                ? 'new'
                : row.orders === 0
                  ? 'on no orders'
                  : `on ${row.orders} order${row.orders === 1 ? '' : 's'}`}
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto text-red-700"
              onClick={() => remove(row)}
            >
              Delete
            </Button>
          </li>
        ))}
        {rows.length === 0 ? (
          <li className="text-sm text-ink-500">
            No flags. Nothing breaks without them — the board simply offers
            none.
          </li>
        ) : null}
      </ul>

      {meta.orphans?.length ? (
        <p className="mt-3 text-xs text-ink-500">
          {meta.orphans.reduce((sum, row) => sum + row.orders, 0)} order
          {meta.orphans.reduce((sum, row) => sum + row.orders, 0) === 1
            ? ' carries'
            : 's carry'}{' '}
          a flag that is no longer on this list (
          {meta.orphans.map((row) => row.id).join(', ')}). Nothing draws it,
          and nothing has been lost.
        </p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={rows.length >= meta.limit}
          onClick={() =>
            setRows((was) => [
              ...(was ?? []),
              { key: `k${nextKey++}`, id: '', label: '', color: 'slate' },
            ])
          }
        >
          Add a flag
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={busy || !dirty || rows.some((row) => !row.label.trim())}
          onClick={save}
        >
          {busy ? 'Saving…' : 'Save flags'}
        </Button>
        {dirty ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => setRows(saved)}
          >
            Undo changes
          </Button>
        ) : null}
        {rows.length >= meta.limit ? (
          <span className="text-xs text-ink-500">
            {meta.limit} is as many as a card can carry legibly.
          </span>
        ) : null}
      </div>
    </Card>
  )
}
