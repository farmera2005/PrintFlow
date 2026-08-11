import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import {
  COLUMN_LABELS,
  ORDER_STATUSES,
  JOB_STATUS_CLASSES,
  LINE_STATE_CLASSES,
  LINE_STATE_LABELS,
  formatDateTime,
  formatMoney,
} from '../lib/format'
import type { Order, OrderLine, OrderStatus, Product } from '../lib/types'
import LabelDialog from './LabelDialog'
import { Alert, Badge, Button, Modal, Spinner, cx, inputClass } from './ui'

/** What to remember about the Etsy listing when a product is picked. */
export interface RememberChoice {
  remember: boolean
  remember_scope: 'listing' | 'variant'
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
  const listingId = line?.etsy_listing_id ?? null
  const variantId = line?.etsy_product_id ?? null

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
            This line came from Etsy listing{' '}
            <code className="font-mono">{listingId}</code>
            {variantId ? (
              <>
                , variation <code className="font-mono">{variantId}</code>
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
              Remember this listing, so future orders match on their own — and clear any
              orders already waiting on it.
            </span>
          </label>
          {remember && variantId ? (
            <select
              className={cx(inputClass, 'mt-2')}
              value={scope}
              onChange={(e) => setScope(e.target.value as 'listing' | 'variant')}
            >
              <option value="listing">The whole listing ({listingId})</option>
              <option value="variant">Only this variation ({variantId})</option>
            </select>
          ) : null}
          {remember && variantId && scope === 'variant' ? (
            <p className="mt-1.5 text-xs text-ink-500">
              Etsy issues a new id for a variation whenever the listing's options are
              edited, so a variation link stops matching after the next edit. Prefer the
              whole listing and let option rules handle the differences.
            </p>
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
    if (action === 'override') {
      if (payload === 'cancel' && !window.confirm('Cancel this line?')) return
      await apply(api.post(`${base}/override`, { action: payload }))
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
              <h2 className="font-mono text-base font-semibold text-ink-900">
                #{order?.order_number ?? '…'}
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

          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
            {error ? <Alert tone="error">{error}</Alert> : null}
            {notice ? <Alert tone="info">{notice}</Alert> : null}
            {!order ? (
              <div className="flex justify-center py-10">
                <Spinner className="h-6 w-6" />
              </div>
            ) : (
              <>
                <section>
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

                <section className="rounded-lg bg-white p-3 ring-1 ring-ink-200">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Shipping
                  </h3>
                  {order.tracking_number ? (
                    <div className="mt-2 space-y-1 text-sm">
                      <p className="text-ink-800">
                        Tracking <span className="font-mono">{order.tracking_number}</span>
                      </p>
                      <p className="text-xs text-ink-500">
                        Label created {formatDateTime(order.label_created_at)} ·{' '}
                        {order.carrier_code} / {order.service_code}
                        {order.label_cost
                          ? ` · ${formatMoney(order.label_cost, order.label_currency)}`
                          : ''}
                      </p>
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

                <ShipTo order={order} />
                <Money order={order} onRefreshed={load} />

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
                    View raw Etsy payload
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
  if (!to) return null
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
  if (!order.revenue && !order.etsy_fees && !order.label_cost) return null
  const currency = order.currency ?? order.label_currency
  const show = (amount: string | null) => (amount ? formatMoney(amount, currency) : null)

  const takings: [string, string | null][] = [
    ['Items', show(order.items_total)],
    ['Shipping', show(order.shipping_total)],
    ['Tax', show(order.tax_total)],
    ['Discount', order.discount_total ? `−${show(order.discount_total)}` : null],
  ]
  const costs: [string, string | null][] = [
    ['Etsy fees', show(order.etsy_fees)],
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
        <button
          type="button"
          className="ml-auto text-xs text-ink-500 underline"
          onClick={() => api.post('/api/orders/finances/refresh').then(onRefreshed)}
        >
          Check Etsy for fees
        </button>
      </div>

      <dl className="mt-2 space-y-1 text-sm">
        <div className="flex justify-between font-medium text-ink-800">
          <dt>Revenue</dt>
          <dd>{show(order.revenue) ?? '—'}</dd>
        </div>
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
          {order.finance_synced_at
            ? 'Etsy has charged no fees against this order yet.'
            : 'Fees have not been read yet — Etsy posts them to the shop ledger ' +
              'after the sale, so they arrive on a later poll.'}
        </p>
      ) : null}

      {order.fee_lines?.length ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-ink-500">
            {order.fee_lines.length} fee line
            {order.fee_lines.length === 1 ? '' : 's'} from Etsy's ledger
          </summary>
          <ul className="mt-1 space-y-0.5">
            {order.fee_lines.map((line, index) => (
              <li
                key={`${line.ledger_entry_id ?? index}`}
                className="flex justify-between gap-2 text-xs text-ink-500"
              >
                <span className="min-w-0 truncate">{line.description ?? line.kind}</span>
                <span>{formatMoney(line.amount, currency)}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
