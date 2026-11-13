import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { COLUMN_LABELS } from '../lib/format'
import type {
  OrderStatus,
  ProductionItem,
  ProductionList,
  ProductionRunning,
} from '../lib/types'
import { Alert, Badge, Card, EmptyState, Spinner, cx } from '../components/ui'

/** What the shop still has to make, what it has decided goes first, and what
 *  is being made right now.
 *
 * A list of *things and quantities*, and nothing else. How the making
 * happens — which file, which machine, what was sent where — is the Printers
 * tab's business: somebody standing at a bench with this screen open wants to
 * know what to pick up next, not how the queue is plumbed.
 *
 * One list, one number per thing. A unit that comes off a shelf is still a
 * thing somebody has to go and get, so it is a row here like any other; that
 * it is a walk rather than a machine is a note on the row, written where it
 * changes what the person does, and not a second list to read.
 *
 * **First** is the exception, and it is the shop's own: the list sorts by how
 * long an order has waited, which is right until the afternoon somebody is on
 * the phone. Drag a row up there and it goes first — on this screen, on the
 * printed sheet, and in what actually gets sent to a machine. Drag it back and
 * it rejoins the queue where its wait puts it.
 *
 * **Being made** sits between them, because it is the only part of the screen
 * that changes on its own. It is drawn as a bar per thing rather than as a
 * row in a table, since "how far along is it" is a quantity a shape answers
 * faster than a number does.
 *
 * The whole thing can be narrowed to board columns, because "what do the
 * orders nobody has started need" and "what is the assembly bench waiting
 * for" are two different afternoons. The choice is remembered in this
 * browser, not on the server: it is where somebody is standing, not what the
 * shop has decided, and two people at two benches want different answers.
 */

/* Where this browser remembers which columns it was last showing. Per
   viewer on purpose — see above. */
const COLUMNS_KEY = 'printflow.tomake.columns'

function remembered(): OrderStatus[] | null {
  try {
    const saved = window.localStorage.getItem(COLUMNS_KEY)
    if (!saved) return null
    const parsed = JSON.parse(saved)
    return Array.isArray(parsed) && parsed.every((x) => typeof x === 'string')
      ? (parsed as OrderStatus[])
      : null
  } catch {
    // A browser with storage switched off still gets a working screen; it
    // just starts from the whole shop every time.
    return null
  }
}

function remember(columns: OrderStatus[] | null) {
  try {
    if (columns === null) window.localStorage.removeItem(COLUMNS_KEY)
    else window.localStorage.setItem(COLUMNS_KEY, JSON.stringify(columns))
  } catch {
    /* Not worth a word on screen: the filter still works, it just will not
       be there tomorrow. */
  }
}

/* The two colours the screen uses for quantities, and the only two. Emerald
   already means finished everywhere else in PrintFlow and amber already
   means in progress, so this borrows the language rather than inventing a
   third. The pair clears colour-blind separation and 3:1 against the
   surface, which matters on the bar, where a segment's meaning is carried by
   its colour as well as by the number beside it. */
const MADE_FILL = 'bg-emerald-700'
const HAPPENING_FILL = 'bg-amber-600'

/* Where a dragged row would land. An inset shadow rather than a border, so
   nothing on the page moves a pixel while somebody is aiming at it. */
const DROP_ABOVE = 'shadow-[inset_0_2px_0_0_#047857]'
const DROP_BELOW = 'shadow-[inset_0_-2px_0_0_#047857]'

type Section = 'priority' | 'rest'
type DropAt = { section: Section; index: number }

/** A thing, as one string. The pair is what every list here is keyed on, and
 *  a drag has to carry it through a `dataTransfer` that only speaks text. */
const keyOf = (item: { product_id: string; variation_id: string | null }) =>
  `${item.product_id}:${item.variation_id ?? ''}`

const unkey = (key: string) => {
  const cut = key.indexOf(':')
  const variation = key.slice(cut + 1)
  return { product_id: key.slice(0, cut), variation_id: variation || null }
}

function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string | number
  hint?: string
  tone?: string
}) {
  return (
    <Card className="p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-ink-500">{label}</p>
      <p className={cx('mt-1 text-xl font-semibold tabular-nums', tone ?? 'text-ink-900')}>
        {value}
      </p>
      {hint ? <p className="mt-0.5 text-xs text-ink-500">{hint}</p> : null}
    </Card>
  )
}

/** How long something has been waiting or running, in the words somebody
 *  would use rather than a timestamp nobody reads. */
function since(iso: string | null): string {
  if (!iso) return '—'
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000)
  if (minutes < 60) return `${Math.max(minutes, 1)} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h`
  const days = Math.floor(hours / 24)
  return days === 1 ? '1 day' : `${days} days`
}

function Chevron({ down }: { down?: boolean }) {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden="true">
      <path
        d={down ? 'M4 6l4 4 4-4' : 'M4 10l4-4 4 4'}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function Cross() {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden="true">
      <path
        d="M4 4l8 8M12 4l-8 8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** One of the small controls beside a row.
 *
 * They exist because dragging is a mouse, and a mouse is not the only way
 * anybody works: everything a drag does here, these do too, from a keyboard,
 * in the same order. Muted until the row is hovered or one of them is
 * focused, so the table reads as a list and not as a control panel.
 */
function RowButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string
  onClick: () => void
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="rounded p-1 text-ink-400 opacity-0 transition hover:bg-ink-100 hover:text-ink-700 focus:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-emerald-700 disabled:cursor-not-allowed disabled:opacity-30 group-hover:opacity-100"
    >
      {children}
    </button>
  )
}

/** One board column, as a switch.
 *
 * It carries its own count, taken over the whole shop rather than over what
 * is currently showing — so a column you have switched off still says what
 * it is holding, which is the only thing that would make you switch it back
 * on. A column holding nothing says so rather than disappearing: an empty
 * Assembly is an answer, and a missing one is a puzzle.
 */
function ColumnChip({
  column,
  showing,
  orders,
  units,
  onToggle,
}: {
  column: OrderStatus
  showing: boolean
  orders: number
  units: number
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={showing}
      title={
        orders
          ? `${orders} order${orders === 1 ? '' : 's'} here, wanting ${units}`
          : 'Nothing here'
      }
      className={cx(
        'rounded-full border px-2.5 py-1 text-xs font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-emerald-700',
        showing
          ? 'border-emerald-700 bg-emerald-50 text-emerald-900'
          : 'border-ink-200 bg-white text-ink-500 hover:border-ink-300 hover:text-ink-700',
      )}
    >
      {COLUMN_LABELS[column]}
      <span
        className={cx(
          'ml-1.5 tabular-nums',
          showing ? 'text-emerald-700' : 'text-ink-400',
        )}
      >
        {units}
      </span>
    </button>
  )
}

/** One thing being made, as a bar.
 *
 * Three segments that always add up to what is under way: finished, moving,
 * and waiting its turn. The moving segment breathes, because this is the one
 * card on the screen that is true only for as long as you are looking at it
 * — and a shape that changes says "this is live" faster than a word does.
 * It is a shallow breath on purpose: deep enough to notice, not deep enough
 * to take the colour below the contrast it was picked at, and off entirely
 * for a viewer who has asked for less motion. Every segment's number is
 * written out beside the bar as well, so the colours are never the only
 * thing carrying the meaning.
 */
function Running({ item }: { item: ProductionRunning }) {
  const width = (units: number) =>
    item.units > 0 ? `${(units / item.units) * 100}%` : '0%'

  return (
    <div className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-mono text-xs text-ink-500">{item.sku}</span>
        <span className="text-sm font-medium text-ink-900">{item.name}</span>
        {item.variation_label ? (
          <span className="text-xs text-ink-500">· {item.variation_label}</span>
        ) : null}
        <span className="ml-auto text-xs text-ink-500">
          {item.started_at ? `started ${since(item.started_at)} ago` : ''}
        </span>
      </div>

      <div className="mt-2 flex h-2.5 w-full items-stretch gap-[2px] overflow-hidden rounded-full bg-ink-100">
        <div
          className={cx('rounded-full', MADE_FILL)}
          style={{ width: width(item.units_made) }}
          title={`${item.units_made} made`}
        />
        <div
          className={cx('rounded-full', HAPPENING_FILL, 'breathe')}
          style={{ width: width(item.units_happening) }}
          title={`${item.units_happening} being made now`}
        />
      </div>

      <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
        <span className="text-base font-semibold tabular-nums text-ink-900">
          {item.units_made}
          <span className="text-xs font-normal text-ink-500"> of {item.units} made</span>
        </span>
        {item.units_happening ? (
          <span className="text-amber-700">{item.units_happening} under way</span>
        ) : null}
        {item.units_waiting ? (
          <span className="text-ink-500">{item.units_waiting} queued behind</span>
        ) : null}
      </div>
    </div>
  )
}

/** One row of the list, in either section.
 *
 * Deliberately the same component in both: a thing does not change when it is
 * made first, and a row that redrew itself on the way up would make the drag
 * feel like it had done something other than move it.
 */
function Row({
  item,
  place,
  dragging,
  drop,
  onDragStart,
  onDragEnd,
  onDragOver,
  controls,
}: {
  item: ProductionItem
  place: number | null
  dragging: boolean
  drop: 'above' | 'below' | null
  onDragStart: () => void
  onDragEnd: () => void
  onDragOver: (event: React.DragEvent) => void
  controls: React.ReactNode
}) {
  const nothingLeft = item.units === 0
  return (
    <tr
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragOver={onDragOver}
      className={cx(
        'group cursor-grab text-ink-700 active:cursor-grabbing',
        dragging && 'opacity-40',
        drop === 'above' && DROP_ABOVE,
        drop === 'below' && DROP_BELOW,
      )}
    >
      <td className="w-8 py-2 pl-3 pr-0 text-right text-xs tabular-nums text-ink-400">
        {place ?? ''}
      </td>
      <td className="px-3 py-2">
        <span className="font-mono text-xs text-ink-500">{item.sku}</span>
        <span
          className={cx(
            'ml-2 font-medium',
            nothingLeft ? 'text-ink-500' : 'text-ink-900',
          )}
        >
          {item.name}
        </span>
        {item.variation_label ? (
          <span className="ml-1.5 text-xs text-ink-500">· {item.variation_label}</span>
        ) : null}
        {item.bundles.length ? (
          <span className="ml-1.5 text-xs text-ink-400">
            → {item.bundles.join(', ')}
          </span>
        ) : null}
      </td>
      <td className="px-3 py-2 text-right">
        {nothingLeft ? (
          <span className="text-xs text-ink-400">nothing outstanding</span>
        ) : (
          <>
            <span className="text-base font-semibold tabular-nums text-ink-900">
              {item.units}
            </span>
            {/* Where it comes from, said only when it changes the walk. A row
                that is entirely off a shelf does not need the number said
                twice. */}
            {item.units_from_stock > 0 ? (
              <span className="ml-1 text-xs font-normal text-ink-500">
                {item.units_from_stock === item.units
                  ? 'off a shelf'
                  : `${item.units_from_stock} off a shelf`}
              </span>
            ) : null}
          </>
        )}
      </td>
      <td className="px-3 py-2 text-right text-xs text-ink-600">
        {since(item.waiting_since)}
      </td>
      <td className="py-2 pl-3 pr-1">
        {!item.can_start ? (
          <Badge className="bg-red-100 text-red-800 ring-red-300">
            Cannot be made yet
          </Badge>
        ) : null}
      </td>
      <td className="whitespace-nowrap py-2 pl-0 pr-2 text-right">{controls}</td>
    </tr>
  )
}

export default function ToMake() {
  const [list, setList] = useState<ProductionList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropAt, setDropAt] = useState<DropAt | null>(null)
  // Null is "every open column", which is not the same as a list that
  // happens to name all four: the first stays right when a column is added,
  // and is what a screen nobody has touched should ask for.
  const [columns, setColumns] = useState<OrderStatus[] | null>(remembered)
  // A drag in progress is a half-finished thought, and the minute refresh
  // would throw it away mid-gesture.
  const busy = useRef(false)

  const query = useMemo(
    () => (columns === null ? '' : `?columns=${columns.join(',')}`),
    [columns],
  )

  const load = useCallback(async () => {
    if (busy.current) return
    try {
      setList(await api.get<ProductionList>(`/api/production${query}`))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [query])

  useEffect(() => {
    load()
  }, [load])

  // Work moves on its own, so the screen does too — slowly, because a shop
  // floor display left open all day should not be a poll every second.
  useEffect(() => {
    const timer = window.setInterval(load, 60_000)
    return () => window.clearInterval(timer)
  }, [load])

  /** Switch one column on or off.
   *
   * Switching the last one off would leave a screen that says nothing and
   * explains nothing, so it goes back to showing everything instead — the
   * same place the "Show all" link goes, reached by the same gesture.
   */
  const toggle = useCallback(
    (column: OrderStatus) => {
      if (!list) return
      const open = list.columns.map((row) => row.key)
      const showing = new Set(columns ?? open)
      if (showing.has(column)) showing.delete(column)
      else showing.add(column)
      const next = open.filter((key) => showing.has(key))
      const all = next.length === open.length || next.length === 0
      setColumns(all ? null : next)
      remember(all ? null : next)
    },
    [columns, list],
  )

  const showAll = useCallback(() => {
    setColumns(null)
    remember(null)
  }, [])

  /** Put a thing somewhere, and tell the server the whole order.
   *
   * The screen moves first and the server confirms, because a drag that
   * waits for a round trip feels broken even when it is not. If the server
   * disagrees, its answer replaces the guess — it is the one that decides
   * what prints first.
   */
  const place = useCallback(
    async (key: string, section: Section, index: number) => {
      if (!list) return
      const was = list.priority.map(keyOf)
      const order = was.filter((other) => other !== key)
      if (section === 'priority') {
        // `index` means "before whatever is at that place now". Taking the
        // row out first shifts everything after it up one, so dropping a row
        // further down the list has to come back a step or it lands one
        // short of where the line was drawn.
        const from = was.indexOf(key)
        const at = from >= 0 && from < index ? index - 1 : index
        order.splice(Math.max(0, Math.min(at, order.length)), 0, key)
      }

      const everything = [...list.priority, ...list.items]
      const found = new Map(everything.map((item) => [keyOf(item), item]))
      const priority = order
        .map((pinned) => found.get(pinned))
        .filter((item): item is ProductionItem => Boolean(item))
      const pinned = new Set(order)
      // Out of the priority list and back into the queue, where its wait
      // puts it — the server sorts the rest, so this keeps their order.
      const items = everything.filter((item) => !pinned.has(keyOf(item)))

      setList({ ...list, priority, items })
      busy.current = true
      try {
        setList(
          await api.put<ProductionList>(`/api/production/priority${query}`, {
            items: order.map(unkey),
          }),
        )
        setError(null)
      } catch (err) {
        setError(errorMessage(err))
        busy.current = false
        await load()
        return
      }
      busy.current = false
    },
    [list, load, query],
  )

  /** Which side of a row the pointer is on, so a drop lands where it looks
   *  like it will rather than always before or always after. */
  const aimAt = useCallback(
    (section: Section, index: number) => (event: React.DragEvent) => {
      event.preventDefault()
      const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
      const below = event.clientY > box.top + box.height / 2
      setDropAt({ section, index: below ? index + 1 : index })
    },
    [],
  )

  const drop = useCallback(
    (fallback: DropAt) => (event: React.DragEvent) => {
      event.preventDefault()
      const key = dragging ?? event.dataTransfer.getData('text/plain')
      const target = dropAt ?? fallback
      setDragging(null)
      setDropAt(null)
      if (key) place(key, target.section, target.index)
    },
    [dragging, dropAt, place],
  )

  const marker = (section: Section, index: number, last: boolean) => {
    if (!dropAt || dropAt.section !== section) return null
    if (dropAt.index === index) return 'above' as const
    if (last && dropAt.index === index + 1) return 'below' as const
    return null
  }

  const totals = list?.totals
  const header = (
    <tr className="text-left text-xs uppercase tracking-wide text-ink-500">
      <th className="w-8 py-2 pl-3 pr-0" />
      <th className="px-3 py-2 font-medium">Item</th>
      <th className="px-3 py-2 text-right font-medium">Quantity</th>
      <th className="px-3 py-2 text-right font-medium">Waiting</th>
      <th className="py-2 pl-3 pr-1 font-medium" />
      <th className="py-2 pl-0 pr-2" />
    </tr>
  )

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">To make</h1>
          <span className="text-sm text-ink-500">
            {columns === null
              ? 'Everything open orders are short of, added up by item.'
              : 'What these board columns are short of, added up by item.'}
          </span>
          <a
            className="ml-auto rounded-md px-2.5 py-1 text-xs font-medium text-ink-600 underline"
            href={`/api/production.csv${query}`}
          >
            Download CSV
          </a>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {/* The board's own columns, as switches. Above the tiles rather than
            beside them, because every number below depends on which of these
            are on and a filter found afterwards is a filter read too late. */}
        {list ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-0.5 text-xs font-medium uppercase tracking-wide text-ink-500">
              Columns
            </span>
            {list.columns.map((column) => (
              <ColumnChip
                key={column.key}
                column={column.key}
                showing={column.showing}
                orders={column.orders}
                units={column.units}
                onToggle={() => toggle(column.key)}
              />
            ))}
            {columns === null ? (
              <span className="ml-1 text-xs text-ink-400">all of them</span>
            ) : (
              <button
                type="button"
                onClick={showAll}
                className="ml-1 rounded px-1.5 py-1 text-xs font-medium text-ink-600 underline"
              >
                Show all
              </button>
            )}
          </div>
        ) : null}

        {!list ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Tile
                label="To make"
                value={totals!.units}
                hint={`${totals!.items} item${totals!.items === 1 ? '' : 's'}, for ${
                  totals!.orders
                } order${totals!.orders === 1 ? '' : 's'}`}
              />
              <Tile
                label="Off a shelf"
                value={totals!.units_from_stock}
                hint={`of the ${totals!.units}, a walk not a machine`}
              />
              <Tile
                label="Being made"
                value={totals!.units_in_production}
                hint={
                  totals!.units_happening
                    ? `${totals!.units_happening} under way right now`
                    : 'Waiting their turn'
                }
                tone={totals!.units_in_production ? 'text-amber-700' : undefined}
              />
              <Tile
                label="Cannot start"
                value={totals!.blocked + list.unmatched.length}
                hint="Not set up, or nothing matched"
                tone={
                  totals!.blocked + list.unmatched.length ? 'text-red-700' : undefined
                }
              />
            </div>

            {/* First. Its own card, above everything, because that is what
                "first" has to look like — and a drop target big enough to
                aim at even when it is empty. */}
            <Card
              className={cx(
                'overflow-hidden',
                dropAt?.section === 'priority' && 'ring-2 ring-emerald-700',
              )}
              onDragOver={(event: React.DragEvent) => {
                event.preventDefault()
                if (!dropAt || dropAt.section !== 'priority') {
                  setDropAt({ section: 'priority', index: list.priority.length })
                }
              }}
              onDrop={drop({ section: 'priority', index: list.priority.length })}
            >
              <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">First</h2>
                <span className="text-xs text-ink-500">
                  Whatever the shop has decided goes before the queue — on this
                  screen, on the printed sheet, and on the machines.
                </span>
              </div>

              {list.priority.length === 0 ? (
                <p className="m-2 rounded-lg border-2 border-dashed border-ink-200 px-3 py-6 text-center text-xs text-ink-500">
                  Nothing goes first. Drag a row up here — or use the{' '}
                  <span className="inline-flex align-text-bottom text-ink-400">
                    <Chevron />
                  </span>{' '}
                  beside it — and it jumps the queue until somebody drags it back.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[32rem] text-sm">
                    <thead>{header}</thead>
                    <tbody className="divide-y divide-ink-200">
                      {list.priority.map((item, index) => (
                        <Row
                          key={keyOf(item)}
                          item={item}
                          place={index + 1}
                          dragging={dragging === keyOf(item)}
                          drop={marker(
                            'priority',
                            index,
                            index === list.priority.length - 1,
                          )}
                          onDragStart={() => setDragging(keyOf(item))}
                          onDragEnd={() => {
                            setDragging(null)
                            setDropAt(null)
                          }}
                          onDragOver={aimAt('priority', index)}
                          controls={
                            <>
                              <RowButton
                                label="Move up"
                                disabled={index === 0}
                                onClick={() => place(keyOf(item), 'priority', index - 1)}
                              >
                                <Chevron />
                              </RowButton>
                              <RowButton
                                label="Move down"
                                disabled={index === list.priority.length - 1}
                                onClick={() => place(keyOf(item), 'priority', index + 2)}
                              >
                                <Chevron down />
                              </RowButton>
                              <RowButton
                                label="Back to the queue"
                                onClick={() => place(keyOf(item), 'rest', 0)}
                              >
                                <Cross />
                              </RowButton>
                            </>
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            {list.in_production.length ? (
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                  <h2 className="text-sm font-semibold text-ink-800">Being made now</h2>
                  <span className="text-xs text-ink-500">
                    Under way without anybody asking. Closest to finished first.
                  </span>
                  <span className="ml-auto flex items-center gap-1.5 text-xs text-ink-500">
                    <span className={cx('h-2 w-2 rounded-full', MADE_FILL)} />
                    made
                    <span
                      className={cx('ml-2 h-2 w-2 rounded-full', HAPPENING_FILL)}
                    />
                    under way
                  </span>
                </div>
                <div className="grid gap-2 p-2 sm:grid-cols-2">
                  {list.in_production.map((item) => (
                    <Running key={keyOf(item)} item={item} />
                  ))}
                </div>
              </Card>
            ) : null}

            <Card
              className={cx(
                'overflow-hidden',
                dropAt?.section === 'rest' && 'ring-2 ring-emerald-700',
              )}
              onDragOver={(event: React.DragEvent) => {
                event.preventDefault()
                setDropAt({ section: 'rest', index: 0 })
              }}
              onDrop={drop({ section: 'rest', index: 0 })}
            >
              <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">Then</h2>
                <span className="text-xs text-ink-500">
                  Everything not in hand yet, however it is going to get here.
                  Longest wait first.
                </span>
              </div>

              {list.items.length === 0 ? (
                <EmptyState
                  title={
                    list.in_production.length || list.priority.length
                      ? 'Nothing else waiting'
                      : columns === null
                        ? 'Nothing to make'
                        : 'Nothing in these columns'
                  }
                  description={
                    list.in_production.length || list.priority.length
                      ? 'Everything these columns are short of is above.'
                      : columns === null
                        ? 'Every open order has what it needs. New orders appear here within minutes of arriving.'
                        : // Never "nothing to make" under a filter: the
                          // difference between an empty shop and an empty
                          // corner of it is the whole reason to say which.
                          'The columns you are showing have everything they need. Switch another one on to see the rest.'
                  }
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[32rem] text-sm">
                    <thead>{header}</thead>
                    <tbody className="divide-y divide-ink-200">
                      {list.items.map((item, index) => (
                        <Row
                          key={keyOf(item)}
                          item={item}
                          place={null}
                          dragging={dragging === keyOf(item)}
                          drop={null}
                          onDragStart={() => setDragging(keyOf(item))}
                          onDragEnd={() => {
                            setDragging(null)
                            setDropAt(null)
                          }}
                          onDragOver={(event) => {
                            event.preventDefault()
                            setDropAt({ section: 'rest', index })
                          }}
                          controls={
                            <RowButton
                              label="Make this first"
                              onClick={() => place(keyOf(item), 'priority', 0)}
                            >
                              <Chevron />
                            </RowButton>
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {totals!.blocked ? (
                <p className="border-t border-ink-200 px-3 py-2 text-xs text-ink-500">
                  “Cannot be made yet” means nothing here knows how to make it. Set it
                  up on the Products tab and it joins the list.
                </p>
              ) : null}
            </Card>

            {list.unmatched.length ? (
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                  <h2 className="text-sm font-semibold text-ink-800">
                    Waiting on a product
                  </h2>
                  <span className="text-xs text-ink-500">
                    Nothing says what these are, so nothing can be made for them. Link
                    them on the Products tab.
                  </span>
                </div>
                <div className="divide-y divide-ink-200">
                  {list.unmatched.map((line) => (
                    <div
                      key={`${line.sku ?? ''}:${line.title ?? ''}`}
                      className="flex flex-wrap items-center gap-2 px-3 py-2"
                    >
                      <span className="w-8 text-right text-base font-semibold tabular-nums text-ink-900">
                        {line.units}
                      </span>
                      <span className="font-mono text-xs text-ink-500">
                        {line.sku ?? 'no code'}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm text-ink-700">
                        {line.title ?? 'Unknown item'}
                      </span>
                      <span className="text-xs text-ink-500">
                        {since(line.waiting_since)}
                      </span>
                    </div>
                  ))}
                </div>
              </Card>
            ) : null}
          </>
        )}
      </div>
    </div>
  )
}
