import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  LINE_STATE_CLASSES,
  LINE_STATE_LABELS,
  formatAge,
} from '../lib/format'
import type { BoardResponse, Order, OrderLine } from '../lib/types'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, EmptyState, Spinner, cx } from '../components/ui'

const REFRESH_MS = 20_000

function flatten(lines: OrderLine[]): OrderLine[] {
  return lines.flatMap((line) => [line, ...flatten(line.children)])
}

function OrderCard({ order, onOpen }: { order: Order; onOpen: () => void }) {
  const leaves = flatten(order.lines).filter(
    (line) => !line.is_bundle && line.state !== 'cancelled',
  )
  const attention = order.summary.needs_attention

  return (
    <button
      type="button"
      onClick={onOpen}
      className={cx(
        'w-full rounded-lg bg-white p-3 text-left shadow-sm ring-1 transition hover:shadow-md',
        attention ? 'ring-red-300' : 'ring-ink-200',
      )}
    >
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-sm font-semibold text-ink-900">
          #{order.order_number}
        </span>
        <span className="ml-auto text-xs text-ink-400">{formatAge(order.placed_at)}</span>
      </div>
      <p className="truncate text-sm text-ink-600">{order.buyer_name ?? 'Unknown buyer'}</p>

      <div className="mt-2 flex flex-wrap gap-1">
        {order.summary.unmatched_count > 0 ? (
          <Badge className="bg-red-100 text-red-800 ring-red-300">
            {order.summary.unmatched_count} unmatched SKU
          </Badge>
        ) : null}
        {order.summary.failed_job_count > 0 ? (
          <Badge className="bg-red-100 text-red-800 ring-red-300">
            {order.summary.failed_job_count} print failed
          </Badge>
        ) : null}
        {order.status === 'assembly' && order.summary.pending_assembly.length ? (
          <Badge className="bg-violet-100 text-violet-800 ring-violet-300">
            {order.summary.pending_assembly.length} to assemble
          </Badge>
        ) : null}
        {order.tracking_number ? (
          <Badge className="bg-indigo-100 text-indigo-800 ring-indigo-300">
            {order.tracking_number}
          </Badge>
        ) : null}
      </div>

      <ul className="mt-2 space-y-1">
        {leaves.slice(0, 4).map((line) => (
          <li key={line.id} className="flex items-center gap-1.5 text-xs">
            <span className="shrink-0 text-ink-400">{line.quantity}×</span>
            <span className="min-w-0 flex-1 truncate font-mono text-ink-700">
              {line.sku ?? line.sku_raw ?? '—'}
            </span>
            <Badge className={LINE_STATE_CLASSES[line.state]}>
              {LINE_STATE_LABELS[line.state]}
            </Badge>
          </li>
        ))}
        {leaves.length > 4 ? (
          <li className="text-xs text-ink-400">+{leaves.length - 4} more</li>
        ) : null}
      </ul>

      {order.summary.units_to_print > 0 || order.summary.units_from_stock > 0 ? (
        <p className="mt-2 text-[11px] text-ink-500">
          {order.summary.units_from_stock} from stock · {order.summary.units_to_print} to
          print
        </p>
      ) : null}
    </button>
  )
}

export default function Board() {
  const [board, setBoard] = useState<BoardResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setBoard(await api.get<BoardResponse>('/api/board'))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  if (!board) {
    return (
      <div className="flex h-full items-center justify-center">
        {error ? <Alert tone="error">{error}</Alert> : <Spinner className="h-6 w-6" />}
      </div>
    )
  }

  const total = board.columns.reduce((sum, column) => sum + column.count, 0)

  return (
    <div className="flex h-full flex-col">
      {error ? (
        <div className="px-3 pt-3 sm:px-6">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}

      {total === 0 ? (
        <div className="p-6">
          <EmptyState
            title="No orders yet"
            description="Orders appear here within a few minutes of arriving on Etsy. Check Settings if Etsy is not connected."
          />
        </div>
      ) : (
        // scroll-p matches p so snapping does not swallow the container padding.
        <div className="board-scroll flex min-h-0 flex-1 snap-x scroll-p-3 gap-3 overflow-x-auto p-3 sm:scroll-p-6 sm:p-6">
          {board.columns.map((column) => (
            <section
              key={column.key}
              className="flex min-h-0 w-[85vw] shrink-0 snap-start flex-col sm:w-72"
            >
              <header className="mb-2 flex items-center gap-2">
                <h2 className="text-sm font-semibold text-ink-800">
                  {COLUMN_LABELS[column.key]}
                </h2>
                <span className="rounded-full bg-ink-200 px-1.5 text-xs text-ink-600">
                  {column.count}
                </span>
              </header>
              <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
                {column.orders.length === 0 ? (
                  <p className="rounded-lg border border-dashed border-ink-300 px-3 py-6 text-center text-xs text-ink-400">
                    Nothing here
                  </p>
                ) : (
                  column.orders.map((order) => (
                    <OrderCard
                      key={order.id}
                      order={order}
                      onOpen={() => setOpenOrderId(order.id)}
                    />
                  ))
                )}
              </div>
            </section>
          ))}
        </div>
      )}

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
