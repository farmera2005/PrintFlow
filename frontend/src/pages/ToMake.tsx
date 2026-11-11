import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { SOURCE_CLASSES, SOURCE_LABELS, formatDateTime } from '../lib/format'
import type { OrderSource, ProductionList, ProductionItem } from '../lib/types'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, Button, Card, EmptyState, Spinner, cx } from '../components/ui'

/** Everything the shop still has to make, in one list.
 *
 * PrintFlow already knew all of this, scattered: the board knows which orders
 * are open, each order knows which of its lines are short, the Printers tab
 * knows what is on a machine. What nobody could see was the shape a print
 * shop actually works to — *one part, however many orders want it*. Six
 * orders each wanting one bracket is one plate's work, and finding that out
 * meant opening six orders.
 *
 * So the rows are things to make rather than orders, and the orders live
 * underneath them. Three things follow from that and are worth not undoing:
 *
 * **Outstanding is the number, not needed.** Units a live plate already
 * covers are somebody else's problem — the machine's. What is left is what
 * has to be started.
 *
 * **Oldest first.** Not biggest, not alphabetical. The part that has kept an
 * order waiting longest is the one to put on next.
 *
 * **What cannot be made is on the list too.** A product with no print file
 * and a line nothing matched are both real work that cannot start, and a
 * screen that only counted what it understood would under-report the day.
 */

const STAGE_CLASSES: Record<string, string> = {
  blocked: 'bg-red-100 text-red-800 ring-red-300',
  to_plan: 'bg-slate-100 text-slate-700 ring-slate-300',
  to_send: 'bg-amber-100 text-amber-900 ring-amber-300',
  on_a_machine: 'bg-sky-100 text-sky-800 ring-sky-300',
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

/** How long something has been waiting, in the words somebody would use. */
function waitedFor(iso: string | null): string {
  if (!iso) return '—'
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000)
  if (days < 1) return 'today'
  if (days === 1) return '1 day'
  return `${days} days`
}

/** The orders behind one row, which is the answer to "who is this for". */
function Behind({
  item,
  onOpenOrder,
}: {
  item: ProductionItem
  onOpenOrder: (id: string) => void
}) {
  return (
    <tr className="bg-ink-50/60">
      <td colSpan={7} className="px-3 py-2">
        <ul className="space-y-1">
          {item.orders.map((entry) => (
            <li key={entry.line_id} className="flex flex-wrap items-center gap-2 text-xs">
              <button
                type="button"
                className="font-mono font-semibold text-ink-900 underline"
                onClick={() => onOpenOrder(entry.order_id)}
              >
                #{entry.order_number}
              </button>
              <Badge className={SOURCE_CLASSES[entry.source as OrderSource]}>
                {SOURCE_LABELS[entry.source as OrderSource]}
              </Badge>
              <span className="text-ink-600">{entry.buyer_name ?? 'Unknown buyer'}</span>
              {entry.for_bundle ? (
                <Badge className="bg-violet-100 text-violet-800 ring-violet-300">
                  part of a bundle
                </Badge>
              ) : null}
              <span className="ml-auto text-ink-500">
                {entry.placed_at ? formatDateTime(entry.placed_at) : '—'}
              </span>
              <span className="w-16 text-right font-medium tabular-nums text-ink-800">
                {entry.units} ×
              </span>
            </li>
          ))}
        </ul>
        {item.orders.some((entry) => entry.note) ? (
          <p className="mt-2 text-xs text-amber-800">
            {item.orders.find((entry) => entry.note)?.note}
          </p>
        ) : null}
      </td>
    </tr>
  )
}

export default function ToMake() {
  const [list, setList] = useState<ProductionList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [openRow, setOpenRow] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)
  const [hideCovered, setHideCovered] = useState(true)

  const load = useCallback(async () => {
    try {
      setList(await api.get<ProductionList>('/api/production'))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const send = async () => {
    setBusy(true)
    setNote(null)
    try {
      // The same dispatch the Printers tab uses: this screen is where the
      // unsent plates are visible, so it is where somebody wants the button.
      const waiting = totals?.plates_to_send ?? 0
      const result = await api.post<{ dispatched?: number; failed?: number }>(
        '/api/print-jobs/dispatch',
      )
      const sent = result?.dispatched ?? 0
      const failed = result?.failed ?? 0
      if (sent) {
        setNote(
          `Sent ${sent} plate${sent === 1 ? '' : 's'} to Bambuddy.` +
            (failed ? ` ${failed} would not go — see the Printers tab.` : ''),
        )
      } else if (waiting) {
        // Dispatch reports nothing sent when Bambuddy is not connected, which
        // is not the same as nothing to send — and saying so would send
        // somebody looking for plates that are still sitting right here.
        setNote(
          `Nothing reached Bambuddy, so ${waiting} plate${
            waiting === 1 ? ' is' : 's are'
          } still waiting. Check the Bambuddy connection in Settings.`,
        )
      } else {
        setNote('Nothing was waiting to be sent.')
      }
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const totals = list?.totals
  // Rows whose live plates already cover them are the machine's business, not
  // the bench's — shown on request rather than by default.
  const rows = (list?.items ?? []).filter(
    (item) => !hideCovered || item.units_outstanding > 0,
  )

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">To make</h1>
          <span className="text-sm text-ink-500">
            Every open order's unmade parts, added up by part.
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs text-ink-600">
              <input
                type="checkbox"
                checked={hideCovered}
                onChange={(event) => setHideCovered(event.target.checked)}
              />
              Hide what is already on a machine
            </label>
            <a
              className="rounded-md px-2.5 py-1 text-xs font-medium text-ink-600 underline"
              href="/api/production.csv"
            >
              Download CSV
            </a>
            {totals && totals.plates_to_send > 0 ? (
              <Button variant="primary" onClick={send} disabled={busy}>
                {busy
                  ? 'Sending…'
                  : `Send ${totals.plates_to_send} plate${
                      totals.plates_to_send === 1 ? '' : 's'
                    }`}
              </Button>
            ) : null}
          </div>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}
        {note ? <Alert tone="info">{note}</Alert> : null}

        {!list ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Tile
                label="Units to make"
                value={totals!.units_outstanding}
                hint={`${totals!.items} item${totals!.items === 1 ? '' : 's'} across ${
                  totals!.orders
                } order${totals!.orders === 1 ? '' : 's'}`}
              />
              <Tile
                label="Plates to send"
                value={totals!.plates_to_send}
                hint="Worked out, not yet on Bambuddy"
                tone={totals!.plates_to_send ? 'text-amber-700' : undefined}
              />
              <Tile
                label="On machines"
                value={totals!.plates_on_machines}
                hint="Queued or printing now"
                tone={totals!.plates_on_machines ? 'text-sky-700' : undefined}
              />
              <Tile
                label="Cannot start"
                value={totals!.blocked + list.unmatched.length}
                hint="No print file, or no product matched"
                tone={
                  totals!.blocked + list.unmatched.length ? 'text-red-700' : undefined
                }
              />
            </div>

            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">The list</h2>
                <span className="text-xs text-ink-500">
                  Longest wait first. A row opens for the orders behind it.
                </span>
              </div>

              {rows.length === 0 ? (
                <EmptyState
                  title={
                    list.items.length ? 'Everything is on a machine' : 'Nothing to make'
                  }
                  description={
                    list.items.length
                      ? 'Every outstanding part has a plate queued or printing. Untick the box above to see them.'
                      : 'Every open order has what it needs. New orders appear here within minutes of arriving.'
                  }
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[42rem] text-sm">
                    <thead>
                      <tr className="text-left text-xs uppercase tracking-wide text-ink-500">
                        <th className="px-3 py-2 font-medium">Item</th>
                        <th className="px-3 py-2 text-right font-medium">To make</th>
                        <th className="px-3 py-2 text-right font-medium">Plates</th>
                        <th className="px-3 py-2 text-right font-medium">Orders</th>
                        <th className="px-3 py-2 text-right font-medium">Waiting</th>
                        <th className="px-3 py-2 font-medium">Status</th>
                        <th className="w-6 px-3 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-ink-200">
                      {rows.map((item) => {
                        const key = `${item.product_id}:${item.variation_id ?? ''}`
                        return (
                          <Fragment key={key}>
                            <tr
                              role="button"
                              tabIndex={0}
                              onClick={() =>
                                setOpenRow((was) => (was === key ? null : key))
                              }
                              onKeyDown={(event) => {
                                if (event.key === 'Enter' || event.key === ' ') {
                                  event.preventDefault()
                                  setOpenRow((was) => (was === key ? null : key))
                                }
                              }}
                              className="cursor-pointer text-ink-700 hover:bg-ink-50"
                            >
                              <td className="px-3 py-2">
                                <span className="font-mono text-xs text-ink-500">
                                  {item.sku}
                                </span>
                                <span className="ml-2 font-medium text-ink-900">
                                  {item.name}
                                </span>
                                {item.variation_label ? (
                                  <span className="ml-1.5 text-xs text-ink-500">
                                    · {item.variation_label}
                                  </span>
                                ) : null}
                              </td>
                              <td className="px-3 py-2 text-right font-semibold tabular-nums text-ink-900">
                                {item.units_outstanding}
                                {item.units_covered ? (
                                  <span className="ml-1 text-xs font-normal text-ink-400">
                                    of {item.units_needed}
                                  </span>
                                ) : null}
                              </td>
                              <td className="px-3 py-2 text-right tabular-nums">
                                {item.plates_outstanding || '—'}
                                {item.units_per_plate && item.units_per_plate > 1 ? (
                                  <span className="ml-1 text-xs text-ink-400">
                                    @{item.units_per_plate}
                                  </span>
                                ) : null}
                              </td>
                              <td className="px-3 py-2 text-right tabular-nums">
                                {item.order_count}
                              </td>
                              <td className="px-3 py-2 text-right text-xs text-ink-600">
                                {waitedFor(item.waiting_since)}
                              </td>
                              <td className="px-3 py-2">
                                <Badge className={STAGE_CLASSES[item.stage]}>
                                  {item.stage_label}
                                </Badge>
                              </td>
                              <td className="px-3 py-2 text-xs text-ink-400">
                                {openRow === key ? '▾' : '▸'}
                              </td>
                            </tr>
                            {openRow === key ? (
                              <Behind item={item} onOpenOrder={setOpenOrderId} />
                            ) : null}
                          </Fragment>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            {list.assembly.length ? (
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                  <h2 className="text-sm font-semibold text-ink-800">To assemble</h2>
                  <span className="text-xs text-ink-500">
                    Bundles waiting to be put together. Tick them off in the order.
                  </span>
                </div>
                <div className="divide-y divide-ink-200">
                  {list.assembly.map((waiting) => (
                    <div
                      key={waiting.line_id}
                      role="button"
                      tabIndex={0}
                      onClick={() => setOpenOrderId(waiting.order_id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setOpenOrderId(waiting.order_id)
                        }
                      }}
                      className="flex w-full cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-ink-50"
                    >
                      <span className="font-mono text-xs font-semibold text-ink-900">
                        #{waiting.order_number}
                      </span>
                      <span className="text-sm text-ink-800">
                        {waiting.quantity} × {waiting.name}
                      </span>
                      <span className="text-xs text-ink-500">{waiting.sku}</span>
                      <span className="ml-auto text-xs text-ink-500">
                        {waitedFor(waiting.placed_at)}
                      </span>
                      {waiting.ready ? (
                        <Badge className="bg-emerald-100 text-emerald-800 ring-emerald-300">
                          parts ready
                        </Badge>
                      ) : (
                        <Badge className="bg-slate-100 text-slate-700 ring-slate-300">
                          {waiting.components_ready}/{waiting.components_total} parts made
                        </Badge>
                      )}
                    </div>
                  ))}
                </div>
              </Card>
            ) : null}

            {list.unmatched.length ? (
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                  <h2 className="text-sm font-semibold text-ink-800">
                    Waiting on a product
                  </h2>
                  <span className="text-xs text-ink-500">
                    Nothing says what these are, so nothing can be made for them.
                  </span>
                </div>
                <div className="divide-y divide-ink-200">
                  {list.unmatched.map((line) => (
                    <div
                      key={line.line_id}
                      role="button"
                      tabIndex={0}
                      onClick={() => setOpenOrderId(line.order_id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setOpenOrderId(line.order_id)
                        }
                      }}
                      className="flex w-full cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-ink-50"
                    >
                      <span className="font-mono text-xs font-semibold text-ink-900">
                        #{line.order_number}
                      </span>
                      <span className="font-mono text-xs text-ink-500">
                        {line.sku ?? 'no code'}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm text-ink-700">
                        {line.title ?? 'Unknown item'}
                      </span>
                      <span className="text-xs text-ink-500">
                        {waitedFor(line.placed_at)}
                      </span>
                      <span className="w-10 text-right text-sm tabular-nums text-ink-800">
                        {line.units} ×
                      </span>
                    </div>
                  ))}
                </div>
              </Card>
            ) : null}
          </>
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
