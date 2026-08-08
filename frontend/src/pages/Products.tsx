import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type {
  BambuddyArchive,
  BambuddyPrinter,
  Fulfillment,
  Product,
  QboItem,
} from '../lib/types'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Modal,
  Spinner,
  inputClass,
} from '../components/ui'

const FULFILLMENTS: { value: Fulfillment; label: string; hint: string }[] = [
  { value: 'printed', label: 'Printed', hint: 'Made on the print farm when stock runs short.' },
  { value: 'stocked', label: 'Stocked', hint: 'Always pulled from inventory.' },
  { value: 'bundle', label: 'Bundle', hint: 'Explodes into components via its BOM.' },
]

export default function Products() {
  const [products, setProducts] = useState<Product[] | null>(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Product | 'new' | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ products: Product[] }>(
        `/api/products?q=${encodeURIComponent(query)}`,
      )
      setProducts(data.products)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [query])

  useEffect(() => {
    load()
  }, [load])

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Products</h1>
          <input
            className={`${inputClass} max-w-xs`}
            placeholder="Search SKU or name…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Button variant="primary" className="ml-auto" onClick={() => setEditing('new')}>
            New product
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!products ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : products.length === 0 ? (
          <EmptyState
            title={query ? 'No products match that search' : 'No products yet'}
            description="Products map Etsy SKUs to what actually gets made: a printed part, a stocked item, or a bundle with a bill of materials."
            action={
              <Button variant="primary" onClick={() => setEditing('new')}>
                Create the first product
              </Button>
            }
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {products.map((product) => (
              <button
                key={product.id}
                type="button"
                onClick={() => setEditing(product)}
                className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left hover:bg-ink-50"
              >
                <span className="font-mono text-sm font-medium text-ink-900">
                  {product.sku}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-ink-600">
                  {product.name}
                </span>
                <Badge>{product.fulfillment}</Badge>
                {product.fulfillment === 'bundle' ? (
                  <Badge
                    className={
                      product.bom.length
                        ? 'bg-violet-100 text-violet-800 ring-violet-300'
                        : 'bg-red-100 text-red-800 ring-red-300'
                    }
                  >
                    {product.bom.length} components
                  </Badge>
                ) : null}
                {product.fulfillment === 'printed' ? (
                  <Badge
                    className={
                      product.print_mapping
                        ? 'bg-emerald-100 text-emerald-800 ring-emerald-300'
                        : 'bg-red-100 text-red-800 ring-red-300'
                    }
                  >
                    {product.print_mapping ? 'mapped' : 'no print mapping'}
                  </Badge>
                ) : null}
                {product.qbo_item_id ? <Badge>QBO linked</Badge> : null}
                {!product.active ? <Badge>inactive</Badge> : null}
              </button>
            ))}
          </Card>
        )}
      </div>

      {editing ? (
        <ProductEditor
          product={editing === 'new' ? null : editing}
          allProducts={products ?? []}
          onClose={() => setEditing(null)}
          onSaved={async (saved) => {
            await load()
            setEditing(saved)
          }}
          onDeleted={async () => {
            await load()
            setEditing(null)
          }}
        />
      ) : null}
    </div>
  )
}

function ProductEditor({
  product,
  allProducts,
  onClose,
  onSaved,
  onDeleted,
}: {
  product: Product | null
  allProducts: Product[]
  onClose: () => void
  onSaved: (product: Product) => void | Promise<void>
  onDeleted: () => void | Promise<void>
}) {
  const [sku, setSku] = useState(product?.sku ?? '')
  const [name, setName] = useState(product?.name ?? '')
  const [fulfillment, setFulfillment] = useState<Fulfillment>(
    product?.fulfillment ?? 'printed',
  )
  const [active, setActive] = useState(product?.active ?? true)
  const [qboItem, setQboItem] = useState<{ id: string; name: string } | null>(
    product?.qbo_item_id
      ? { id: product.qbo_item_id, name: product.qbo_item_name ?? product.qbo_item_id }
      : null,
  )
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [qboPickerOpen, setQboPickerOpen] = useState(false)
  const [archivePickerOpen, setArchivePickerOpen] = useState(false)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const body = {
        sku,
        name,
        fulfillment,
        qbo_item_id: qboItem?.id ?? null,
        qbo_item_name: qboItem?.name ?? null,
        active,
      }
      const saved = product
        ? await api.put<Product>(`/api/products/${product.id}`, body)
        : await api.post<Product>('/api/products', body)
      await onSaved(saved)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!product) return
    if (!window.confirm(`Delete ${product.sku}? This cannot be undone.`)) return
    setBusy(true)
    try {
      await api.del(`/api/products/${product.id}`)
      await onDeleted()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      wide
      title={product ? `Edit ${product.sku}` : 'New product'}
      onClose={onClose}
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="SKU" hint="Matched against the Etsy SKU, ignoring case and spaces.">
            <input
              className={inputClass}
              value={sku}
              onChange={(e) => setSku(e.target.value)}
            />
          </Field>
          <Field label="Name">
            <input
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
        </div>

        <Field
          label="Fulfillment"
          hint={FULFILLMENTS.find((f) => f.value === fulfillment)?.hint}
        >
          <select
            className={inputClass}
            value={fulfillment}
            onChange={(e) => setFulfillment(e.target.value as Fulfillment)}
          >
            {FULFILLMENTS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </Field>

        {fulfillment !== 'bundle' ? (
          <Field
            label="QuickBooks item"
            hint="Quantity on hand comes from here. A printed SKU with no item linked always prints in full."
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-ink-700">
                {qboItem ? `${qboItem.name} (${qboItem.id})` : 'Not linked'}
              </span>
              <Button size="sm" onClick={() => setQboPickerOpen(true)}>
                {qboItem ? 'Change' : 'Link item'}
              </Button>
              {qboItem ? (
                <Button size="sm" variant="ghost" onClick={() => setQboItem(null)}>
                  Unlink
                </Button>
              ) : null}
            </div>
          </Field>
        ) : null}

        <label className="flex items-center gap-2 text-sm text-ink-700">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
          />
          Active
        </label>

        {error ? <Alert tone="error">{error}</Alert> : null}

        <div className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={save} disabled={busy || !sku || !name}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
          {product ? (
            <Button variant="ghost" onClick={remove} disabled={busy}>
              Delete
            </Button>
          ) : null}
        </div>

        {product && fulfillment === 'bundle' ? (
          <BomEditor product={product} allProducts={allProducts} onSaved={onSaved} />
        ) : null}

        {product && fulfillment === 'printed' ? (
          <PrintMappingEditor
            product={product}
            onSaved={onSaved}
            onOpenPicker={() => setArchivePickerOpen(true)}
          />
        ) : null}

        {!product ? (
          <Alert tone="info">
            Save the product first — the BOM editor and Bambuddy mapping appear afterwards.
          </Alert>
        ) : null}
      </div>

      {qboPickerOpen ? (
        <QboItemPicker
          onClose={() => setQboPickerOpen(false)}
          onPick={(item) => {
            setQboItem({ id: item.id, name: item.name })
            setQboPickerOpen(false)
          }}
        />
      ) : null}

      {archivePickerOpen && product ? (
        <ArchivePicker
          onClose={() => setArchivePickerOpen(false)}
          onPick={async (archive, printerId) => {
            setArchivePickerOpen(false)
            const saved = await api.put<Product>(`/api/products/${product.id}/print-mapping`, {
              bambuddy_archive_id: archive.id,
              bambuddy_archive_name: archive.name,
              plate_number: product.print_mapping?.plate_number ?? 1,
              units_per_plate: product.print_mapping?.units_per_plate ?? 1,
              print_options: product.print_mapping?.print_options ?? {},
              preferred_printer_id: printerId,
            })
            await onSaved(saved)
          }}
        />
      ) : null}
    </Modal>
  )
}

function BomEditor({
  product,
  allProducts,
  onSaved,
}: {
  product: Product
  allProducts: Product[]
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [componentId, setComponentId] = useState('')
  const [quantity, setQuantity] = useState(1)
  const [error, setError] = useState<string | null>(null)

  // Single-level BOMs: a bundle may never contain another bundle.
  const candidates = allProducts.filter(
    (p) => p.fulfillment !== 'bundle' && !product.bom.some((b) => b.component_id === p.id),
  )

  const add = async () => {
    setError(null)
    try {
      const saved = await api.post<Product>(`/api/products/${product.id}/bom`, {
        component_id: componentId,
        quantity,
      })
      setComponentId('')
      setQuantity(1)
      await onSaved(saved)
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const update = async (bomId: string, newQuantity: number) => {
    const entry = product.bom.find((b) => b.id === bomId)
    if (!entry) return
    const saved = await api.put<Product>(`/api/products/${product.id}/bom/${bomId}`, {
      component_id: entry.component_id,
      quantity: newQuantity,
    })
    await onSaved(saved)
  }

  const remove = async (bomId: string) => {
    const saved = await api.del<Product>(`/api/products/${product.id}/bom/${bomId}`)
    await onSaved(saved)
  }

  return (
    <section className="rounded-lg bg-ink-50 p-3">
      <h3 className="text-sm font-semibold text-ink-800">Bill of materials</h3>
      <p className="mt-0.5 text-xs text-ink-500">
        One level only. Ordering this bundle creates a component line for each row.
      </p>

      {product.bom.length === 0 ? (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
          No components yet — orders for this bundle cannot be produced.
        </p>
      ) : (
        <ul className="mt-3 space-y-1.5">
          {product.bom.map((entry) => (
            <li key={entry.id} className="flex flex-wrap items-center gap-2">
              {/* inputClass carries w-full, so the width has to live on a wrapper. */}
              <div className="w-20 shrink-0">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  value={entry.quantity}
                  onChange={(e) => update(entry.id, Number(e.target.value))}
                />
              </div>
              <span className="font-mono text-sm text-ink-800">{entry.component_sku}</span>
              <span className="min-w-0 flex-1 truncate text-sm text-ink-600">
                {entry.component_name}
              </span>
              <Badge>{entry.component_fulfillment}</Badge>
              <Button size="sm" variant="ghost" onClick={() => remove(entry.id)}>
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <div className="w-20">
          <Field label="Qty">
            <input
              className={inputClass}
              type="number"
              min={1}
              value={quantity}
              onChange={(e) => setQuantity(Number(e.target.value))}
            />
          </Field>
        </div>
        <div className="min-w-48 flex-1">
          <Field label="Component">
            <select
              className={inputClass}
              value={componentId}
              onChange={(e) => setComponentId(e.target.value)}
            >
              <option value="">Select a product…</option>
              {candidates.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.sku} — {candidate.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Button onClick={add} disabled={!componentId}>
          Add
        </Button>
      </div>
      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
    </section>
  )
}

function PrintMappingEditor({
  product,
  onSaved,
  onOpenPicker,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
  onOpenPicker: () => void
}) {
  const mapping = product.print_mapping
  const [plate, setPlate] = useState(mapping?.plate_number ?? 1)
  const [units, setUnits] = useState(mapping?.units_per_plate ?? 1)
  const [options, setOptions] = useState(
    JSON.stringify(mapping?.print_options ?? {}, null, 0),
  )
  const [printerId, setPrinterId] = useState<string>(
    mapping?.preferred_printer_id ? String(mapping.preferred_printer_id) : '',
  )
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPlate(mapping?.plate_number ?? 1)
    setUnits(mapping?.units_per_plate ?? 1)
    setOptions(JSON.stringify(mapping?.print_options ?? {}, null, 0))
    setPrinterId(mapping?.preferred_printer_id ? String(mapping.preferred_printer_id) : '')
  }, [mapping])

  const save = async () => {
    setError(null)
    try {
      const saved = await api.put<Product>(`/api/products/${product.id}/print-mapping`, {
        bambuddy_archive_id: mapping?.bambuddy_archive_id,
        bambuddy_archive_name: mapping?.bambuddy_archive_name,
        plate_number: plate,
        units_per_plate: units,
        print_options: options.trim() ? JSON.parse(options) : {},
        preferred_printer_id: printerId ? Number(printerId) : null,
      })
      await onSaved(saved)
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const clear = async () => {
    const saved = await api.del<Product>(`/api/products/${product.id}/print-mapping`)
    await onSaved(saved)
  }

  return (
    <section className="rounded-lg bg-ink-50 p-3">
      <h3 className="text-sm font-semibold text-ink-800">Bambuddy print mapping</h3>
      {!mapping ? (
        <div className="mt-2 space-y-2">
          <p className="text-sm text-red-800">
            No archive attached — orders needing this SKU cannot be queued.
          </p>
          <Button variant="primary" size="sm" onClick={onOpenPicker}>
            Browse Bambuddy archives
          </Button>
        </div>
      ) : (
        <div className="mt-2 space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-ink-700">
              {mapping.bambuddy_archive_name ?? `Archive ${mapping.bambuddy_archive_id}`}
            </span>
            <Badge>id {mapping.bambuddy_archive_id}</Badge>
            <Button size="sm" onClick={onOpenPicker}>
              Change archive
            </Button>
            <Button size="sm" variant="ghost" onClick={clear}>
              Remove mapping
            </Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Plate number">
              <input
                className={inputClass}
                type="number"
                min={1}
                value={plate}
                onChange={(e) => setPlate(Number(e.target.value))}
              />
            </Field>
            <Field label="Units per plate" hint="How many of this SKU one plate yields.">
              <input
                className={inputClass}
                type="number"
                min={1}
                value={units}
                onChange={(e) => setUnits(Number(e.target.value))}
              />
            </Field>
            <Field label="Preferred printer ID" hint="Blank lets Bambuddy dispatch.">
              <input
                className={inputClass}
                value={printerId}
                onChange={(e) => setPrinterId(e.target.value)}
              />
            </Field>
          </div>
          <Field label="Print options (JSON)" hint="Merged into the Bambuddy queue request.">
            <input
              className={inputClass}
              value={options}
              onChange={(e) => setOptions(e.target.value)}
            />
          </Field>
          {error ? <Alert tone="error">{error}</Alert> : null}
          <Button size="sm" variant="primary" onClick={save}>
            Save mapping
          </Button>
        </div>
      )}
    </section>
  )
}

function QboItemPicker({
  onClose,
  onPick,
}: {
  onClose: () => void
  onPick: (item: QboItem) => void
}) {
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<QboItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setBusy(true)
    api
      .get<{ items: QboItem[] }>(`/api/integrations/qbo/items?q=${encodeURIComponent(query)}`)
      .then((data) => {
        setItems(data.items)
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }, [query])

  return (
    <Modal open title="Link a QuickBooks item" onClose={onClose}>
      <input
        className={inputClass}
        placeholder="Search items by name…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
        {busy ? <Spinner /> : null}
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onPick(item)}
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-ink-50"
          >
            <span className="min-w-0 flex-1 truncate text-sm text-ink-800">{item.name}</span>
            {item.sku ? <span className="font-mono text-xs text-ink-500">{item.sku}</span> : null}
            <Badge>
              {item.tracked ? `${item.qty_on_hand ?? 0} on hand` : 'not tracked'}
            </Badge>
          </button>
        ))}
      </div>
    </Modal>
  )
}

function ArchivePicker({
  onClose,
  onPick,
}: {
  onClose: () => void
  onPick: (archive: BambuddyArchive, printerId: number | null) => void | Promise<void>
}) {
  const [query, setQuery] = useState('')
  const [archives, setArchives] = useState<BambuddyArchive[]>([])
  const [printers, setPrinters] = useState<BambuddyPrinter[]>([])
  const [printerId, setPrinterId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .get<{ printers: BambuddyPrinter[] }>('/api/integrations/bambuddy/printers')
      .then((data) => setPrinters(data.printers))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    setBusy(true)
    api
      .get<{ archives: BambuddyArchive[] }>(
        `/api/integrations/bambuddy/archives?search=${encodeURIComponent(query)}`,
      )
      .then((data) => {
        setArchives(data.archives.filter((a) => a.id !== null))
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }, [query])

  return (
    <Modal open title="Attach a Bambuddy archive" onClose={onClose}>
      <input
        className={inputClass}
        placeholder="Search archived 3MF files…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="mt-3">
        <Field label="Preferred printer" hint="Leave as Any to let Bambuddy dispatch.">
          <select
            className={inputClass}
            value={printerId}
            onChange={(e) => setPrinterId(e.target.value)}
          >
            <option value="">Any printer</option>
            {printers.map((printer) => (
              <option key={printer.id} value={printer.id ?? ''}>
                {printer.name ?? `Printer ${printer.id}`}
              </option>
            ))}
          </select>
        </Field>
      </div>
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      <div className="mt-3 max-h-72 space-y-1 overflow-y-auto">
        {busy ? <Spinner /> : null}
        {!busy && archives.length === 0 ? (
          <p className="py-4 text-center text-sm text-ink-500">No archives found.</p>
        ) : null}
        {archives.map((archive) => (
          <button
            key={String(archive.id)}
            type="button"
            onClick={() => onPick(archive, printerId ? Number(printerId) : null)}
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-ink-50"
          >
            <span className="min-w-0 flex-1 truncate text-sm text-ink-800">
              {archive.name ?? `Archive ${archive.id}`}
            </span>
            {archive.plates ? <Badge>{archive.plates} plates</Badge> : null}
            <Badge>id {archive.id}</Badge>
          </button>
        ))}
      </div>
    </Modal>
  )
}
