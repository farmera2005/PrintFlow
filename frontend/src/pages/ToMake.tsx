import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { ProductionList, ProductionRunning } from '../lib/types'
import { Alert, Badge, Card, EmptyState, Spinner, cx } from '../components/ui'

/** What the shop still has to make, and what is being made right now.
 *
 * A list of *things and quantities*, and nothing else. How the making
 * happens — which file, which machine, what was sent where — is the Printers
 * tab's business: somebody standing at a bench with this screen open wants to
 * know what to pick up next, not how the queue is plumbed.
 *
 * Every unit an open order is short of is in exactly one of three places:
 *
 * **Being made** sits at the top, because it is the only part of the screen
 * that changes on its own. It is drawn as a bar per thing rather than as a
 * row in the table, since "how far along is it" is a quantity a shape
 * answers faster than a number does.
 *
 * **To make** is the list proper: nothing has started on these, so they are
 * the work. Longest wait first — the thing that has kept an order waiting
 * longest is the one to put on next.
 *
 * **To gather** is what comes off a shelf instead of a machine, which is the
 * other half of getting an order out and appears on no other screen.
 */

/* The two colours the screen uses for quantities, and the only two. Emerald
   already means finished everywhere else in PrintFlow and amber already
   means in progress, so this borrows the language rather than inventing a
   third. The pair clears colour-blind separation and 3:1 against the
   surface, which matters on the bar, where a segment's meaning is carried by
   its colour as well as by the number beside it. */
const MADE_FILL = 'bg-emerald-700'
const HAPPENING_FILL = 'bg-amber-600'

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

export default function ToMake() {
  const [list, setList] = useState<ProductionList | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  // Work moves on its own, so the screen does too — slowly, because a shop
  // floor display left open all day should not be a poll every second.
  useEffect(() => {
    const timer = window.setInterval(load, 60_000)
    return () => window.clearInterval(timer)
  }, [load])

  const totals = list?.totals

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">To make</h1>
          <span className="text-sm text-ink-500">
            Everything open orders are short of, added up by item.
          </span>
          <a
            className="ml-auto rounded-md px-2.5 py-1 text-xs font-medium text-ink-600 underline"
            href="/api/production.csv"
          >
            Download CSV
          </a>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!list ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Tile
                label="To make"
                value={totals!.units_to_make}
                hint={`${totals!.items} item${totals!.items === 1 ? '' : 's'}, for ${
                  totals!.orders
                } order${totals!.orders === 1 ? '' : 's'}`}
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
                label="To gather"
                value={list.gather.reduce((sum, part) => sum + part.units, 0)}
                hint="Off a shelf rather than made"
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
                    <Running
                      key={`${item.product_id}:${item.variation_id ?? ''}`}
                      item={item}
                    />
                  ))}
                </div>
              </Card>
            ) : null}

            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">To make</h2>
                <span className="text-xs text-ink-500">
                  Nothing has started on these. Longest wait first.
                </span>
              </div>

              {list.items.length === 0 ? (
                <EmptyState
                  title={
                    list.in_production.length
                      ? 'Nothing waiting to be started'
                      : 'Nothing to make'
                  }
                  description={
                    list.in_production.length
                      ? 'Everything open orders are short of is already being made.'
                      : 'Every open order has what it needs. New orders appear here within minutes of arriving.'
                  }
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[30rem] text-sm">
                    <thead>
                      <tr className="text-left text-xs uppercase tracking-wide text-ink-500">
                        <th className="px-3 py-2 font-medium">Item</th>
                        <th className="px-3 py-2 text-right font-medium">Quantity</th>
                        <th className="px-3 py-2 text-right font-medium">Waiting</th>
                        <th className="px-3 py-2 font-medium" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-ink-200">
                      {list.items.map((item) => (
                        <tr
                          key={`${item.product_id}:${item.variation_id ?? ''}`}
                          className="text-ink-700"
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
                            {item.for_bundle ? (
                              <span className="ml-1.5 text-xs text-ink-400">
                                · goes into a bundle
                              </span>
                            ) : null}
                          </td>
                          <td className="px-3 py-2 text-right">
                            <span className="text-base font-semibold tabular-nums text-ink-900">
                              {item.units_to_make}
                            </span>
                            {item.units_to_make !== item.units_needed ? (
                              <span className="ml-1 text-xs font-normal text-ink-400">
                                of {item.units_needed}
                              </span>
                            ) : null}
                          </td>
                          <td className="px-3 py-2 text-right text-xs text-ink-600">
                            {since(item.waiting_since)}
                          </td>
                          <td className="px-3 py-2">
                            {!item.can_start ? (
                              <Badge className="bg-red-100 text-red-800 ring-red-300">
                                Cannot be made yet
                              </Badge>
                            ) : null}
                          </td>
                        </tr>
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

            {list.gather.length ? (
              <Card className="overflow-hidden">
                <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                  <h2 className="text-sm font-semibold text-ink-800">To gather</h2>
                  <span className="text-xs text-ink-500">
                    Off a shelf rather than made: the parts that go into bundles
                    waiting to be built, and whatever an order takes from stock on
                    its own.
                  </span>
                </div>
                <div className="divide-y divide-ink-200">
                  {list.gather.map((part) => (
                    <div
                      key={`${part.product_id ?? part.sku}:${part.variation_id ?? ''}`}
                      className="flex flex-wrap items-center gap-2 px-3 py-2"
                    >
                      <span className="w-8 text-right text-base font-semibold tabular-nums text-ink-900">
                        {part.units}
                      </span>
                      <span className="font-mono text-xs text-ink-500">{part.sku}</span>
                      <span className="text-sm text-ink-800">{part.name}</span>
                      {part.variation_label ? (
                        <span className="text-xs text-ink-500">
                          · {part.variation_label}
                        </span>
                      ) : null}
                      {/* What it is being built into. A pile of parts with
                          nothing to build is not a job either — and an item
                          that is not part of a bundle simply is the thing,
                          so it says nothing. */}
                      {part.bundles.length ? (
                        <span className="text-xs text-ink-400">
                          → {part.bundles.join(', ')}
                        </span>
                      ) : null}
                      {part.units_waiting ? (
                        <Badge className="bg-slate-100 text-slate-700 ring-slate-300">
                          {part.units_waiting} more once those bundles are made
                        </Badge>
                      ) : null}
                      <span className="ml-auto text-xs text-ink-500">
                        {since(part.waiting_since)}
                      </span>
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
