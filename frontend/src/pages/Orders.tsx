import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { COLUMN_LABELS, formatDateTime } from '../lib/format'
import type { Order, OrderStatus } from '../lib/types'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, Card, EmptyState, Spinner, cx, inputClass } from '../components/ui'

/** Every order, including the ones the board does not draw.
 *
 * The board has five live columns, so a cancelled order vanishes from it
 * entirely and a shipped one scrolls away. This is the way back to any of
 * them — the same drawer, reachable by number or buyer.
 */

const STATUS_CLASSES: Record<OrderStatus, string> = {
  new: 'bg-ink-100 text-ink-700 ring-ink-300',
  in_production: 'bg-amber-100 text-amber-800 ring-amber-300',
  assembly: 'bg-violet-100 text-violet-800 ring-violet-300',
  ready_to_ship: 'bg-indigo-100 text-indigo-800 ring-indigo-300',
  shipped: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  cancelled: 'bg-red-100 text-red-800 ring-red-300',
}

const FILTERS: { key: OrderStatus | 'all'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'new', label: COLUMN_LABELS.new },
  { key: 'in_production', label: COLUMN_LABELS.in_production },
  { key: 'assembly', label: COLUMN_LABELS.assembly },
  { key: 'ready_to_ship', label: COLUMN_LABELS.ready_to_ship },
  { key: 'shipped', label: COLUMN_LABELS.shipped },
  { key: 'cancelled', label: 'Cancelled' },
]

interface OrdersResponse {
  orders: Order[]
  counts: Record<string, number>
  total: number
}

export default function Orders() {
  const [data, setData] = useState<OrdersResponse | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<OrderStatus | 'all'>('all')
  const [error, setError] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)

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

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Orders</h1>
          <input
            className={`${inputClass} max-w-xs`}
            placeholder="Search by order number or buyer…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {data ? (
            <span className="text-sm text-ink-500">{data.total} in total</span>
          ) : null}
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
              query || filter !== 'all'
                ? 'Try a different search, or clear the filter.'
                : 'Orders appear here within a few minutes of arriving on Etsy.'
            }
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {data.orders.map((order) => (
              <button
                key={order.id}
                type="button"
                onClick={() => setOpenOrderId(order.id)}
                className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left hover:bg-ink-50"
              >
                <span className="font-mono text-sm font-semibold text-ink-900">
                  #{order.order_number}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-ink-600">
                  {order.buyer_name ?? 'Unknown buyer'}
                </span>
                <span className="text-xs text-ink-400">
                  {order.placed_at ? formatDateTime(order.placed_at) : '—'}
                </span>
                {order.summary.unmatched_count > 0 ? (
                  <Badge className="bg-red-100 text-red-800 ring-red-300">
                    {order.summary.unmatched_count} without a product
                  </Badge>
                ) : null}
                <Badge className={STATUS_CLASSES[order.status]}>
                  {order.status === 'cancelled' ? 'Cancelled' : COLUMN_LABELS[order.status]}
                </Badge>
              </button>
            ))}
          </Card>
        )}
      </div>

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
