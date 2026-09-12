import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { Product } from '../lib/types'
import { Alert, Badge, Button, Field, Modal, Spinner, cx, inputClass } from './ui'

/** Add an order that did not come from a shop window.
 *
 * A phone call, a market stall, a trade sale, a replacement sent out free.
 * Everything past this point is the same as a polled order — the lines match,
 * bundles explode, stock is decided, plates are planned — so this form's whole
 * job is to collect what a receipt would otherwise have carried.
 *
 * Which makes the price the interesting field. A polled order keeps its
 * payload and the invoice reads each line's price back out of it; there is no
 * payload here, so the price has to be typed or the line cannot be invoiced.
 * It is optional rather than required because a replacement sent out free is a
 * real order with nothing to bill, and a zero would say something different.
 */

interface Line {
  /** Local only, so React can key the rows while they are being edited. */
  key: number
  product: Product | null
  quantity: string
  unitPrice: string
}

let nextKey = 1

function blankLine(): Line {
  return { key: nextKey++, product: null, quantity: '1', unitPrice: '' }
}

/** Today, in the YYYY-MM-DD an <input type="date"> wants, in local time. */
function today(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function ProductPicker({
  onPick,
  onClose,
}: {
  onPick: (product: Product) => void
  onClose: () => void
}) {
  const [query, setQuery] = useState('')
  const [products, setProducts] = useState<Product[] | null>(null)

  useEffect(() => {
    let dropped = false
    api
      .get<{ products: Product[] }>(
        `/api/products?q=${encodeURIComponent(query)}&limit=50`,
      )
      .then((data) => !dropped && setProducts(data.products))
      .catch(() => !dropped && setProducts([]))
    return () => {
      dropped = true
    }
  }, [query])

  return (
    <Modal open title="Pick a product" onClose={onClose}>
      <input
        className={inputClass}
        placeholder="Search by code or name…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        autoFocus
      />
      <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
        {products === null ? <Spinner /> : null}
        {products !== null && products.length === 0 ? (
          <p className="py-4 text-center text-sm text-ink-500">
            Nothing matches. Products are created on the Products screen.
          </p>
        ) : null}
        {(products ?? []).map((product) => (
          <button
            key={product.id}
            type="button"
            onClick={() => onPick(product)}
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

export default function NewOrderDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const [reference, setReference] = useState('')
  const [buyer, setBuyer] = useState('')
  const [placed, setPlaced] = useState(today())
  const [lines, setLines] = useState<Line[]>([blankLine()])
  const [shipping, setShipping] = useState('')
  const [tax, setTax] = useState('')
  const [discount, setDiscount] = useState('')
  const [address, setAddress] = useState({
    name: '',
    first_line: '',
    second_line: '',
    city: '',
    state: '',
    zip: '',
    country: '',
  })
  const [picking, setPicking] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const setLine = (key: number, patch: Partial<Line>) =>
    setLines((was) => was.map((l) => (l.key === key ? { ...l, ...patch } : l)))

  // Worked out here as well as on the server, so the figure is visible while
  // it is being typed rather than only after the order exists.
  const money = (raw: string) => {
    const n = Number(raw)
    return Number.isFinite(n) ? n : 0
  }
  const items = lines.reduce(
    (sum, l) => sum + money(l.unitPrice) * (Number(l.quantity) || 0),
    0,
  )
  const total = items + money(shipping) + money(tax) - money(discount)

  const ready = lines.some((l) => l.product) && !busy

  const create = async () => {
    setBusy(true)
    setError(null)
    try {
      const filled = Object.fromEntries(
        Object.entries(address).filter(([, v]) => v.trim()),
      )
      await api.post('/api/orders', {
        order_number: reference.trim() || null,
        buyer_name: buyer.trim() || null,
        // Midday rather than midnight, so a date typed here cannot land on the
        // previous day once it is turned into UTC.
        placed_at: placed ? new Date(`${placed}T12:00:00`).toISOString() : null,
        ship_to: Object.keys(filled).length ? filled : null,
        shipping_total: shipping.trim() || null,
        tax_total: tax.trim() || null,
        discount_total: discount.trim() || null,
        lines: lines
          .filter((l) => l.product)
          .map((l) => ({
            product_id: l.product!.id,
            quantity: Number(l.quantity) || 1,
            unit_price: l.unitPrice.trim() || null,
          })),
      })
      onCreated()
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open title="New order" onClose={onClose}>
      <p className="mb-3 text-sm text-ink-600">
        For an order that did not come from Etsy or Wix. It joins the board and
        goes through the same matching, stock and plate planning as any other.
      </p>

      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Reference" hint="Blank generates one.">
          <input
            className={inputClass}
            placeholder="M-1001"
            value={reference}
            onChange={(e) => setReference(e.target.value)}
          />
        </Field>
        <Field label="Buyer">
          <input
            className={inputClass}
            value={buyer}
            onChange={(e) => setBuyer(e.target.value)}
          />
        </Field>
        <Field label="Placed">
          <input
            className={inputClass}
            type="date"
            value={placed}
            onChange={(e) => setPlaced(e.target.value)}
          />
        </Field>
      </div>

      <h3 className="mt-4 text-sm font-semibold text-ink-800">Lines</h3>
      <div className="mt-1 space-y-2">
        {lines.map((line, index) => (
          <div
            key={line.key}
            className="flex flex-wrap items-end gap-2 rounded-md border border-ink-200 p-2"
          >
            <div className="min-w-48 flex-1">
              <Field label={`Product ${index + 1}`}>
                <button
                  type="button"
                  onClick={() => setPicking(line.key)}
                  className={cx(
                    inputClass,
                    'text-left',
                    line.product ? 'text-ink-900' : 'text-ink-400',
                  )}
                >
                  {line.product
                    ? `${line.product.sku} — ${line.product.name}`
                    : 'Choose a product…'}
                </button>
              </Field>
            </div>
            <div className="w-20">
              <Field label="Qty">
                <input
                  className={inputClass}
                  inputMode="numeric"
                  value={line.quantity}
                  onChange={(e) =>
                    setLine(line.key, { quantity: e.target.value.replace(/\D/g, '') })
                  }
                />
              </Field>
            </div>
            <div className="w-28">
              <Field label="Each">
                <input
                  className={inputClass}
                  inputMode="decimal"
                  placeholder="0.00"
                  value={line.unitPrice}
                  onChange={(e) => setLine(line.key, { unitPrice: e.target.value })}
                />
              </Field>
            </div>
            {/* A cross rather than the word: three fields and a "Remove" do
                not fit on one row at this width, and a button that wraps onto
                its own line reads as if it applied to all of them. */}
            <Button
              size="sm"
              variant="ghost"
              title="Remove this line"
              aria-label={`Remove line ${index + 1}`}
              disabled={lines.length === 1}
              onClick={() => setLines((was) => was.filter((l) => l.key !== line.key))}
            >
              ✕
            </Button>
          </div>
        ))}
        <Button size="sm" onClick={() => setLines((was) => [...was, blankLine()])}>
          Add a line
        </Button>
      </div>

      <h3 className="mt-4 text-sm font-semibold text-ink-800">Money</h3>
      <div className="mt-1 grid gap-3 sm:grid-cols-3">
        <Field label="Shipping">
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="0.00"
            value={shipping}
            onChange={(e) => setShipping(e.target.value)}
          />
        </Field>
        <Field label="Tax">
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="0.00"
            value={tax}
            onChange={(e) => setTax(e.target.value)}
          />
        </Field>
        <Field label="Discount">
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="0.00"
            value={discount}
            onChange={(e) => setDiscount(e.target.value)}
          />
        </Field>
      </div>
      <p className="mt-1 text-xs text-ink-500">
        Items {items.toFixed(2)} · total{' '}
        <span className="font-medium text-ink-800">{total.toFixed(2)}</span>. A
        line with no price is left off the invoice rather than billed at zero.
      </p>

      <details className="mt-4">
        <summary className="cursor-pointer text-sm font-semibold text-ink-800">
          Ship to
        </summary>
        <p className="mt-1 text-xs text-ink-500">
          Only needed to buy a label. Leave it blank for an order being
          collected or handed over.
        </p>
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          {(
            [
              ['name', 'Name'],
              ['first_line', 'Address'],
              ['second_line', 'Address line 2'],
              ['city', 'City'],
              ['state', 'State'],
              ['zip', 'Postcode'],
              ['country', 'Country'],
            ] as const
          ).map(([field, label]) => (
            <Field key={field} label={label}>
              <input
                className={inputClass}
                value={address[field]}
                onChange={(e) =>
                  setAddress((was) => ({ ...was, [field]: e.target.value }))
                }
              />
            </Field>
          ))}
        </div>
      </details>

      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button variant="primary" disabled={!ready} onClick={create}>
          {busy ? 'Creating…' : 'Create order'}
        </Button>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        {!lines.some((l) => l.product) ? (
          <span className="text-xs text-ink-500">Pick a product first.</span>
        ) : null}
      </div>

      {picking !== null ? (
        <ProductPicker
          onClose={() => setPicking(null)}
          onPick={(product) => {
            setLine(picking, { product })
            setPicking(null)
          }}
        />
      ) : null}
    </Modal>
  )
}
