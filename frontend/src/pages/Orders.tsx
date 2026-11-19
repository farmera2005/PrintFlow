import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  SOURCE_CLASSES,
  SOURCE_LABELS,
  formatDateTime,
  sourcesWorthShowing,
} from '../lib/format'
import type { Order, OrderStatus } from '../lib/types'
import InvoiceAction from '../components/InvoiceAction'
import NewOrderDialog from '../components/NewOrderDialog'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, Button, Card, EmptyState, Spinner, cx, inputClass } from '../components/ui'

/** Every order, including the ones the board does not draw.
 *
 * A cancelled order never appears on the board, a shipped one scrolls away,
 * and a delivered one is taken off it two days after it arrives. None of them
 * are deleted — deleting an order would delete what it earned and what it
 * cost — so this is the way back to any of them, the same drawer, reachable
 * by number or buyer.
 */

const STATUS_CLASSES: Record<OrderStatus, string> = {
  new: 'bg-ink-100 text-ink-700 ring-ink-300',
  in_production: 'bg-amber-100 text-amber-800 ring-amber-300',
  assembly: 'bg-violet-100 text-violet-800 ring-violet-300',
  ready_to_ship: 'bg-indigo-100 text-indigo-800 ring-indigo-300',
  shipped: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  complete: 'bg-emerald-100 text-emerald-900 ring-emerald-400',
  cancelled: 'bg-red-100 text-red-800 ring-red-300',
}

const FILTERS: { key: OrderStatus | 'all'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'new', label: COLUMN_LABELS.new },
  { key: 'in_production', label: COLUMN_LABELS.in_production },
  { key: 'assembly', label: COLUMN_LABELS.assembly },
  { key: 'ready_to_ship', label: COLUMN_LABELS.ready_to_ship },
  { key: 'shipped', label: COLUMN_LABELS.shipped },
  { key: 'complete', label: COLUMN_LABELS.complete },
  { key: 'cancelled', label: 'Cancelled' },
]

interface OrdersResponse {
  orders: (Order & {
    /** Why this one is in the results, when the reason is not already on
     *  the row — a match on a note or on a line nobody can see from here.
     *  Absent when the match is self-evident. */
    matched_on?: string[]
  })[]
  /** What the search understood: the words, how many matched, and whether
   *  the query was longer than it is willing to honour. */
  search: { terms: string[]; results: number; trimmed: boolean }
  counts: Record<string, number>
  total: number
}

export default function Orders() {
  const [data, setData] = useState<OrdersResponse | null>(null)
  // What is in the box, and what has actually been asked for. Two states
  // rather than one so the box never lags a keystroke behind the person
  // typing into it — see the debounce below.
  const [typed, setTyped] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<OrderStatus | 'all'>('all')
  const [error, setError] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const searchBox = useRef<HTMLInputElement>(null)

  // A search that now reads six fields and the lines of every order is not
  // one to run per keystroke. A quarter of a second is below what anybody
  // reads as a delay and above the gap between two letters.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(typed), 250)
    return () => window.clearTimeout(timer)
  }, [typed])

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams({ q: query, limit: '200' })
      if (filter !== 'all') params.set('status_filter', filter)
      setData(await api.get<OrdersResponse>(`/api/orders?${params}`))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [query, filter])

  useEffect(() => {
    load()
  }, [load])

  // "/" puts the cursor in the box, the way every list people already use
  // does. Ignored while they are typing into something else, which is the
  // one thing that makes the shortcut a nuisance rather than a help.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const typingElsewhere =
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target?.isContentEditable
      if (event.key === '/' && !typingElsewhere) {
        event.preventDefault()
        searchBox.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Orders</h1>
          <div className="relative max-w-sm flex-1">
            <input
              ref={searchBox}
              className={`${inputClass} pr-16`}
              placeholder="Search orders…"
              aria-label="Search orders"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(event) => {
                // Escape clears rather than blurs: the thing somebody wants
                // back is the whole list, and reaching for the X with a
                // hand already on the keyboard is the slower way to it.
                if (event.key === 'Escape' && typed) {
                  event.preventDefault()
                  setTyped('')
                }
              }}
            />
            {typed ? (
              <button
                type="button"
                onClick={() => {
                  setTyped('')
                  searchBox.current?.focus()
                }}
                aria-label="Clear the search"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-xs text-ink-400 hover:bg-ink-100 hover:text-ink-700"
              >
                Clear
              </button>
            ) : (
              <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 rounded border border-ink-200 px-1 text-[10px] font-medium text-ink-400">
                /
              </span>
            )}
          </div>
          {data ? (
            <span className="text-sm text-ink-500">
              {/* What was actually asked, and what came back. A count of
                  everything is the wrong number to show beside a search. */}
              {data.search.terms.length
                ? `${data.search.results}${
                    data.search.results === 200 ? '+' : ''
                  } of ${data.total}`
                : `${data.total} in total`}
            </span>
          ) : null}
          {/* Here rather than on the board: the board is for orders that
              already exist, and this is the list that owns every order
              whatever its status. */}
          <Button className="ml-auto" variant="primary" onClick={() => setAdding(true)}>
            New order
          </Button>
        </div>

        <div className="flex flex-wrap gap-1 text-xs">
          {FILTERS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              onClick={() => setFilter(entry.key)}
              className={cx(
                'rounded-md px-2.5 py-1 font-medium',
                filter === entry.key
                  ? 'bg-ink-900 text-white'
                  : 'text-ink-600 hover:bg-ink-100',
              )}
            >
              {entry.label}
              {data && entry.key !== 'all' && data.counts[entry.key] ? (
                <span className="ml-1 opacity-60">{data.counts[entry.key]}</span>
              ) : null}
            </button>
          ))}
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!data ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : data.orders.length === 0 ? (
          <EmptyState
            title="No orders match"
            description={
              // Say what was looked for and where, because "no match" on
              // its own leaves somebody wondering whether the thing they
              // typed is even a thing the search reads.
              data.search.terms.length
                ? `Nothing has ${
                    data.search.terms.length > 1 ? 'all of ' : ''
                  }${data.search.terms.map((term) => `“${term}”`).join(' and ')} in its ` +
                  'number, buyer, items, tracking number, invoice number or note' +
                  (filter !== 'all' ? ` — and this is only ${COLUMN_LABELS[filter]}.` : '.')
                : filter !== 'all'
                  ? 'Nothing is in this column. Try All.'
                  : 'Orders appear here within a few minutes of arriving on a connected shop.'
            }
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {data.orders.map((order) => (
              // A div with a button's manners rather than a <button>: the row
              // carries its own Invoice control, and a button inside a button
              // is not something a browser is obliged to make sense of.
              // Focusable, activated by Enter and Space, announced as a button.
              <div
                key={order.id}
                role="button"
                tabIndex={0}
                onClick={() => setOpenOrderId(order.id)}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    setOpenOrderId(order.id)
                  }
                }}
                className="flex w-full cursor-pointer flex-wrap items-center gap-2 px-4 py-3 text-left hover:bg-ink-50"
              >
                <span className="font-mono text-sm font-semibold text-ink-900">
                  #{order.order_number}
                </span>
                {sourcesWorthShowing(data.orders) ? (
                  <Badge className={SOURCE_CLASSES[order.source]}>
                    {SOURCE_LABELS[order.source]}
                  </Badge>
                ) : null}
                <span className="min-w-0 flex-1 truncate text-sm text-ink-600">
                  {order.buyer_name ?? 'Unknown buyer'}
                  {/* Why it is here, when the reason is not visible on the
                      row. Without it a result matched on a note or on one
                      of its lines reads as the search being broken. */}
                  {order.matched_on?.length ? (
                    <span className="ml-1.5 text-xs text-ink-400">
                      — matched {order.matched_on.join(', ')}
                    </span>
                  ) : null}
                </span>
                <span className="text-xs text-ink-400">
                  {order.placed_at ? formatDateTime(order.placed_at) : '—'}
                </span>
                {order.summary.unmatched_count > 0 ? (
                  <Badge className="bg-red-100 text-red-800 ring-red-300">
                    {order.summary.unmatched_count} without a product
                  </Badge>
                ) : null}
                {order.status !== 'cancelled' ? (
                  <InvoiceAction order={order} onChanged={load} />
                ) : null}
                <Badge className={STATUS_CLASSES[order.status]}>
                  {order.status === 'cancelled' ? 'Cancelled' : COLUMN_LABELS[order.status]}
                </Badge>
              </div>
            ))}
          </Card>
        )}
      </div>

      {adding ? (
        <NewOrderDialog onClose={() => setAdding(false)} onCreated={load} />
      ) : null}

      {openOrderId ? (
        <OrderDrawer
          orderId={openOrderId}
          onClose={() => setOpenOrderId(null)}
          onChanged={load}
        />
      ) : null}
    </div>
  )
}
