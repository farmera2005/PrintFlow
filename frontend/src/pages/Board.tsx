import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  LINE_STATE_CLASSES,
  LINE_STATE_LABELS,
  TRACKING_CLASSES,
  TRACKING_LABELS,
  formatAge,
  formatMoney,
} from '../lib/format'
import type { BoardResponse, Order, OrderLine, OrderStatus } from '../lib/types'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, EmptyState, Spinner, cx } from '../components/ui'

const REFRESH_MS = 20_000
// Per dragover tick while the pointer is held near an edge of the board.
const EDGE_SCROLL_PX = 18

/** A component line, with the options the buyer chose for the item it came from.
 *
 * Options live on the ordered line — the bundle — while the card lists what
 * actually gets made, which is its components. Carrying them down is what lets
 * a card say "Red" next to the filament it is going to pull.
 */
interface Leaf {
  line: OrderLine
  options: OrderLine['variations']
  /** Which ordered item this came from, so the options print once per item. */
  group: string
}

function leaves(lines: OrderLine[], inherited: OrderLine['variations'] = [], group = ''): Leaf[] {
  return lines.flatMap((line) => {
    const options = line.variations?.length ? line.variations : inherited
    const key = line.variations?.length ? line.id : group
    if (line.children.length) return leaves(line.children, options, key)
    return [{ line, options, group: key || line.id }]
  })
}

function summariseOptions(options: OrderLine['variations']): string {
  return options.map((option) => `${option.name}: ${option.value}`).join(' · ')
}

/** How long a finished card has left on the board, in words.
 *
 *  Said out loud because the alternative is a card that is simply gone one
 *  morning, which looks exactly like a card that was deleted — and nothing
 *  here is ever deleted. */
function leavesBoardIn(completedAt: string, hours: number): string {
  const goes = new Date(completedAt).getTime() + hours * 3600_000
  const left = goes - Date.now()
  if (!Number.isFinite(left)) return 'Done'
  if (left <= 0) return 'Leaving the board'
  const h = Math.round(left / 3600_000)
  return h < 1 ? 'Off the board within the hour' : `Off the board in ${h}h`
}

function OrderCard({
  order,
  onOpen,
  onDragStart,
  onDragEnd,
  dragging,
  completeHours,
}: {
  order: Order
  onOpen: () => void
  onDragStart: () => void
  onDragEnd: () => void
  dragging: boolean
  /** How long a delivered card stays drawn, as the server sets it. */
  completeHours: number
}) {
  const rows = leaves(order.lines).filter(
    (leaf) => !leaf.line.is_bundle && leaf.line.state !== 'cancelled',
  )
  const visible = rows.slice(0, 4)
  const attention = order.summary.needs_attention

  return (
    // A div with a button's manners rather than a <button>, because the
    // tracking number inside it is a real link and a link inside a button is
    // not something a browser is obliged to make sense of. Everything a button
    // gave us is kept: focusable, activated by Enter and Space, announced as a
    // button. Only the tag changed.
    <div
      role="button"
      tabIndex={0}
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = 'move'
        // Firefox will not start a drag without data on the transfer.
        event.dataTransfer.setData('text/plain', order.id)
        onDragStart()
      }}
      onDragEnd={onDragEnd}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen()
        }
      }}
      className={cx(
        'w-full cursor-grab rounded-lg bg-white p-3 text-left shadow-sm ring-1 transition hover:shadow-md active:cursor-grabbing',
        attention ? 'ring-red-300' : 'ring-ink-200',
        dragging && 'opacity-40',
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
            {order.summary.unmatched_count} without a product
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
        {/* The tracking number, and where it goes. A number nobody can click
            is a number somebody retypes into a carrier's website with the card
            open in another window. stopPropagation because the whole card
            opens the drawer, and following a link is not opening the drawer. */}
        {order.tracking_number && order.tracking_url ? (
          <a
            href={order.tracking_url}
            target="_blank"
            rel="noreferrer"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
            className="rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-medium text-indigo-800 underline decoration-indigo-400 underline-offset-2 ring-1 ring-indigo-300 hover:decoration-indigo-800"
            title={`Track this parcel${order.carrier_code ? ` with ${order.carrier_code}` : ''}`}
          >
            {order.tracking_number}
          </a>
        ) : order.tracking_number ? (
          <Badge className="bg-indigo-100 text-indigo-800 ring-indigo-300">
            {order.tracking_number}
          </Badge>
        ) : null}
        {/* What the carrier last said. Only once it has said something: every
            parcel is "not scanned yet" for its first few hours, and a badge
            saying so on every fresh card is noise. */}
        {order.tracking_status && order.tracking_status !== 'unknown' ? (
          <Badge className={TRACKING_CLASSES[order.tracking_status]}>
            {TRACKING_LABELS[order.tracking_status]}
          </Badge>
        ) : null}
        {/* A finished card says when it goes. Nothing is deleted — it stays in
            the Orders tab — but a card that vanished with no warning reads as
            one that was thrown away. */}
        {order.status === 'complete' && order.completed_at ? (
          <Badge className="bg-ink-100 text-ink-600 ring-ink-300">
            {leavesBoardIn(order.completed_at, completeHours)}
          </Badge>
        ) : null}
        {/* What the label cost. Labels are the one thing PrintFlow spends
            money on, and the number is otherwise only visible in ShipStation. */}
        {order.label_cost ? (
          <Badge className="bg-ink-100 text-ink-700 ring-ink-300">
            {formatMoney(order.label_cost, order.label_currency)}
          </Badge>
        ) : null}
      </div>

      <ul className="mt-2 space-y-1">
        {visible.map((leaf, index) => {
          // Once per ordered item, above the components it turns into — the
          // options describe the thing that was bought, not each part of it.
          const heads = leaf.options.length && leaf.group !== visible[index - 1]?.group
          return (
            <li key={leaf.line.id}>
              {heads ? (
                <p className="truncate text-[11px] text-violet-700" title={summariseOptions(leaf.options)}>
                  {summariseOptions(leaf.options)}
                </p>
              ) : null}
              <div className="flex items-center gap-1.5 text-xs">
                <span className="shrink-0 text-ink-400">{leaf.line.quantity}×</span>
                {/* The listing's own name, because that is what the operator
                    recognises and what Etsy always sends. Codes are optional
                    now, so leading with one would leave rows blank. */}
                <span className="min-w-0 flex-1 truncate text-ink-700">
                  {leaf.line.name ?? leaf.line.sku ?? '—'}
                </span>
                <Badge className={LINE_STATE_CLASSES[leaf.line.state]}>
                  {LINE_STATE_LABELS[leaf.line.state]}
                </Badge>
              </div>
            </li>
          )
        })}
        {rows.length > 4 ? (
          <li className="text-xs text-ink-400">+{rows.length - 4} more</li>
        ) : null}
      </ul>

      {order.summary.units_to_print > 0 || order.summary.units_from_stock > 0 ? (
        <p className="mt-2 text-[11px] text-ink-500">
          {order.summary.units_from_stock} from stock · {order.summary.units_to_print} to
          print
        </p>
      ) : null}
    </div>
  )
}

export default function Board() {
  const [board, setBoard] = useState<BoardResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [over, setOver] = useState<OrderStatus | null>(null)

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

  /** Move a card to the column it was dropped on.
   *
   * The card moves on screen first and the request follows, because a board
   * that lags a drag by a round trip feels broken. A failure reloads, which
   * puts the card back where the server says it is. */
  const move = async (orderId: string, status: OrderStatus) => {
    const current = board?.columns
      .flatMap((column) => column.orders)
      .find((order) => order.id === orderId)
    if (!current || current.status === status) return

    setBoard((previous) =>
      previous
        ? {
            ...previous,
            columns: previous.columns.map((column) => ({
              ...column,
              orders:
                column.key === status
                  ? [{ ...current, status }, ...column.orders]
                  : column.orders.filter((order) => order.id !== orderId),
              count:
                column.key === status
                  ? column.count + 1
                  : column.orders.some((order) => order.id === orderId)
                    ? column.count - 1
                    : column.count,
            })),
          }
        : previous,
    )

    try {
      await api.put(`/api/orders/${orderId}/status`, { status })
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
    await load()
  }

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
        <div
          className="board-scroll flex min-h-0 flex-1 snap-x scroll-p-3 gap-3 overflow-x-auto p-3 sm:scroll-p-6 sm:p-6"
          // The board scrolls sideways and the browser will not scroll it for a
          // drag, so a column off the edge is unreachable without this: hold
          // near an edge and it comes to you.
          onDragOver={(event) => {
            if (!dragging) return
            const box = event.currentTarget.getBoundingClientRect()
            const edge = Math.min(120, box.width / 4)
            if (event.clientX < box.left + edge) {
              event.currentTarget.scrollLeft -= EDGE_SCROLL_PX
            } else if (event.clientX > box.right - edge) {
              event.currentTarget.scrollLeft += EDGE_SCROLL_PX
            }
          }}
        >
          {board.columns.map((column) => (
            <section
              key={column.key}
              onDragOver={(event) => {
                if (!dragging) return
                // Without preventDefault the browser refuses the drop.
                event.preventDefault()
                event.dataTransfer.dropEffect = 'move'
                setOver(column.key)
              }}
              onDragLeave={(event) => {
                // Only when the pointer has actually left the column, not when
                // it crosses onto a card inside it.
                if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                  setOver((current) => (current === column.key ? null : current))
                }
              }}
              onDrop={(event) => {
                event.preventDefault()
                const id = dragging ?? event.dataTransfer.getData('text/plain')
                setOver(null)
                setDragging(null)
                if (id) move(id, column.key)
              }}
              className={cx(
                'flex min-h-0 w-[85vw] shrink-0 snap-start flex-col rounded-lg sm:w-72',
                over === column.key && 'bg-ink-200/60 ring-2 ring-ink-400',
              )}
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
                    {dragging ? 'Drop here' : 'Nothing here'}
                  </p>
                ) : (
                  column.orders.map((order) => (
                    <OrderCard
                      key={order.id}
                      order={order}
                      dragging={dragging === order.id}
                      onDragStart={() => setDragging(order.id)}
                      onDragEnd={() => {
                        setDragging(null)
                        setOver(null)
                      }}
                      onOpen={() => setOpenOrderId(order.id)}
                      completeHours={board.complete_board_hours}
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
