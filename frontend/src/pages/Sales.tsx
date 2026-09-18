import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  SOURCE_CLASSES,
  SOURCE_LABELS,
  formatDateTime,
  formatDay,
  formatMoney,
} from '../lib/format'
import type { OrderSource, OrderStatus, SalesReport, SalesPeriod } from '../lib/types'
import OrderDrawer from '../components/OrderDrawer'
import { Alert, Badge, Card, EmptyState, Spinner, cx } from '../components/ui'

/** What the shop sold, at four zoom levels.
 *
 * The figures are the orders' own — this screen adds no arithmetic of its own,
 * the database does the grouping — so the design problem here is only ever
 * *which* number goes where. Three decisions carry it:
 *
 * **Sold, not shipped.** A period holds the orders placed in it. An order
 * posted late belongs to the month it was bought in, which is the month
 * somebody is asking about.
 *
 * **Two kinds of done, shown apart.** Money from orders that went out of the
 * door is not the same money as orders still being printed, so every row
 * splits into Processed and In progress rather than presenting one total that
 * quietly mixes them.
 *
 * **The bar is the same two numbers.** It carries no information the columns
 * do not, which is the point: it is there so a glance down the table finds the
 * period worth reading, and the reading is done in the figures.
 */

const GRAINS: { key: SalesReport['grain']; label: string }[] = [
  { key: 'week', label: 'Weekly' },
  { key: 'month', label: 'Monthly' },
  { key: 'quarter', label: 'Quarterly' },
  { key: 'year', label: 'Annual' },
]

/** How many periods to offer, per grain. The first is the default the API
 *  would have picked anyway, so the selector starts where the report does. */
const SPANS: Record<SalesReport['grain'], number[]> = {
  week: [13, 26, 52],
  month: [12, 24, 36],
  quarter: [8, 12, 20],
  year: [5, 10],
}

/* Two series, and the only two colours on this screen. Emerald already means
   shipped everywhere else in PrintFlow and amber already means in production,
   so the report borrows the language rather than inventing a third one. The
   pair clears colour-blind separation and 3:1 against the surface, which
   matters because the bar is the one place the split is not also written. */
const DONE_FILL = 'bg-emerald-700'
const LIVE_FILL = 'bg-amber-600'

function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
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

/** One period's split, as a length.
 *
 * Scaled against the biggest period in the range rather than against its own
 * total, or every row would be full and the bar would say nothing. Segments
 * are separated by a surface-coloured gap so two fills never touch. */
function SplitBar({
  done,
  live,
  max,
  currency,
}: {
  done: number
  live: number
  max: number
  currency: string | null
}) {
  const scale = (amount: number) => (max > 0 ? Math.max((amount / max) * 100, amount > 0 ? 1.5 : 0) : 0)
  return (
    <div className="flex h-2.5 w-full items-stretch gap-[2px] overflow-hidden rounded-full bg-ink-100">
      <div
        className={cx('rounded-full', DONE_FILL)}
        style={{ width: `${scale(done)}%` }}
        title={`Processed ${formatMoney(String(done), currency)}`}
      />
      <div
        className={cx('rounded-full', LIVE_FILL)}
        style={{ width: `${scale(live)}%` }}
        title={`In progress ${formatMoney(String(live), currency)}`}
      />
    </div>
  )
}

/** The parts behind a period's sales, opened one row at a time.
 *
 * Kept shut by default: the question is nearly always "how much", and the
 * answer to "made up of what" is four more columns nobody was reading. */
function Breakdown({ period, currency }: { period: SalesPeriod; currency: string | null }) {
  const rows: [string, string][] = [
    ['Items', period.all.items_total],
    ['Shipping', period.all.shipping_total],
    ['Tax', period.all.tax_total],
    ['Discount', period.all.discount_total],
    ['Channel fees', period.all.etsy_fees],
    ['Marketing', period.all.marketing_fees],
    ['Card processing', period.all.processing_fees],
    ['Postage', period.all.label_cost],
  ]
  return (
    <tr className="bg-ink-50/60">
      <td colSpan={8} className="px-3 py-2">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
          {rows.map(([label, amount]) => (
            <div key={label} className="flex justify-between gap-2 text-xs">
              <dt className="text-ink-500">{label}</dt>
              <dd className="tabular-nums text-ink-700">
                {formatMoney(amount, currency)}
              </dd>
            </div>
          ))}
        </dl>
        <p className="mt-2 text-xs text-ink-500">
          {period.cancelled_orders > 0
            ? `${period.cancelled_orders} cancelled order${
                period.cancelled_orders === 1 ? '' : 's'
              } in this period, counted in none of the figures above.`
            : 'Nothing was cancelled in this period.'}
        </p>
      </td>
    </tr>
  )
}

export default function Sales() {
  const [grain, setGrain] = useState<SalesReport['grain']>('month')
  const [span, setSpan] = useState<number | null>(null)
  const [report, setReport] = useState<SalesReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openRow, setOpenRow] = useState<string | null>(null)
  const [openOrderId, setOpenOrderId] = useState<string | null>(null)

  // The browser's own zone, so a week breaks where the shop's week does rather
  // than where UTC's does — a Sunday-evening sale belongs to the week it felt
  // like, not to the one it was in Greenwich.
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone

  const query = useCallback(() => {
    const params = new URLSearchParams({ grain })
    if (span) params.set('periods', String(span))
    if (timezone) params.set('tz', timezone)
    return params
  }, [grain, span, timezone])

  const load = useCallback(async () => {
    try {
      setReport(await api.get<SalesReport>(`/api/sales/report?${query()}`))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [query])

  useEffect(() => {
    load()
  }, [load])

  const currency = report?.currency ?? null
  const periods = report?.periods ?? []
  const biggest = Math.max(
    1,
    ...periods.map((period) => Number(period.all.revenue) || 0),
  )

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Sales</h1>
          <div className="flex flex-wrap gap-1">
            {GRAINS.map((option) => (
              <button
                key={option.key}
                type="button"
                onClick={() => {
                  setGrain(option.key)
                  // The spans differ per grain, so a choice made for months
                  // would be a strange number of weeks.
                  setSpan(null)
                  setOpenRow(null)
                }}
                className={cx(
                  'rounded-md px-2.5 py-1 text-xs font-medium',
                  grain === option.key
                    ? 'bg-ink-900 text-white'
                    : 'bg-white text-ink-600 ring-1 ring-ink-300',
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1.5 text-xs text-ink-600">
            Showing
            <select
              className="rounded-md border-0 bg-white py-1 pl-2 pr-7 text-xs text-ink-800 ring-1 ring-inset ring-ink-300"
              value={span ?? SPANS[grain][0]}
              onChange={(event) => setSpan(Number(event.target.value))}
            >
              {SPANS[grain].map((count) => (
                <option key={count} value={count}>
                  {count} {grain === 'quarter' ? 'quarters' : `${grain}s`}
                </option>
              ))}
            </select>
          </label>
          <a
            className="ml-auto rounded-md px-2.5 py-1 text-xs font-medium text-ink-600 underline"
            href={`/api/sales/report.csv?${query()}`}
          >
            Download CSV
          </a>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!report ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Tile
                label="Sold"
                value={formatMoney(report.totals.all.revenue, currency)}
                hint={`${report.totals.all_orders} order${
                  report.totals.all_orders === 1 ? '' : 's'
                } placed ${formatDay(report.from)} – ${formatDay(report.to)}`}
              />
              <Tile
                label="Processed"
                value={formatMoney(report.totals.done.revenue, currency)}
                hint={`${report.totals.done_orders} shipped or complete`}
                tone="text-emerald-700"
              />
              <Tile
                label="In progress"
                value={formatMoney(report.totals.live.revenue, currency)}
                hint={`${report.totals.live_orders} still in the shop`}
                tone="text-amber-700"
              />
              <Tile
                label="Net"
                value={formatMoney(report.totals.all.net, currency)}
                hint="After fees and postage"
              />
            </div>

            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-center gap-3 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">
                  {report.grain_label} breakdown
                </h2>
                {/* Two series, so the legend is not optional — and the words
                    are what carry the identity, not the squares. */}
                <span className="flex items-center gap-1.5 text-xs text-ink-600">
                  <span className={cx('h-2.5 w-2.5 rounded-full', DONE_FILL)} />
                  Processed
                </span>
                <span className="flex items-center gap-1.5 text-xs text-ink-600">
                  <span className={cx('h-2.5 w-2.5 rounded-full', LIVE_FILL)} />
                  In progress
                </span>
                <span className="ml-auto text-xs text-ink-500">
                  Dated when the order was placed · {report.timezone}
                </span>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full min-w-[46rem] text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-ink-500">
                      <th className="px-3 py-2 font-medium">Period</th>
                      <th className="px-3 py-2 text-right font-medium">Orders</th>
                      <th className="px-3 py-2 text-right font-medium">Sold</th>
                      <th className="w-32 px-3 py-2 font-medium">Split</th>
                      <th className="px-3 py-2 text-right font-medium">Processed</th>
                      <th className="px-3 py-2 text-right font-medium">In progress</th>
                      <th className="px-3 py-2 text-right font-medium">Invoiced</th>
                      <th className="px-3 py-2 text-right font-medium">Net</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-ink-200">
                    {periods.map((period) => (
                      <Fragment key={period.start}>
                        <tr
                          role="button"
                          tabIndex={0}
                          onClick={() =>
                            setOpenRow((was) => (was === period.start ? null : period.start))
                          }
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault()
                              setOpenRow((was) =>
                                was === period.start ? null : period.start,
                              )
                            }
                          }}
                          className={cx(
                            'cursor-pointer hover:bg-ink-50',
                            period.all_orders === 0 ? 'text-ink-400' : 'text-ink-700',
                          )}
                        >
                          <td className="whitespace-nowrap px-3 py-2 font-medium text-ink-800">
                            {period.label}
                            {period.partial ? (
                              <span className="ml-1.5 text-xs font-normal text-ink-500">
                                so far
                              </span>
                            ) : null}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {period.all_orders}
                          </td>
                          <td className="px-3 py-2 text-right font-medium tabular-nums text-ink-900">
                            {formatMoney(period.all.revenue, currency)}
                          </td>
                          <td className="px-3 py-2">
                            <SplitBar
                              done={Number(period.done.revenue)}
                              live={Number(period.live.revenue)}
                              max={biggest}
                              currency={currency}
                            />
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {formatMoney(period.done.revenue, currency)}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {formatMoney(period.live.revenue, currency)}
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {formatMoney(period.invoiced_total, currency)}
                            {/* Of how many, because "how much is billed" and
                                "how many are billed" are different questions
                                and the gap between them is the work left. */}
                            <span
                              className="ml-1.5 text-xs text-ink-400"
                              title={`${period.invoiced_orders} of ${period.all_orders} orders invoiced in QuickBooks`}
                            >
                              {period.invoiced_orders}/{period.all_orders}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-right tabular-nums">
                            {formatMoney(period.all.net, currency)}
                          </td>
                        </tr>
                        {openRow === period.start ? (
                          <Breakdown period={period} currency={currency} />
                        ) : null}
                      </Fragment>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="border-t-2 border-ink-300 bg-ink-50 font-medium text-ink-900">
                      <td className="px-3 py-2">Total</td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {report.totals.all_orders}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {formatMoney(report.totals.all.revenue, currency)}
                      </td>
                      <td />
                      <td className="px-3 py-2 text-right tabular-nums">
                        {formatMoney(report.totals.done.revenue, currency)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {formatMoney(report.totals.live.revenue, currency)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {formatMoney(report.totals.invoiced_total, currency)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {formatMoney(report.totals.all.net, currency)}
                      </td>
                    </tr>
                  </tfoot>
                </table>
              </div>
              <p className="border-t border-ink-200 px-3 py-2 text-xs text-ink-500">
                Cancelled orders are in none of these figures. A period row opens
                for the items, postage, tax and fees behind it.
              </p>
            </Card>

            {report.channels.length > 1 ? (
              <Card className="p-3">
                <h2 className="text-sm font-semibold text-ink-800">By channel</h2>
                <div className="mt-2 space-y-2">
                  {report.channels.map((channel) => (
                    <div key={channel.source} className="flex flex-wrap items-center gap-2">
                      <Badge className={SOURCE_CLASSES[channel.source as OrderSource]}>
                        {SOURCE_LABELS[channel.source as OrderSource]}
                      </Badge>
                      <span className="text-xs text-ink-500">
                        {channel.orders} order{channel.orders === 1 ? '' : 's'}
                      </span>
                      <span className="ml-auto text-sm font-medium tabular-nums text-ink-900">
                        {formatMoney(channel.revenue, currency)}
                      </span>
                      <div className="w-full">
                        <SplitBar
                          done={Number(channel.done_revenue)}
                          live={Number(channel.live_revenue)}
                          max={Math.max(
                            1,
                            ...report.channels.map((row) => Number(row.revenue) || 0),
                          )}
                          currency={currency}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              </Card>
            ) : null}

            <Card className="overflow-hidden">
              <div className="flex flex-wrap items-baseline gap-2 border-b border-ink-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-ink-800">Still in progress</h2>
                <span className="text-xs text-ink-500">
                  Every order in the shop, oldest first — whenever it was placed.
                </span>
              </div>
              {report.open_orders.length === 0 ? (
                <EmptyState
                  title="Nothing in the shop"
                  description="Every order has shipped or completed."
                />
              ) : (
                <div className="divide-y divide-ink-200">
                  {report.open_orders.map((order) => (
                    <div
                      key={order.id}
                      role="button"
                      tabIndex={0}
                      onClick={() => setOpenOrderId(order.id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          setOpenOrderId(order.id)
                        }
                      }}
                      className="flex w-full cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-left hover:bg-ink-50"
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
                      {order.invoiced ? (
                        <Badge className="bg-emerald-100 text-emerald-800 ring-emerald-300">
                          Invoiced
                        </Badge>
                      ) : null}
                      <Badge className="bg-amber-100 text-amber-900 ring-amber-300">
                        {COLUMN_LABELS[order.status as OrderStatus]}
                      </Badge>
                      <span className="w-20 text-right text-sm tabular-nums text-ink-800">
                        {order.revenue ? formatMoney(order.revenue, order.currency) : '—'}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
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
