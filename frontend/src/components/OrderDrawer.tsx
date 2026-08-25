import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  ORDER_STATUSES,
  JOB_STATUS_CLASSES,
  LINE_STATE_CLASSES,
  LINE_STATE_LABELS,
  SOURCE_CLASSES,
  SOURCE_LABELS,
  TRACKING_CLASSES,
  TRACKING_LABELS,
  formatDateTime,
  formatMoney,
} from '../lib/format'
import type {
  InvoiceNumbering,
  Order,
  OrderLine,
  OrderStatus,
  Product,
} from '../lib/types'
import LabelDialog from './LabelDialog'
import { Alert, Badge, Button, Field, Modal, Spinner, cx, inputClass } from './ui'

/** The drawer's three views of one order.
 *
 *  It had grown into a single scroll holding what is made, where it goes and
 *  what it earned — three questions asked at three different moments, by
 *  people doing three different jobs. Splitting them costs one click and stops
 *  the address being somewhere below the print queue. */
const TABS = [
  { key: 'lines', label: 'Lines' },
  { key: 'shipping', label: 'Shipping' },
  { key: 'money', label: 'Money' },
] as const

type Tab = (typeof TABS)[number]['key']

/** What to remember about the listing or catalogue item when a product is picked. */
export interface RememberChoice {
  remember: boolean
  remember_scope: 'listing' | 'variant'
}

/** What this line is called on the channel it came from.
 *
 * Both channels have the same two ideas — the thing being sold, and which
 * variation of it — under different names and different id types. The picker
 * only needs the two ids and the words to put around them. */
function channelIdentity(line: OrderLine | null): {
  channel: 'Etsy' | 'Wix'
  itemId: string | null
  variantId: string | null
  itemWord: string
  variantWord: string
  caveat: string | null
} {
  if (line?.wix_catalog_item_id) {
    return {
      channel: 'Wix',
      itemId: line.wix_catalog_item_id,
      variantId: line.wix_variant_id,
      itemWord: 'item',
      variantWord: 'variant',
      caveat: null,
    }
  }
  return {
    channel: 'Etsy',
    itemId: line?.etsy_listing_id != null ? String(line.etsy_listing_id) : null,
    variantId: line?.etsy_product_id != null ? String(line.etsy_product_id) : null,
    itemWord: 'listing',
    variantWord: 'variation',
    caveat:
      "Etsy issues a new id for a variation whenever the listing's options are " +
      'edited, so a variation link stops matching after the next edit. Prefer the ' +
      'whole listing and let option rules handle the differences.',
  }
}

function ProductPicker({
  open,
  onClose,
  onPick,
  line,
}: {
  open: boolean
  onClose: () => void
  onPick: (product: Product, remember: RememberChoice) => void
  line: OrderLine | null
}) {
  const skuHint = line?.sku_raw ?? null
  const { channel, itemId, variantId, itemWord, variantWord, caveat } =
    channelIdentity(line)
  const listingId = itemId

  const [query, setQuery] = useState(skuHint ?? '')
  const [products, setProducts] = useState<Product[]>([])
  const [busy, setBusy] = useState(false)
  // Remembering is the whole point: the listing id is what future orders match
  // on, so a link made once fixes the listing rather than this one order.
  const [remember, setRemember] = useState(false)
  const [scope, setScope] = useState<'listing' | 'variant'>('listing')

  useEffect(() => {
    if (!open) return
    setQuery(skuHint ?? '')
    setRemember(Boolean(listingId))
    setScope('listing')
  }, [open, skuHint, listingId])

  useEffect(() => {
    if (!open) return
    setBusy(true)
    api
      .get<{ products: Product[] }>(`/api/products?q=${encodeURIComponent(query)}&limit=50`)
      .then((data) => setProducts(data.products))
      .finally(() => setBusy(false))
  }, [open, query])

  return (
    <Modal open={open} title="Link a product to this line" onClose={onClose}>
      <p className="mb-3 text-sm text-ink-600">
        {listingId ? (
          <>
            This line came from {channel} {itemWord}{' '}
            <code className="font-mono">{listingId}</code>
            {variantId ? (
              <>
                , {variantWord} <code className="font-mono">{variantId}</code>
              </>
            ) : null}
            .{' '}
          </>
        ) : null}
        Pick the product it should map to — intake re-runs for this line
        immediately.
      </p>
      <input
        className={inputClass}
        placeholder="Search products…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {listingId ? (
        <div className="mt-3 rounded-md bg-ink-100 p-2.5">
          <label className="flex items-start gap-2 text-sm text-ink-700">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            />
            <span>
              Remember this {itemWord}, so future orders match on their own — and clear
              any orders already waiting on it.
            </span>
          </label>
          {remember && variantId ? (
            <select
              className={cx(inputClass, 'mt-2')}
              value={scope}
              onChange={(e) => setScope(e.target.value as 'listing' | 'variant')}
            >
              <option value="listing">
                The whole {itemWord} ({listingId})
              </option>
              <option value="variant">
                Only this {variantWord} ({variantId})
              </option>
            </select>
          ) : null}
          {remember && variantId && scope === 'variant' && caveat ? (
            <p className="mt-1.5 text-xs text-ink-500">{caveat}</p>
          ) : null}
        </div>
      ) : null}

      <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
        {busy ? <Spinner /> : null}
        {!busy && products.length === 0 ? (
          <p className="py-4 text-center text-sm text-ink-500">
            No products match. Create one on the Products screen first.
          </p>
        ) : null}
        {products.map((product) => (
          <button
            key={product.id}
            type="button"
            onClick={() => onPick(product, { remember, remember_scope: scope })}
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-ink-50"
          >
            <span className="font-mono text-sm text-ink-800">{product.sku}</span>
            <span className="min-w-0 flex-1 truncate text-sm text-ink-600">
              {product.name}
            </span>
            <Badge>{product.fulfillment}</Badge>
          </button>
        ))}
      </div>
    </Modal>
  )
}

/** The order's column, and a way to say it is something else.
 *
 * Almost always the roll-up is right, so the plain case is a label. What it
 * cannot know is the phone call: a buyer who cancelled, an order handed over
 * in person. Setting one is deliberate and reversible, and the drawer says
 * which of the two it is looking at. */
function StatusPicker({
  order,
  onSet,
}: {
  order: Order
  onSet: (status: OrderStatus, note?: string) => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const suggests =
    order.suggested_status && order.suggested_status !== order.status
      ? order.suggested_status
      : null

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge
        className="bg-ink-100 text-ink-700 ring-ink-300"
        title={order.status_note ?? undefined}
      >
        {COLUMN_LABELS[order.status]}
      </Badge>
      {open ? (
        <>
          <select
            className={cx(inputClass, 'w-auto py-1 text-xs')}
            value={order.status}
            onChange={(e) => {
              setOpen(false)
              onSet(e.target.value as OrderStatus)
            }}
          >
            {ORDER_STATUSES.map((value) => (
              <option key={value} value={value}>
                {COLUMN_LABELS[value]}
              </option>
            ))}
          </select>
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </>
      ) : (
        <>
          <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
            Move
          </Button>
          {/* The rules never move a card. When they disagree they offer. */}
          {suggests ? (
            <Button size="sm" variant="ghost" onClick={() => onSet(suggests)}>
              Looks {COLUMN_LABELS[suggests].toLowerCase()} → move it
            </Button>
          ) : null}
        </>
      )}
    </div>
  )
}

function LineRow({
  line,
  depth,
  bambuddyBase,
  onAction,
}: {
  line: OrderLine
  depth: number
  bambuddyBase: string | null | undefined
  onAction: (action: string, line: OrderLine, payload?: unknown) => Promise<void>
}) {
  const [open, setOpen] = useState(true)
  const [busy, setBusy] = useState(false)

  const run = async (action: string, payload?: unknown) => {
    setBusy(true)
    try {
      await onAction(action, line, payload)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li>
      <div
        className={cx(
          'rounded-md border border-ink-200 bg-white p-2.5',
          line.state === 'unmatched' && 'border-red-300 bg-red-50',
        )}
        style={{ marginLeft: depth * 16 }}
      >
        <div className="flex flex-wrap items-center gap-2">
          {line.is_bundle ? (
            <button
              type="button"
              className="text-xs text-ink-500"
              onClick={() => setOpen((v) => !v)}
              aria-label={open ? 'Collapse bundle' : 'Expand bundle'}
            >
              {open ? '▾' : '▸'}
            </button>
          ) : null}
          <span className="text-sm text-ink-500">{line.quantity}×</span>
          {/* The name is what the operator recognises; the code is a handle,
              and plenty of products no longer have one worth showing. */}
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink-900">
            {line.name ?? line.sku ?? 'Unnamed item'}
          </span>
          {line.sku ? (
            <span className="font-mono text-xs text-ink-500">{line.sku}</span>
          ) : null}
          {line.variation_label ? (
            <Badge className="bg-sky-100 text-sky-800 ring-sky-300">
              {line.variation_label}
            </Badge>
          ) : null}
          <Badge className={LINE_STATE_CLASSES[line.state]}>
            {LINE_STATE_LABELS[line.state]}
          </Badge>
          {line.override_state ? <Badge>manual override</Badge> : null}
          {line.force_print ? <Badge>forced print</Badge> : null}
        </div>

        {!line.is_bundle && line.product_id ? (
          <p className="mt-1 text-xs text-ink-500">
            {line.qty_from_stock} from stock · {line.qty_to_print} to print
          </p>
        ) : null}

        {/* What the buyer picked. The person making this needs to read it
            here, not in Etsy — and free text is the whole point of
            personalisation, so it gets room to be read. */}
        {line.variations?.length ? (
          <ul className="mt-1 space-y-0.5">
            {line.variations.map((variation, i) => (
              <li key={i} className="text-xs">
                <span className="text-ink-500">{variation.name}:</span>{' '}
                <span
                  className={
                    variation.free_text
                      ? 'font-medium text-ink-900'
                      : 'font-medium text-ink-800'
                  }
                >
                  {variation.value}
                </span>
              </li>
            ))}
          </ul>
        ) : null}

        {line.option_effects?.length ? (
          <p className="mt-1 text-xs text-violet-700">
            {line.option_effects.join(' · ')}
          </p>
        ) : null}

        {line.stock_note ? (
          <p className="mt-1 text-xs text-amber-800">{line.stock_note}</p>
        ) : null}

        {/* What QuickBooks was told about these units. A removal that quietly
            did not happen is the failure worth designing against, so the line
            says either that it is done or what went wrong. */}
        {line.qbo_stock_removed_at ? (
          <p className="mt-1 text-xs text-emerald-800">
            {line.qbo_stock_qty ?? line.quantity} out of QuickBooks stock ·{' '}
            {/* Which of the two events booked it. They are undone by different
                things, so a line that says only "out of stock" leaves somebody
                guessing what putting it back would mean. */}
            {line.qbo_stock_reason === 'assembled' ? 'consumed assembling' : 'printed'} ·{' '}
            {formatDateTime(line.qbo_stock_removed_at)}
          </p>
        ) : null}
        {line.qbo_stock_error ? (
          <p className="mt-1 text-xs text-red-700">
            QuickBooks stock not updated: {line.qbo_stock_error}
          </p>
        ) : null}

        {line.print_jobs.length ? (
          <ul className="mt-2 space-y-1">
            {line.print_jobs.map((job) => (
              <li key={job.id} className="flex flex-wrap items-center gap-2 text-xs">
                <Badge className={JOB_STATUS_CLASSES[job.status]}>{job.status}</Badge>
                <span className="text-ink-500">
                  plate {job.plate_number ?? '—'} · {job.units_expected} units
                </span>
                {job.bambuddy_queue_id && bambuddyBase ? (
                  <a
                    className="text-ink-600 underline"
                    href={job.bambuddy_url ?? `${bambuddyBase}/queue/${job.bambuddy_queue_id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    queue #{job.bambuddy_queue_id}
                  </a>
                ) : null}
                {job.error ? <span className="text-red-700">{job.error}</span> : null}
                {job.status === 'failed' || job.status === 'cancelled' ? (
                  <Button
                    size="sm"
                    onClick={() => run('requeue-job', job.id)}
                    disabled={busy}
                  >
                    Re-queue
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        <div className="mt-2 flex flex-wrap gap-1.5">
          {line.state === 'unmatched' ? (
            <Button size="sm" variant="primary" onClick={() => run('link')} disabled={busy}>
              Link product
            </Button>
          ) : null}
          {/* A wrong match used to be permanent. Letting go of it puts the
              line back where the picker can reach it. */}
          {line.state !== 'unmatched' && line.parent_line_id === null && line.product_id ? (
            <Button size="sm" variant="ghost" onClick={() => run('unmatch')} disabled={busy}>
              Change product
            </Button>
          ) : null}
          {line.is_bundle ? (
            <Button
              size="sm"
              variant={line.assembled_at ? 'ghost' : 'primary'}
              onClick={() => run('assemble', !line.assembled_at)}
              disabled={busy}
            >
              {line.assembled_at
                ? `Assembled ${formatDateTime(line.assembled_at)} — undo`
                : 'Assembly complete'}
            </Button>
          ) : null}
          {!line.is_bundle && line.product_id && line.state !== 'cancelled' ? (
            <>
              {line.override_state ? (
                <Button size="sm" onClick={() => run('override', 'clear')} disabled={busy}>
                  Clear override
                </Button>
              ) : (
                <Button
                  size="sm"
                  onClick={() => run('override', 'mark_printed')}
                  disabled={busy}
                  title="For a print done outside Bambuddy"
                >
                  Mark printed
                </Button>
              )}
              {/* Only a printed product has a print path to fall back on. */}
              {line.fulfillment === 'printed' && !line.force_print && line.qty_from_stock > 0 ? (
                <Button size="sm" onClick={() => run('force-print', true)} disabled={busy}>
                  Skip stock, print anyway
                </Button>
              ) : null}
              {line.force_print ? (
                <Button size="sm" onClick={() => run('force-print', false)} disabled={busy}>
                  Use stock again
                </Button>
              ) : null}
              {/* The way back in when the removal could not be made —
                  QuickBooks was down, or an account had not been chosen yet.
                  Booking twice is impossible; the line remembers. */}
              {line.qbo_stock_error && !line.qbo_stock_removed_at ? (
                <Button
                  size="sm"
                  onClick={() => run('stock-removal')}
                  disabled={busy}
                  title="Try the QuickBooks stock removal again"
                >
                  Retry stock removal
                </Button>
              ) : null}
              {/* Cancelling a line puts its stock back on its own. This is the
                  other case: a line marked printed that was not. Clearing the
                  override cannot be trusted to mean "undo the books" — it is
                  just as often a tidy-up after a print that really did finish
                  — so the reversal is its own deliberate button. */}
              {line.qbo_stock_removed_at ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => run('restore-stock')}
                  disabled={busy}
                  title="Delete the QuickBooks Purchase and put these units back"
                >
                  Put stock back
                </Button>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => run('override', 'cancel')}
                disabled={busy}
              >
                Cancel line
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {line.children.length && open ? (
        <ul className="mt-1.5 space-y-1.5">
          {line.children.map((child) => (
            <LineRow
              key={child.id}
              line={child}
              depth={depth + 1}
              bambuddyBase={bambuddyBase}
              onAction={onAction}
            />
          ))}
        </ul>
      ) : null}
    </li>
  )
}

export default function OrderDrawer({
  orderId,
  onClose,
  onChanged,
}: {
  orderId: string
  onClose: () => void
  onChanged: () => void
}) {
  const [order, setOrder] = useState<Order | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('lines')
  const [pickerLine, setPickerLine] = useState<OrderLine | null>(null)
  const [labelOpen, setLabelOpen] = useState(false)
  const [matching, setMatching] = useState(false)

  const load = useCallback(async () => {
    try {
      setOrder(await api.get<Order>(`/api/orders/${orderId}`))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [orderId])

  useEffect(() => {
    load()
  }, [load])

  const apply = async (promise: Promise<unknown>) => {
    try {
      const updated = (await promise) as Order
      // Array.isArray, not `'lines' in updated`: a response that carries a
      // `lines` key of the wrong shape used to be handed straight to the
      // renderer, which then called .map on it and took the whole drawer down.
      // Reloading is always a safe answer; a blank screen never is.
      if (updated && typeof updated === 'object' && Array.isArray(updated.lines)) {
        setOrder(updated)
      } else {
        await load()
      }
      setError(null)
      onChanged()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const handleAction = async (action: string, line: OrderLine, payload?: unknown) => {
    const base = `/api/orders/${orderId}/lines/${line.id}`
    if (action === 'link') {
      setPickerLine(line)
      return
    }
    if (action === 'unmatch') {
      if (
        !window.confirm(
          'Let go of this product? Anything queued for it that has not reached ' +
            'Bambuddy yet is dropped, and the line goes back to needing a match.',
        )
      )
        return
      setNotice(null)
      await apply(
        api
          .post<Order & { unmatched?: { kept_jobs: number } }>(`${base}/unmatch`)
          .then((updated) => {
            const kept = updated.unmatched?.kept_jobs ?? 0
            if (kept) {
              setNotice(
                `${kept} print job${kept === 1 ? '' : 's'} already on the Bambuddy ` +
                  'queue were left as they are.',
              )
            }
            return updated
          }),
      )
      return
    }
    if (action === 'requeue-job') {
      await apply(api.post(`/api/print-jobs/${payload}/requeue`).then(() => null))
      return
    }
    if (action === 'assemble') {
      await apply(api.post(`${base}/assemble`, { assembled: payload }))
      return
    }
    if (action === 'force-print') {
      await apply(api.post(`${base}/force-print`, { force_print: payload }))
      return
    }
    if (action === 'stock-removal') {
      await apply(api.post(`${base}/stock-removal`))
      return
    }
    if (action === 'restore-stock') {
      if (
        !window.confirm(
          'Put these units back into QuickBooks stock? The Purchase that took ' +
            'them out is deleted, which reverses every quantity it moved.',
        )
      )
        return
      await apply(api.del(`${base}/stock-removal`))
      return
    }
    if (action === 'override') {
      if (payload === 'cancel' && !window.confirm('Cancel this line?')) return
      setNotice(null)
      await apply(
        api
          .post<Order & { books?: { booked?: boolean; quantity?: number }[] }>(
            `${base}/override`,
            { action: payload },
          )
          .then((updated) => {
            // Marking a line printed writes to somebody's books. Saying so is
            // the difference between a button that worked and a button that
            // did something nobody asked about.
            const booked = (updated.books ?? []).filter((row) => row.booked)
            const units = booked.reduce((sum, row) => sum + (row.quantity ?? 0), 0)
            if (units) {
              setNotice(
                `${units} unit${units === 1 ? '' : 's'} taken out of QuickBooks stock.`,
              )
            }
            return updated
          }),
      )
    }
  }

  /** Move the order to a column. Nothing else ever moves one. */
  const setStatus = async (next: OrderStatus, note?: string) => {
    setNotice(null)
    if (
      next === 'cancelled' &&
      !window.confirm(
        'Cancel this order? Its lines are cancelled too, which releases their ' +
          'stock and keeps any plates that have not gone to Bambuddy off the ' +
          'printers. You can undo it by clearing the status.',
      )
    )
      return
    await apply(
      api.put<Order>(`/api/orders/${orderId}/status`, {
        status: next,
        note: note ?? null,
      }),
    )
  }

  const matchShipStation = async () => {
    setMatching(true)
    try {
      await api.post(`/api/orders/${orderId}/match-shipstation`)
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setMatching(false)
    }
  }

  return (
    <>
      <div
        className="fixed inset-0 z-40 flex justify-end bg-ink-900/40"
        // Only a click on the backdrop itself closes the drawer.
        // Comparing target to currentTarget rather than stopping
        // propagation on the panel: stopPropagation silences every
        // descendant, which is what made clicking inside a dialog close
        // the whole drawer.
        onClick={(event) => {
          if (event.target === event.currentTarget) onClose()
        }}
      >
        <div className="flex h-full w-full max-w-2xl flex-col bg-ink-50 shadow-xl">
          <header className="flex flex-wrap items-center gap-3 border-b border-ink-200 bg-white px-4 py-3">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 font-mono text-base font-semibold text-ink-900">
                #{order?.order_number ?? '…'}
                {/* Always here, unlike on a card: this is the one order, and
                    "which shop do I go and look at" is the first thing asked
                    of it when a buyer writes in. */}
                {order ? (
                  <Badge className={SOURCE_CLASSES[order.source]}>
                    {SOURCE_LABELS[order.source]}
                  </Badge>
                ) : null}
              </h2>
              <p className="truncate text-sm text-ink-500">
                {order?.buyer_name ?? ''}
              </p>
            </div>
            {order ? <StatusPicker order={order} onSet={setStatus} /> : null}
            <Button variant="ghost" className="ml-auto" onClick={onClose}>
              Close
            </Button>
          </header>

          {order ? (
            <nav className="flex gap-1 border-b border-ink-200 bg-white px-3">
              {TABS.map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTab(key)}
                  className={cx(
                    'border-b-2 px-3 py-2 text-sm',
                    tab === key
                      ? 'border-ink-900 font-medium text-ink-900'
                      : 'border-transparent text-ink-500 hover:text-ink-800',
                  )}
                >
                  {label}
                  {key === 'lines' ? (
                    <span className="ml-1.5 text-xs text-ink-400">
                      {order.summary.line_count}
                    </span>
                  ) : null}
                </button>
              ))}
            </nav>
          ) : null}

          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
            {error ? <Alert tone="error">{error}</Alert> : null}
            {notice ? <Alert tone="info">{notice}</Alert> : null}
            {!order ? (
              <div className="flex justify-center py-10">
                <Spinner className="h-6 w-6" />
              </div>
            ) : (
              <>
                <section className={tab === 'lines' ? undefined : 'hidden'}>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Lines
                  </h3>
                  <ul className="space-y-1.5">
                    {order.lines.map((line) => (
                      <LineRow
                        key={line.id}
                        line={line}
                        depth={0}
                        bambuddyBase={order.bambuddy_base_url}
                        onAction={handleAction}
                      />
                    ))}
                  </ul>
                </section>

                <section
                  className={cx(
                    'rounded-lg bg-white p-3 ring-1 ring-ink-200',
                    tab !== 'shipping' && 'hidden',
                  )}
                >
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Shipping
                  </h3>
                  {order.tracking_number ? (
                    <div className="mt-2 space-y-1 text-sm">
                      <p className="text-ink-800">
                        Tracking{' '}
                        {order.tracking_url ? (
                          <a
                            className="font-mono underline decoration-ink-300 underline-offset-2 hover:decoration-ink-800"
                            href={order.tracking_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {order.tracking_number}
                          </a>
                        ) : (
                          <span className="font-mono">{order.tracking_number}</span>
                        )}
                      </p>
                      <p className="text-xs text-ink-500">
                        Label created {formatDateTime(order.label_created_at)} ·{' '}
                        {order.carrier_code} / {order.service_code}
                        {order.label_cost
                          ? ` · ${formatMoney(order.label_cost, order.label_currency)}`
                          : ''}
                      </p>

                      {/* Where the parcel is, as of the last time anybody
                          asked. The carrier's own sentence is kept because it
                          is the part that answers "delivered where?" — a left
                          parcel and a handed-over one are the same word here
                          and different things on a doorstep. */}
                      {order.tracking_status ? (
                        <div className="flex flex-wrap items-center gap-2 pt-1">
                          <Badge className={TRACKING_CLASSES[order.tracking_status]}>
                            {TRACKING_LABELS[order.tracking_status]}
                          </Badge>
                          <span className="text-xs text-ink-500">
                            checked {formatDateTime(order.tracking_checked_at)}
                          </span>
                        </div>
                      ) : null}
                      {order.tracking_detail ? (
                        <p className="text-xs text-ink-600">{order.tracking_detail}</p>
                      ) : null}
                      {order.delivered_at ? (
                        <p className="text-xs text-emerald-800">
                          Delivered {formatDateTime(order.delivered_at)} — moved to
                          Complete.
                        </p>
                      ) : null}

                      <p className="text-xs text-ink-500">
                        ShipStation pushes this tracking number back to Etsy.
                      </p>
                      {order.has_label_pdf ? (
                        <a
                          className="inline-block pt-1 text-sm text-ink-700 underline"
                          href={`/api/orders/${order.id}/label.pdf`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open label PDF
                        </a>
                      ) : null}
                    </div>
                  ) : (
                    <div className="mt-2 space-y-2">
                      <p className="text-sm text-ink-600">
                        {order.shipstation_order_id
                          ? `Matched in ShipStation (order ${order.shipstation_order_id}).`
                          : 'Not matched in ShipStation yet — its Etsy import can lag by up to an hour.'}
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {!order.shipstation_order_id ? (
                          <Button size="sm" onClick={matchShipStation} disabled={matching}>
                            {matching ? 'Checking…' : 'Check ShipStation now'}
                          </Button>
                        ) : null}
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={!order.shipstation_order_id}
                          onClick={() => setLabelOpen(true)}
                        >
                          Create label
                        </Button>
                      </div>
                      {order.status !== 'ready_to_ship' ? (
                        <p className="text-xs text-ink-500">
                          This order is not Ready to Ship yet. Labels cost money, so they are
                          never bought automatically.
                        </p>
                      ) : null}
                    </div>
                  )}
                </section>

                {tab === 'shipping' ? <ShipTo order={order} /> : null}
                {tab === 'money' ? <Money order={order} onRefreshed={load} /> : null}

                <section className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    onClick={() => apply(api.post(`/api/orders/${orderId}/reprocess`))}
                  >
                    Re-run intake
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={async () => {
                      if (
                        !window.confirm(
                          'Unmatch every line and run intake again from scratch? ' +
                            'Use this after linking a listing or adding variations.',
                        )
                      )
                        return
                      setNotice(null)
                      await apply(
                        api
                          .post<Order & { reset?: { kept_jobs: number } }>(
                            `/api/orders/${orderId}/reset-matching`,
                          )
                          .then((updated) => {
                            const kept = updated.reset?.kept_jobs ?? 0
                            setNotice(
                              'Re-matched from scratch.' +
                                (kept
                                  ? ` ${kept} print job${kept === 1 ? '' : 's'} already on ` +
                                    'the Bambuddy queue were left alone.'
                                  : ''),
                            )
                            return updated
                          }),
                      )
                    }}
                  >
                    Reset matching
                  </Button>
                  <a
                    className="inline-flex items-center rounded-md px-2.5 py-1 text-xs text-ink-600 underline"
                    href={`/api/orders/${order.id}/raw`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    View raw {SOURCE_LABELS[order.source]} payload
                  </a>
                </section>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Siblings of the backdrop, not children of it. A dialog nested
          inside a surface that closes on click is one stray bubbling
          event away from closing the thing it sits on. */}
      <ProductPicker
        open={pickerLine !== null}
        line={pickerLine}
        onClose={() => setPickerLine(null)}
        onPick={async (product, remember) => {
          const line = pickerLine
          setPickerLine(null)
          setNotice(null)
          if (!line) return
          await apply(
            api
              .post<Order & { also_fixed?: number }>(
                `/api/orders/${orderId}/lines/${line.id}/link-product`,
                { product_id: product.id, ...remember },
              )
              .then((updated) => {
                // This line was already matched before the rule was applied, so
                // the count is other lines only — and they are on a different
                // screen, so say so, otherwise the work is invisible.
                const others = updated.also_fixed ?? 0
                if (remember.remember && others > 0) {
                  setNotice(
                    `Linked. ${others} other line${others === 1 ? '' : 's'} waiting on ` +
                      'this listing matched too.',
                  )
                }
                return updated
              }),
          )
        }}
      />

      {order && labelOpen ? (
        <LabelDialog
          order={order}
          onClose={() => setLabelOpen(false)}
          // The dialog stays up after a purchase to say what it cost, so it
          // decides when it closes. This only refreshes what is behind it.
          onCreated={async () => {
            await load()
            onChanged()
          }}
        />
      ) : null}
    </>
  )
}

/** Where the parcel is going, as Etsy sent it.
 *
 *  `formatted_address` is Etsy's own layout for the destination country, which
 *  is the version to copy onto a label — address order is not the same
 *  everywhere, and reassembling the parts here would get some countries wrong.
 *  The parts are shown only when there is no formatted version. */
function ShipTo({ order }: { order: Order }) {
  const to = order.ship_to
  const channel = SOURCE_LABELS[order.source]
  // An empty block that explains itself, rather than one that is simply not
  // there: "PrintFlow has not read it" and "the shop did not send one" are
  // different, and only one of them is worth anybody's time.
  if (!to) {
    return (
      <section className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Ship to
        </h3>
        <p className="mt-2 text-sm text-ink-500">
          What {channel} sent for this order carries no address. Older orders
          predate PrintFlow reading it — the next poll fills them in; if this
          one stays empty, <em>View raw {channel} payload</em> below shows what
          arrived.
        </p>
      </section>
    )
  }
  const lines = (
    to.formatted
      ? to.formatted.split('\n')
      : [
          to.first_line,
          to.second_line,
          [to.city, to.state, to.zip].filter(Boolean).join(' '),
          to.country,
        ]
  )
    .filter((line): line is string => Boolean(line && line.trim()))
    // Etsy's formatted address opens with the recipient, and the name is
    // already the heading of this block — printed twice it reads as a mistake.
    .filter((line) => line.trim() !== (to.name ?? '').trim())

  return (
    <section className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
      <div className="flex items-baseline gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Ship to
        </h3>
        <button
          type="button"
          className="ml-auto text-xs text-ink-500 underline"
          onClick={() =>
            navigator.clipboard?.writeText(
              [to.name, ...lines].filter(Boolean).join('\n'),
            )
          }
        >
          Copy
        </button>
      </div>
      <p className="mt-2 text-sm font-medium text-ink-800">{to.name ?? '—'}</p>
      {lines.map((line) => (
        <p key={line} className="text-sm text-ink-600">
          {line}
        </p>
      ))}
      {to.email ? <p className="mt-1 text-xs text-ink-500">{to.email}</p> : null}
    </section>
  )
}

/** What the order was worth, and what selling it cost.
 *
 *  Three systems each hold a piece: Etsy knows what the buyer paid and what it
 *  charged for the sale, ShipStation knows what the label cost, and only
 *  PrintFlow sees all of them at once. The fees arrive later than the rest —
 *  Etsy charges after the sale, not during — so an order with none yet says so
 *  rather than showing a net that is really just the revenue. */
function Money({ order, onRefreshed }: { order: Order; onRefreshed: () => void }) {
  const currency = order.currency ?? order.label_currency
  const show = (amount: string | null) => (amount ? formatMoney(amount, currency) : null)

  const takings: [string, string | null][] = [
    ['Items', show(order.items_total)],
    ['Shipping', show(order.shipping_total)],
    ['Tax', show(order.tax_total)],
    ['Discount', order.discount_total ? `−${show(order.discount_total)}` : null],
  ]
  // Etsy is the only channel PrintFlow can sweep a fee ledger from, so on a
  // Wix order these three are whatever somebody typed. The first row is named
  // after the channel rather than after the column it is stored in — "Etsy
  // fees" on a Wix order is a figure nobody can account for.
  const sweepable = order.source === 'etsy'
  const costs: [string, string | null][] = [
    [`${SOURCE_LABELS[order.source]} fees`, show(order.etsy_fees)],
    ['Marketing fees', show(order.marketing_fees)],
    ['Processing fees', show(order.processing_fees)],
    ['Shipping label', show(order.label_cost)],
  ]
  const anyFees = order.etsy_fees || order.marketing_fees || order.processing_fees

  return (
    <section className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
      <div className="flex items-baseline gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Money
        </h3>
        {sweepable ? (
          <button
            type="button"
            className="ml-auto text-xs text-ink-500 underline"
            onClick={() => api.post('/api/orders/finances/refresh').then(onRefreshed)}
          >
            Check Etsy for fees
          </button>
        ) : null}
      </div>

      {!order.revenue ? (
        <p className="mt-2 text-sm text-ink-500">
          What {SOURCE_LABELS[order.source]} sent for this order carries no
          totals. The next poll reads them out of the payload already stored,
          so this fills itself in; <em> View raw {SOURCE_LABELS[order.source]}
          payload</em> below shows what actually arrived.
        </p>
      ) : null}

      <dl className="mt-2 space-y-1 text-sm">
        {order.revenue ? (
          <div className="flex justify-between font-medium text-ink-800">
            <dt>Revenue</dt>
            <dd>{show(order.revenue)}</dd>
          </div>
        ) : null}
        {takings.map(([label, value]) =>
          value ? (
            <div key={label} className="flex justify-between pl-3 text-xs text-ink-500">
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ) : null,
        )}

        {costs.map(([label, value]) =>
          value ? (
            <div key={label} className="flex justify-between text-ink-600">
              <dt>{label}</dt>
              <dd>−{value}</dd>
            </div>
          ) : null,
        )}

        {order.net ? (
          <div className="flex justify-between border-t border-ink-200 pt-1 font-semibold text-ink-900">
            <dt>Net</dt>
            <dd>{show(order.net)}</dd>
          </div>
        ) : null}
      </dl>

      {!anyFees ? (
        <p className="mt-2 text-xs text-ink-500">
          {!sweepable
            ? // Only Etsy has a fee ledger PrintFlow reads. Saying "not read
              // yet" on a Wix order would be a promise nothing is going to keep.
              `PrintFlow reads no fee ledger from ${SOURCE_LABELS[order.source]}. ` +
              'Type what the sale cost below and it counts towards net the same way.'
            : order.finance_synced_at
              ? 'Etsy has charged no fees against this order yet.'
              : 'Fees have not been read yet — Etsy posts them to the shop ledger ' +
                'after the sale, so they arrive on a later poll.'}
        </p>
      ) : null}

      {order.fee_lines?.length ? <FeeBreakdown order={order} /> : null}

      <FeesByHand order={order} onChanged={onRefreshed} />
      <ExpensePanel order={order} onChanged={onRefreshed} />
      <InvoicePanel order={order} onChanged={onRefreshed} />
    </section>
  )
}

/** Etsy's fees, typed in.
 *
 *  Etsy's ledger settles days after a sale, and until it does an order shows no
 *  cost at all — so a shop closing its month either waits for Etsy or works the
 *  figures out on paper. This is the third option.
 *
 *  What is typed is marked as typed, and *Check Etsy for fees* then leaves this
 *  order alone: a sweep quietly replacing a number somebody put here — and may
 *  already have expensed to QuickBooks — is how the books and the screen stop
 *  agreeing. Emptying every box hands the order back to the sweep.
 */
function FeesByHand({ order, onChanged }: { order: Order; onChanged: () => void }) {
  const manual = order.fees_source === 'manual'
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState({
    etsy_fees: order.etsy_fees ?? '',
    marketing_fees: order.marketing_fees ?? '',
    processing_fees: order.processing_fees ?? '',
  })
  // Re-read when the order does: another tab, or the sweep, may have moved them.
  useEffect(() => {
    setDraft({
      etsy_fees: order.etsy_fees ?? '',
      marketing_fees: order.marketing_fees ?? '',
      processing_fees: order.processing_fees ?? '',
    })
  }, [order.etsy_fees, order.marketing_fees, order.processing_fees])

  const save = async (values: typeof draft) => {
    setBusy(true)
    setError(null)
    try {
      await api.put(`/api/orders/${order.id}/fees`, values)
      onChanged()
      setOpen(false)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const fields: [keyof typeof draft, string][] = [
    // The column is `etsy_fees` for historical reasons; the label says which
    // shop actually charged them, because that is what somebody typing a
    // figure off a statement is looking at.
    ['etsy_fees', `${SOURCE_LABELS[order.source]} fees`],
    ['marketing_fees', 'Marketing'],
    ['processing_fees', 'Processing'],
  ]

  return (
    <div className="mt-3 border-t border-ink-200 pt-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          {SOURCE_LABELS[order.source]} fees
        </h4>
        {manual ? (
          <Badge className="bg-sky-100 text-sky-800">entered by hand</Badge>
        ) : null}
        <button
          type="button"
          className="ml-auto text-xs text-ink-500 underline"
          onClick={() => setOpen((was) => !was)}
        >
          {open ? 'Close' : manual ? 'Edit' : 'Enter them'}
        </button>
      </div>

      {manual && !open ? (
        <p className="mt-1 text-xs text-ink-500">
          {order.source === 'etsy' ? (
            <>
              Typed rather than read from Etsy, so <em>Check Etsy for fees</em>{' '}
              will not overwrite them. Clear every box to hand this order back
              to it.
            </>
          ) : (
            <>
              Typed by hand, which is the only way fees reach a{' '}
              {SOURCE_LABELS[order.source]} order — PrintFlow reads no fee
              ledger there.
            </>
          )}
        </p>
      ) : null}

      {open ? (
        <div className="mt-2 space-y-2">
          <div className="grid grid-cols-3 gap-2">
            {fields.map(([field, label]) => (
              <Field key={field} label={label}>
                <input
                  className={inputClass}
                  inputMode="decimal"
                  placeholder="0.00"
                  value={draft[field]}
                  disabled={busy}
                  onChange={(e) =>
                    setDraft((was) => ({ ...was, [field]: e.target.value }))
                  }
                />
              </Field>
            ))}
          </div>
          <p className="text-xs text-ink-500">
            What Etsy took, as positive amounts. Its ledger states them as money
            leaving; a minus sign copied from there is read as the same figure.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" disabled={busy} onClick={() => save(draft)}>
              {busy ? 'Saving…' : 'Save fees'}
            </Button>
            {manual ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() =>
                  save({ etsy_fees: '', marketing_fees: '', processing_fees: '' })
                }
              >
                Clear, and let Etsy fill them
              </Button>
            ) : null}
          </div>
          {error ? <Alert tone="error">{error}</Alert> : null}
        </div>
      ) : null}
    </div>
  )
}

/** What the order cost, expensed into QuickBooks.
 *
 *  Two bills that arrive at different times from different people: the
 *  carrier's, once a label is priced, and Etsy's, once its ledger settles or
 *  somebody types it. So two documents rather than one, each raised and removed
 *  on its own — an order can easily be ready to expense one and not the other.
 *
 *  Unlike a stock removal these are real money going out, so they are plain
 *  Purchases against the expense accounts chosen in Settings.
 */
function ExpensePanel({ order, onChanged }: { order: Order; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async (kind: string, path: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(kind)
    setError(null)
    try {
      await api.post(`/api/orders/${order.id}/expenses/${kind}${path}`)
      onChanged()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const bills = [
    {
      kind: 'shipping',
      label: 'Shipping label',
      amount: order.label_cost,
      id: order.qbo_shipping_expense_id,
      at: order.qbo_shipping_expense_at,
      total: order.qbo_shipping_expense_total,
      failed: order.qbo_shipping_expense_error,
      missing: 'No label has been priced for this order yet.',
    },
    {
      kind: 'fees',
      label: `${SOURCE_LABELS[order.source]}'s fees`,
      amount:
        order.etsy_fees || order.marketing_fees || order.processing_fees
          ? String(
              Number(order.etsy_fees ?? 0) +
                Number(order.marketing_fees ?? 0) +
                Number(order.processing_fees ?? 0),
            )
          : null,
      id: order.qbo_fee_expense_id,
      at: order.qbo_fee_expense_at,
      total: order.qbo_fee_expense_total,
      failed: order.qbo_fee_expense_error,
      missing:
        order.source === 'etsy'
          ? 'No fees on this order yet — read them from Etsy or type them above.'
          : 'No fees on this order yet — type them above.',
    },
  ]

  return (
    <div className="mt-3 border-t border-ink-200 pt-3">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
        QuickBooks expenses
      </h4>
      <p className="mt-1 text-xs text-ink-500">
        What this order cost, as expenses in QuickBooks. Two bills, raised
        separately: the carrier's postage and the shop's cut.
      </p>
      <div className="mt-2 space-y-2">
        {bills.map((bill) => (
          <div key={bill.kind} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-ink-700">{bill.label}</span>
            {bill.amount ? (
              <span className="text-ink-500">
                {formatMoney(bill.amount, order.currency ?? order.label_currency)}
              </span>
            ) : (
              <span className="text-xs text-ink-400">{bill.missing}</span>
            )}
            <span className="ml-auto flex items-center gap-2">
              {bill.id ? (
                <>
                  <Badge className="bg-emerald-100 text-emerald-800">
                    expensed {formatDateTime(bill.at)}
                  </Badge>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy !== null}
                    onClick={() =>
                      run(
                        bill.kind,
                        '/void',
                        `Remove the ${bill.label.toLowerCase()} expense from ` +
                          'QuickBooks? The Purchase is deleted, and this order ' +
                          'can then be expensed again.',
                      )
                    }
                  >
                    {busy === bill.kind ? 'Working…' : 'Remove'}
                  </Button>
                </>
              ) : (
                <Button
                  size="sm"
                  disabled={busy !== null || !bill.amount}
                  onClick={() => run(bill.kind, '')}
                >
                  {busy === bill.kind ? 'Posting…' : 'Expense it'}
                </Button>
              )}
            </span>
            {bill.failed && !bill.id ? (
              <p className="w-full text-xs text-red-700">
                Last attempt failed: {bill.failed}
              </p>
            ) : null}
          </div>
        ))}
      </div>
      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
    </div>
  )
}

/** The QuickBooks invoice for this order — raise one, or say there is one.
 *
 *  Deliberately a button rather than something that happens on its own: an
 *  invoice is a document in somebody's books, and which orders get one is a
 *  decision about the business rather than about the software.
 */
function InvoicePanel({ order, onChanged }: { order: Order; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // What number this invoice will get, asked before anybody presses the button.
  // QuickBooks numbers its own documents unless the company has been set to
  // number them itself, and the two look identical until an invoice lands
  // without a reference — so it is worth saying which it will be.
  const [numbering, setNumbering] = useState<InvoiceNumbering | null>(null)
  useEffect(() => {
    if (order.qbo_invoice_id) return
    let dropped = false
    api
      .get<InvoiceNumbering>('/api/books/invoice-number')
      .then((data) => !dropped && setNumbering(data))
      .catch(() => undefined)
    return () => {
      dropped = true
    }
  }, [order.qbo_invoice_id])

  const discount = Number(order.discount_total ?? 0) > 0 ? order.discount_total : null

  const run = async (path: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return
    setBusy(true)
    setError(null)
    try {
      await api.post(`/api/orders/${order.id}/invoice${path}`)
      onChanged()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-3 border-t border-ink-200 pt-3">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
        QuickBooks invoice
      </h4>
      {order.qbo_invoice_id ? (
        <div className="mt-1.5 space-y-1">
          <p className="text-sm text-ink-800">
            Invoice{' '}
            <span className="font-mono">{order.qbo_invoice_doc_number ?? order.qbo_invoice_id}</span>
            {order.qbo_invoice_total
              ? ` · ${formatMoney(order.qbo_invoice_total, order.currency)}`
              : null}
          </p>
          <p className="text-xs text-ink-500">
            Raised {formatDateTime(order.qbo_invoice_at)}, billed line by line
            against each item sold. Stock the invoice relieves was handed back by
            the printed lines, so each unit is deducted once.
          </p>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() =>
              run(
                '/void',
                'Void this invoice in QuickBooks? It stays visible there with a ' +
                  'zero total, and this order can then be invoiced again.',
              )
            }
          >
            {busy ? 'Working…' : 'Void invoice'}
          </Button>
        </div>
      ) : (
        <div className="mt-1.5 space-y-1.5">
          <p className="text-xs text-ink-500">
            Bills the buyer's name and address from this order, at the prices{' '}
            {SOURCE_LABELS[order.source]} recorded, each line against its own
            QuickBooks item. Where that item carries stock the invoice relieves
            it, and the printed line's removal is taken back so nothing is
            deducted twice.
          </p>
          {discount ? (
            <p className="text-xs text-ink-600">
              {SOURCE_LABELS[order.source]} took{' '}
              {formatMoney(discount, order.currency)} off this order.
              That goes on as a discount line, so the invoice totals what the
              buyer actually paid.
            </p>
          ) : null}
          {numbering ? (
            <p className="text-xs text-ink-500">
              {numbering.next ? (
                <>
                  This will be invoice{' '}
                  <span className="font-mono text-ink-800">{numbering.next}</span>.
                </>
              ) : (
                numbering.why
              )}
            </p>
          ) : null}
          <Button size="sm" variant="primary" disabled={busy} onClick={() => run('')}>
            {busy ? 'Creating…' : 'Create invoice'}
          </Button>
        </div>
      )}
      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {order.qbo_invoice_error && !order.qbo_invoice_id ? (
        <p className="mt-1.5 text-xs text-red-700">
          Last attempt failed: {order.qbo_invoice_error}
        </p>
      ) : null}
    </div>
  )
}

/** Etsy's fees, in Etsy's own words, grouped the way the totals above group them.
 *
 *  The totals answer "how much"; this answers "for what", which is the question
 *  that actually gets asked — a marketing fee nobody expected is a decision to
 *  revisit, and it cannot be revisited from a single number. Each line is
 *  copied from the shop's payment ledger verbatim, so a figure that looks wrong
 *  can be taken back to Etsy as their own sentence rather than as our arithmetic. */
function FeeBreakdown({ order }: { order: Order }) {
  const currency = order.currency ?? order.label_currency
  const groups: { kind: string; label: string; total: string | null }[] = [
    { kind: 'etsy', label: 'Etsy fees', total: order.etsy_fees },
    { kind: 'marketing', label: 'Marketing fees', total: order.marketing_fees },
    { kind: 'processing', label: 'Processing fees', total: order.processing_fees },
  ]

  return (
    <div className="mt-3 border-t border-ink-200 pt-3">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
        Fee breakdown from Etsy
      </h4>
      <div className="mt-2 space-y-3">
        {groups.map((group) => {
          const lines = (order.fee_lines ?? []).filter((row) => row.kind === group.kind)
          if (!lines.length) return null
          return (
            <div key={group.kind}>
              <div className="flex justify-between text-sm font-medium text-ink-800">
                <span>{group.label}</span>
                <span>{formatMoney(group.total, currency)}</span>
              </div>
              <ul className="mt-0.5 space-y-0.5">
                {lines.map((line, index) => (
                  <li
                    key={`${line.ledger_entry_id ?? index}`}
                    className="flex justify-between gap-3 pl-3 text-xs text-ink-500"
                  >
                    <span className="min-w-0 flex-1 truncate" title={line.description ?? ''}>
                      {line.description ?? '(no description)'}
                    </span>
                    <span className="shrink-0">{formatMoney(line.amount, currency)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>
      <p className="mt-2 text-xs text-ink-400">
        Straight from the shop's payment ledger, in Etsy's own wording.
        {order.finance_synced_at
          ? ` Last read ${formatDateTime(order.finance_synced_at)}.`
          : ''}
      </p>
    </div>
  )
}
