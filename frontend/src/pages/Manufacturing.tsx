import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Modal,
  Spinner,
  cx,
  inputClass,
} from '../components/ui'

interface SheetLine {
  id: string
  product_id: string
  sku: string
  name: string
  qbo_item_id: string | null
  qbo_item_name: string | null
  has_bom: boolean
  quantity: number
  unit_cost: string
  cost_from_bom: boolean
  amount: string
}

interface Sheet {
  id: string
  reference: string
  made_on: string
  memo: string | null
  status: 'draft' | 'posted' | 'voided'
  qbo_purchase_id: string | null
  qbo_doc_number: string | null
  posted_at: string | null
  posted_by: string | null
  voided_at: string | null
  voided_by: string | null
  created_at: string
  lines: SheetLine[]
  total: string
}

interface PreviewRow {
  sku: string
  name: string
  quantity: number
  unit_cost: string | null
  amount: string | null
}

interface Preview {
  made: PreviewRow[]
  made_total: string
  consumed: PreviewRow[]
  consumed_total: string
  net_to_account: string
  account_name: string | null
  problems: string[]
}

interface ProductOption {
  id: string
  sku: string
  name: string
  qbo_item_id: string | null
}

const STATUS_CLASSES: Record<Sheet['status'], string> = {
  draft: 'bg-ink-100 text-ink-700 ring-ink-300',
  posted: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  voided: 'bg-amber-100 text-amber-800 ring-amber-300',
}

/** Money for display. The server is the authority; this only formats. */
function currency(value: string | null): string {
  if (value === null) return '—'
  const n = Number(value)
  return Number.isNaN(n) ? value : n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export default function Manufacturing() {
  const [sheets, setSheets] = useState<Sheet[] | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [products, setProducts] = useState<ProductOption[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ sheets: Sheet[] }>('/api/manufacturing/sheets')
      setSheets(data.sheets)
      setError(null)
      return data.sheets
    } catch (err) {
      setError(errorMessage(err))
      return []
    }
  }, [])

  useEffect(() => {
    load()
    api
      .get<{ products: ProductOption[] }>('/api/products')
      .then((data) => setProducts(data.products))
      .catch(() => setProducts([]))
  }, [load])

  const create = async () => {
    setBusy('new')
    try {
      const sheet = await api.post<Sheet>('/api/manufacturing/sheets', {})
      await load()
      setSelectedId(sheet.id)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const selected = (sheets ?? []).find((s) => s.id === selectedId) ?? null

  if (sheets === null) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  return (
    <div className="space-y-4 p-3 sm:p-6">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Manufacturing</h1>
          <p className="text-sm text-ink-600">
            Record what you made into stock. Posting a sheet writes one expense to
            QuickBooks and raises the quantity on hand.
          </p>
        </div>
        <div className="ml-auto">
          <Button variant="primary" onClick={create} disabled={busy === 'new'}>
            {busy === 'new' ? 'Creating…' : 'New made-items sheet'}
          </Button>
        </div>
      </header>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {sheets.length === 0 ? (
        <EmptyState
          title="No made-items sheets yet"
          description="Start one when you finish a production run. Nothing reaches QuickBooks until you press Post."
        />
      ) : (
        <Card>
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-ink-500">
              <tr>
                <th className="px-3 py-2 font-medium">Reference</th>
                <th className="px-3 py-2 font-medium">Made on</th>
                <th className="px-3 py-2 font-medium">Lines</th>
                <th className="px-3 py-2 text-right font-medium">Value</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">QuickBooks</th>
              </tr>
            </thead>
            <tbody>
              {sheets.map((sheet) => (
                <tr
                  key={sheet.id}
                  className="cursor-pointer border-t border-ink-100 hover:bg-ink-50"
                  onClick={() => setSelectedId(sheet.id)}
                >
                  <td className="px-3 py-2 font-mono text-xs">{sheet.reference}</td>
                  <td className="px-3 py-2">{formatDateTime(sheet.made_on)}</td>
                  <td className="px-3 py-2">{sheet.lines.length}</td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {currency(sheet.total)}
                  </td>
                  <td className="px-3 py-2">
                    <Badge className={STATUS_CLASSES[sheet.status]}>{sheet.status}</Badge>
                  </td>
                  <td className="px-3 py-2 text-xs text-ink-600">
                    {sheet.qbo_doc_number ? `#${sheet.qbo_doc_number}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {selected ? (
        <SheetEditor
          sheet={selected}
          products={products}
          onClose={() => setSelectedId(null)}
          onChanged={load}
        />
      ) : null}
    </div>
  )
}

function SheetEditor({
  sheet,
  products,
  onClose,
  onChanged,
}: {
  sheet: Sheet
  products: ProductOption[]
  onClose: () => void
  onChanged: () => Promise<Sheet[]>
}) {
  const [current, setCurrent] = useState<Sheet>(sheet)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [addProduct, setAddProduct] = useState('')
  const [addQty, setAddQty] = useState('1')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => setCurrent(sheet), [sheet])

  const draft = current.status === 'draft'

  const loadPreview = useCallback(async () => {
    try {
      setPreview(
        await api.get<Preview>(`/api/manufacturing/sheets/${current.id}/preview`),
      )
    } catch (err) {
      setError(errorMessage(err))
      setPreview(null)
    }
  }, [current.id])

  useEffect(() => {
    loadPreview()
  }, [loadPreview, current.lines.length])

  const apply = (updated: Sheet) => {
    setCurrent(updated)
    onChanged()
  }

  const addLine = async () => {
    if (!addProduct) return
    setBusy('add')
    setError(null)
    try {
      apply(
        await api.post<Sheet>(`/api/manufacturing/sheets/${current.id}/lines`, {
          product_id: addProduct,
          quantity: Number(addQty) || 1,
        }),
      )
      setAddProduct('')
      setAddQty('1')
      await loadPreview()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const updateLine = async (lineId: string, body: Record<string, unknown>) => {
    setBusy(lineId)
    setError(null)
    try {
      apply(
        await api.patch<Sheet>(
          `/api/manufacturing/sheets/${current.id}/lines/${lineId}`,
          body,
        ),
      )
      await loadPreview()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const removeLine = async (lineId: string) => {
    setBusy(lineId)
    try {
      apply(
        await api.del<Sheet>(`/api/manufacturing/sheets/${current.id}/lines/${lineId}`),
      )
      await loadPreview()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const post = async () => {
    setBusy('post')
    setError(null)
    try {
      apply(await api.post<Sheet>(`/api/manufacturing/sheets/${current.id}/post`))
      setConfirming(false)
    } catch (err) {
      setError(errorMessage(err))
      setConfirming(false)
    } finally {
      setBusy(null)
    }
  }

  const voidSheet = async () => {
    if (
      !window.confirm(
        'Delete this transaction in QuickBooks? Every quantity it moved is reversed.',
      )
    )
      return
    setBusy('void')
    setError(null)
    try {
      apply(await api.post<Sheet>(`/api/manufacturing/sheets/${current.id}/void`))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const blocked = (preview?.problems ?? []).length > 0

  return (
    <Modal open title={`Made items — ${current.reference}`} onClose={onClose} wide>
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge className={STATUS_CLASSES[current.status]}>{current.status}</Badge>
          {current.qbo_doc_number ? (
            <span className="text-xs text-ink-600">
              QuickBooks expense #{current.qbo_doc_number}
            </span>
          ) : null}
          {current.posted_at ? (
            <span className="text-xs text-ink-500">
              posted {formatDateTime(current.posted_at)} by {current.posted_by}
            </span>
          ) : null}
          {current.voided_at ? (
            <span className="text-xs text-amber-700">
              voided {formatDateTime(current.voided_at)} by {current.voided_by}
            </span>
          ) : null}
        </div>

        {!draft ? (
          <Alert tone="info">
            A posted sheet cannot be changed — it is part of the accounting record.
            To correct it, void it and make a new one.
          </Alert>
        ) : null}

        {/* Items made */}
        <div>
          <h3 className="mb-1 text-sm font-semibold text-ink-800">Items made</h3>
          {current.lines.length === 0 ? (
            <p className="text-sm text-ink-500">Nothing on this sheet yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-ink-500">
                  <tr>
                    <th className="py-1 pr-3 font-medium">SKU</th>
                    <th className="py-1 pr-3 font-medium">Quantity</th>
                    <th className="py-1 pr-3 font-medium">Unit cost</th>
                    <th className="py-1 pr-3 text-right font-medium">Amount</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {current.lines.map((line) => (
                    <tr key={line.id} className="border-t border-ink-100">
                      <td className="py-1.5 pr-3">
                        <div className="font-medium text-ink-800">{line.sku}</div>
                        <div className="text-xs text-ink-500">{line.name}</div>
                        {!line.qbo_item_id ? (
                          <div className="text-xs text-red-700">
                            Not linked to a QuickBooks item
                          </div>
                        ) : null}
                      </td>
                      <td className="py-1.5 pr-3">
                        <div className="w-20">
                          <input
                            className={inputClass}
                            inputMode="numeric"
                            disabled={!draft || busy === line.id}
                            defaultValue={line.quantity}
                            onBlur={(e) => {
                              const value = Number(e.target.value)
                              if (value > 0 && value !== line.quantity)
                                updateLine(line.id, { quantity: value })
                            }}
                          />
                        </div>
                      </td>
                      <td className="py-1.5 pr-3">
                        <div className="w-28">
                          <input
                            className={inputClass}
                            inputMode="decimal"
                            disabled={!draft || busy === line.id}
                            defaultValue={line.unit_cost}
                            onBlur={(e) => {
                              if (e.target.value !== line.unit_cost)
                                updateLine(line.id, { unit_cost: e.target.value })
                            }}
                          />
                        </div>
                        {line.cost_from_bom ? (
                          <div className="text-xs text-ink-500">from BOM</div>
                        ) : null}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {currency(line.amount)}
                      </td>
                      <td className="py-1.5 text-right">
                        {draft ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy === line.id}
                            onClick={() => removeLine(line.id)}
                          >
                            Remove
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {draft ? (
          <div className="flex flex-wrap items-end gap-2 rounded-md bg-ink-50 p-3">
            <div className="min-w-56 flex-1">
              <Field label="Add a product" hint="Cost is prefilled from its BOM where one exists.">
                <select
                  className={inputClass}
                  value={addProduct}
                  onChange={(e) => setAddProduct(e.target.value)}
                >
                  <option value="">Choose a product…</option>
                  {products.map((product) => (
                    <option key={product.id} value={product.id}>
                      {product.sku} — {product.name}
                      {product.qbo_item_id ? '' : ' (not linked to QuickBooks)'}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="Quantity">
              <div className="w-24">
                <input
                  className={inputClass}
                  inputMode="numeric"
                  value={addQty}
                  onChange={(e) => setAddQty(e.target.value.replace(/\D/g, ''))}
                />
              </div>
            </Field>
            <Button onClick={addLine} disabled={!addProduct || busy === 'add'}>
              {busy === 'add' ? 'Adding…' : 'Add'}
            </Button>
          </div>
        ) : null}

        {/* What it does to the books */}
        {preview ? (
          <div className="space-y-2 rounded-md border border-ink-200 p-3">
            <h3 className="text-sm font-semibold text-ink-800">
              What posting does in QuickBooks
            </h3>
            <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
              <dt className="text-ink-600">Inventory added ({preview.made.length} items)</dt>
              <dd className="text-right tabular-nums text-emerald-700">
                +{currency(preview.made_total)}
              </dd>
              {preview.consumed.length ? (
                <>
                  <dt className="text-ink-600">
                    Components consumed ({preview.consumed.length} items)
                  </dt>
                  <dd className="text-right tabular-nums text-amber-700">
                    −{currency(preview.consumed_total)}
                  </dd>
                </>
              ) : null}
              <dt className="border-t border-ink-200 pt-1 font-medium text-ink-800">
                {preview.account_name ?? 'Manufacturing account'}
              </dt>
              <dd className="border-t border-ink-200 pt-1 text-right font-medium tabular-nums">
                {currency(preview.net_to_account)}
              </dd>
            </dl>

            {preview.consumed.length ? (
              <details className="text-xs text-ink-600">
                <summary className="cursor-pointer">Components that come out of stock</summary>
                <ul className="mt-1 space-y-0.5">
                  {preview.consumed.map((row) => (
                    <li key={row.sku}>
                      {row.quantity} × {row.sku}
                      {row.amount === null ? (
                        <span className="text-amber-700">
                          {' '}
                          — no cost in QuickBooks, quantity moves but value does not
                        </span>
                      ) : (
                        <span className="text-ink-500"> — {currency(row.amount)}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            ) : (
              <p className="text-xs text-ink-500">
                Nothing comes out of stock: none of these products has a BOM.
              </p>
            )}

            {preview.problems.length ? (
              <Alert tone="warning">
                <span className="font-medium">Not ready to post:</span>
                <ul className="mt-1 list-disc space-y-0.5 pl-4">
                  {preview.problems.map((problem, i) => (
                    <li key={i}>{problem}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}
          </div>
        ) : null}

        {error ? <Alert tone="error">{error}</Alert> : null}

        <div className="flex flex-wrap items-center gap-2">
          {draft ? (
            <Button
              variant="primary"
              disabled={blocked || busy === 'post' || current.lines.length === 0}
              onClick={() => setConfirming(true)}
            >
              Post to QuickBooks
            </Button>
          ) : null}
          {current.status === 'posted' ? (
            <Button disabled={busy === 'void'} onClick={voidSheet}>
              {busy === 'void' ? 'Voiding…' : 'Void in QuickBooks'}
            </Button>
          ) : null}
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>

      {confirming ? (
        <Modal open title="Post to QuickBooks?" onClose={() => setConfirming(false)}>
          <div className="space-y-3">
            <p className="text-sm text-ink-700">
              This writes a real expense to your books and changes quantities on hand.
              It can be undone with Void, which deletes the transaction.
            </p>
            <dl className="grid grid-cols-[1fr_auto] gap-x-4 text-sm">
              <dt className="text-ink-600">Inventory added</dt>
              <dd className="text-right tabular-nums">
                +{currency(preview?.made_total ?? null)}
              </dd>
              <dt className="text-ink-600">Components consumed</dt>
              <dd className="text-right tabular-nums">
                −{currency(preview?.consumed_total ?? null)}
              </dd>
              <dt className="font-medium text-ink-800">
                {preview?.account_name ?? 'Manufacturing account'}
              </dt>
              <dd className="text-right font-medium tabular-nums">
                {currency(preview?.net_to_account ?? null)}
              </dd>
            </dl>
            <div className={cx('flex gap-2')}>
              <Button variant="primary" onClick={post} disabled={busy === 'post'}>
                {busy === 'post' ? 'Posting…' : 'Yes, post it'}
              </Button>
              <Button variant="ghost" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </div>
          </div>
        </Modal>
      ) : null}
    </Modal>
  )
}
