import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type {
  BambuddyArchive,
  BambuddyPrinter,
  BomEntry,
  Catalog,
  CatalogRow,
  Fulfillment,
  ObservedOption,
  Product,
  ProductVariation,
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
  cx,
  inputClass,
} from '../components/ui'

const FULFILLMENTS: { value: Fulfillment; label: string; hint: string }[] = [
  { value: 'printed', label: 'Printed', hint: 'Made on the print farm when stock runs short.' },
  { value: 'stocked', label: 'Stocked', hint: 'Always pulled from inventory.' },
  { value: 'bundle', label: 'Bundle', hint: 'Explodes into components via its BOM.' },
]

/** Masters first, each followed by its variants.
 *
 * A variant is a product in its own right, but it is not a thing the shop
 * sells on its own — it is one way of making the listing above it. Listing
 * both flat, alphabetically, would scatter families across the screen. */
function nest(products: Product[]): { product: Product; depth: number }[] {
  const children = new Map<string, Product[]>()
  for (const product of products) {
    if (!product.parent_id) continue
    children.set(product.parent_id, [...(children.get(product.parent_id) ?? []), product])
  }
  const rows: { product: Product; depth: number }[] = []
  for (const product of products) {
    // A variant whose master is not in this result set (filtered out by a
    // search, say) still has to appear, or it would be unreachable.
    if (product.parent_id && products.some((p) => p.id === product.parent_id)) continue
    rows.push({ product, depth: 0 })
    for (const child of children.get(product.id) ?? []) {
      rows.push({ product: child, depth: 1 })
    }
  }
  return rows
}

export default function Products() {
  const [products, setProducts] = useState<Product[] | null>(null)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Product | 'new' | null>(null)
  const [checking, setChecking] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

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

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current)
      if (!next.delete(id)) next.add(id)
      return next
    })

  const deleteSelected = async () => {
    const ids = [...selected]
    if (!ids.length) return
    if (
      !window.confirm(
        `Delete ${ids.length} product${ids.length === 1 ? '' : 's'}? This cannot be undone.`,
      )
    )
      return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.post<{
        deleted: number
        kept: { name: string; reason: string }[]
      }>('/api/products/bulk-delete', { product_ids: ids })
      setSelected(new Set())
      // Anything held back is named with its reason — "3 of 5 deleted" without
      // saying which, or why, is not an answer.
      setNotice(
        `Deleted ${result.deleted}.` +
          (result.kept.length
            ? ` Kept ${result.kept.length}: ` +
              result.kept.map((k) => `${k.name} — ${k.reason}`).join(' ')
            : ''),
      )
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Products</h1>
          <input
            className={`${inputClass} max-w-xs`}
            placeholder="Search products…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {selected.size ? (
            <Button
              className="ml-auto"
              variant="ghost"
              disabled={busy}
              onClick={deleteSelected}
            >
              {busy ? 'Deleting…' : `Delete ${selected.size} selected`}
            </Button>
          ) : null}
          <Button
            className={selected.size ? '' : 'ml-auto'}
            onClick={() => setChecking(true)}
          >
            Check against Etsy
          </Button>
          <Button variant="primary" onClick={() => setEditing('new')}>
            New product
          </Button>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}
        {notice ? <Alert tone="info">{notice}</Alert> : null}

        {!products ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : products.length === 0 ? (
          <EmptyState
            title={query ? 'No products match that search' : 'No products yet'}
            description="Products map Etsy listings to what actually gets made: a printed part, a stocked item, or a bundle with a bill of materials."
            action={
              <Button variant="primary" onClick={() => setEditing('new')}>
                Create the first product
              </Button>
            }
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {nest(products).map(({ product, depth }) => (
              // The checkbox sits outside the row button: a control inside a
              // button is not reachable on its own.
              <div
                key={product.id}
                className="flex w-full items-center gap-2 pl-4 hover:bg-ink-50"
                style={{ paddingLeft: 16 + depth * 24 }}
              >
                <input
                  type="checkbox"
                  aria-label={`Select ${product.name}`}
                  checked={selected.has(product.id)}
                  onChange={() => toggle(product.id)}
                />
                <button
                  type="button"
                  onClick={() => setEditing(product)}
                  className="flex min-w-0 flex-1 flex-wrap items-center gap-2 py-3 pr-4 text-left"
                >
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink-900">
                  {depth ? <span className="text-ink-400">↳ </span> : null}
                  {product.name}
                </span>
                <span className="font-mono text-xs text-ink-500">{product.sku}</span>
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
              </div>
            ))}
          </Card>
        )}
      </div>

      {checking ? (
        <CatalogCheck
          allProducts={products ?? []}
          onClose={() => setChecking(false)}
          onChanged={load}
        />
      ) : null}

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
    if (!window.confirm(`Delete ${product.name}? This cannot be undone.`)) return
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
      title={product ? `Edit ${product.name}` : 'New product'}
      onClose={onClose}
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Code"
            hint="Optional. Orders match on the Etsy listing, not on this — leave it blank and one is generated."
          >
            <input
              className={inputClass}
              placeholder="generated from the Etsy listing"
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
            hint="Quantity on hand comes from here. A printed product with no item linked always prints in full."
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
          <Button variant="primary" onClick={save} disabled={busy || !name}>
            {busy ? 'Saving…' : 'Save'}
          </Button>
          {product ? (
            <Button variant="ghost" onClick={remove} disabled={busy}>
              Delete
            </Button>
          ) : null}
        </div>

        {product && fulfillment === 'bundle' ? (
          <>
            <BomEditor product={product} allProducts={allProducts} onSaved={onSaved} />
            <OptionRulesEditor
              product={product}
              allProducts={allProducts}
              onSaved={onSaved}
            />
          </>
        ) : null}

        {product && fulfillment === 'printed' ? (
          <PrintMappingEditor
            product={product}
            onSaved={onSaved}
            onOpenPicker={() => setArchivePickerOpen(true)}
          />
        ) : null}

        {product ? <EtsyLinksEditor product={product} onSaved={onSaved} /> : null}

        {product && fulfillment !== 'bundle' ? (
          // A bundle's options change its BOM, which is what option rules are
          // for; variations are about the thing itself.
          <VariationsEditor product={product} onSaved={onSaved} />
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
            No archive attached — orders needing this product cannot be queued.
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
            <Field label="Units per plate" hint="How many of this product one plate yields.">
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

/** Etsy options that change what a bundle is made of.
 *
 * The option names and values are typed by hand in Etsy's listing editor and
 * exist nowhere else in PrintFlow, so they are offered from what real orders
 * have carried rather than asked for from memory — a rule with a typo in it
 * fires on nothing and says nothing.
 */
function OptionRulesEditor({
  product,
  allProducts,
  onSaved,
}: {
  product: Product
  allProducts: Product[]
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [observed, setObserved] = useState<ObservedOption[] | null>(null)
  const [optionName, setOptionName] = useState('')
  const [optionValue, setOptionValue] = useState('')
  const [replacesId, setReplacesId] = useState('')
  const [componentId, setComponentId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  /* Two sources, merged: what past orders carried, and what the Etsy listing
     offers. The listing is the one that works before a single order has
     arrived, which is when the rules actually need writing. */
  useEffect(() => {
    let cancelled = false
    Promise.all([
      api
        .get<{ options: ObservedOption[] }>(
          `/api/products/${product.id}/observed-options`,
        )
        .then((d) => d.options)
        .catch(() => [] as ObservedOption[]),
      api
        .get<Catalog>('/api/integrations/etsy/catalog')
        .then((c) =>
          c.error
            ? []
            : c.rows
                .filter((row) => row.product_id === product.id)
                .flatMap((row) => row.listing_options),
        )
        .catch(() => [] as ObservedOption[]),
    ]).then(([fromOrders, fromListing]) => {
      if (cancelled) return
      const merged = new Map<string, Map<string, number | undefined>>()
      for (const source of [fromOrders, fromListing]) {
        for (const option of source) {
          const values = merged.get(option.name) ?? new Map()
          for (const entry of option.values) {
            // An order count is the more informative of the two, so it wins.
            values.set(entry.value, entry.orders ?? values.get(entry.value))
          }
          merged.set(option.name, values)
        }
      }
      setObserved(
        [...merged.entries()].map(([name, values]) => ({
          name,
          values: [...values.entries()].map(([value, orders]) => ({ value, orders })),
        })),
      )
    })
    return () => {
      cancelled = true
    }
  }, [product.id])

  // Single-level BOMs, so an option can never bring in another bundle.
  const candidates = allProducts.filter((p) => p.fulfillment !== 'bundle')
  const values = observed?.find((o) => o.name === optionName)?.values ?? []

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      const saved = await api.post<Product>(
        `/api/products/${product.id}/option-rules`,
        {
          option_name: optionName.trim(),
          option_value: optionValue.trim(),
          component_id: componentId,
          replaces_id: replacesId || null,
          quantity: quantity ? Number(quantity) : null,
        },
      )
      await onSaved(saved)
      setOptionValue('')
      setComponentId('')
      setQuantity('')
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (ruleId: string) => {
    setBusy(true)
    try {
      await onSaved(
        await api.del<Product>(`/api/products/${product.id}/option-rules/${ruleId}`),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-ink-200 p-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-800">Etsy options</h3>
        <p className="text-xs text-ink-500">
          When a buyer picks an option, swap a component for another or add one.
          A colour choice usually swaps the filament.
        </p>
      </div>

      {product.option_rules.length === 0 ? (
        <p className="text-sm text-ink-500">
          No option rules — every order uses the BOM above as it stands.
        </p>
      ) : (
        <ul className="space-y-1 text-sm">
          {product.option_rules.map((rule) => (
            <li
              key={rule.id}
              className="flex flex-wrap items-center gap-2 border-b border-ink-100 pb-1"
            >
              <span className="font-medium text-ink-800">
                {rule.option_name} = {rule.option_value}
              </span>
              <span className="text-ink-600">
                {rule.replaces_sku
                  ? `swap ${rule.replaces_sku} → ${rule.component_sku}`
                  : `add ${rule.component_sku}`}
                {rule.quantity ? ` × ${rule.quantity}` : ''}
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto"
                disabled={busy}
                onClick={() => remove(rule.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      {observed !== null && observed.length === 0 ? (
        <Alert tone="info">
          No orders for this product have carried options yet. Rules can still be
          typed in, but the names and values must match Etsy's exactly — it is
          usually easier to wait for one order and pick them from the list.
        </Alert>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-2">
        <Field label="Option">
          {observed && observed.length ? (
            <select
              className={inputClass}
              value={optionName}
              onChange={(e) => {
                setOptionName(e.target.value)
                setOptionValue('')
              }}
            >
              <option value="">Choose an option…</option>
              {observed.map((option) => (
                <option key={option.name} value={option.name}>
                  {option.name}
                </option>
              ))}
            </select>
          ) : (
            <input
              className={inputClass}
              placeholder="Color"
              value={optionName}
              onChange={(e) => setOptionName(e.target.value)}
            />
          )}
        </Field>
        <Field label="Value">
          {values.length ? (
            <select
              className={inputClass}
              value={optionValue}
              onChange={(e) => setOptionValue(e.target.value)}
            >
              <option value="">Choose a value…</option>
              {values.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.value}
                  {entry.orders
                    ? ` (${entry.orders} order${entry.orders === 1 ? '' : 's'})`
                    : ' (from the listing)'}
                </option>
              ))}
            </select>
          ) : (
            <input
              className={inputClass}
              placeholder="Red"
              value={optionValue}
              onChange={(e) => setOptionValue(e.target.value)}
            />
          )}
        </Field>
        <Field label="Replaces" hint="Leave as “nothing” to add a component instead.">
          <select
            className={inputClass}
            value={replacesId}
            onChange={(e) => setReplacesId(e.target.value)}
          >
            <option value="">nothing — just add</option>
            {product.bom.map((entry: BomEntry) => (
              <option key={entry.id} value={entry.component_id}>
                {entry.component_sku}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Use this component">
          <select
            className={inputClass}
            value={componentId}
            onChange={(e) => setComponentId(e.target.value)}
          >
            <option value="">Choose a component…</option>
            {candidates.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.sku} — {candidate.name}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Quantity"
          hint={replacesId ? 'Blank keeps the quantity from the BOM.' : 'Blank means one.'}
        >
          <input
            className={inputClass}
            inputMode="numeric"
            placeholder={replacesId ? 'same as BOM' : '1'}
            value={quantity}
            onChange={(e) => setQuantity(e.target.value.replace(/\D/g, ''))}
          />
        </Field>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}
      <Button
        onClick={add}
        disabled={busy || !optionName.trim() || !optionValue.trim() || !componentId}
      >
        {busy ? 'Adding…' : 'Add rule'}
      </Button>
    </div>
  )
}

/** The buyable combinations of a product, and what each one changes.
 *
 * Read from the Etsy listing rather than typed: the option names and values
 * have to match Etsy's exactly for an order to attach to the right one, and a
 * typo there is silent — it prints the wrong plate and nobody finds out until
 * the parcel is open.
 */
function VariationsEditor({
  product,
  onSaved,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState<ProductVariation | null>(null)

  const sync = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<
        Product & {
          sync: { added: number; updated: number; retired: number; reattached: number }
        }
      >(`/api/products/${product.id}/variations/sync-etsy`, {})
      await onSaved(saved)
      const sync = saved.sync
      setNotice(
        `${sync.added} new, ${sync.updated} refreshed` +
          (sync.retired ? `, ${sync.retired} no longer sold` : '') +
          (sync.reattached ? `, ${sync.reattached} open order line(s) re-matched` : '') +
          '. Orders match these on their own.',
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const save = async (variation: ProductVariation, changes: Partial<ProductVariation>) => {
    setBusy(true)
    setError(null)
    try {
      const body = { ...variation, ...changes }
      await onSaved(
        await api.put<Product>(
          `/api/products/${product.id}/variations/${variation.id}`,
          {
            bambuddy_archive_id: body.bambuddy_archive_id,
            bambuddy_archive_name: body.bambuddy_archive_name,
            plate_number: body.plate_number,
            units_per_plate: body.units_per_plate,
            preferred_printer_id: body.preferred_printer_id,
            qbo_item_id: body.qbo_item_id,
            qbo_item_name: body.qbo_item_name,
            active: body.active,
          },
        ),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (variationId: string) => {
    setBusy(true)
    try {
      await onSaved(
        await api.del<Product>(`/api/products/${product.id}/variations/${variationId}`),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  /** Give a variation a product of its own, so it can have its own components. */
  const makeProduct = async (variation: ProductVariation, fulfillment: Fulfillment) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<Product & { moved: number }>(
        `/api/products/${product.id}/variations/${variation.id}/product`,
        { fulfillment },
      )
      await onSaved(saved)
      setNotice(
        `“${variation.label}” is now its own product — give it its components below.` +
          (saved.moved ? ` ${saved.moved} open order line(s) moved onto it.` : ''),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const detachProduct = async (variation: ProductVariation) => {
    setBusy(true)
    try {
      await onSaved(
        await api.del<Product>(
          `/api/products/${product.id}/variations/${variation.id}/product`,
        ),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-ink-200 p-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-48 flex-1">
          <h3 className="text-sm font-semibold text-ink-800">Variations</h3>
          <p className="text-xs text-ink-500">
            What the buyer can pick on Etsy. Orders attach to one automatically —
            by Etsy's variation id, or by the option values if Etsy has reissued
            the ids. A variation that is genuinely a different build gets its own
            product, nested under this one, with its own components; otherwise it
            uses this product's settings.
          </p>
        </div>
        <Button size="sm" onClick={sync} disabled={busy}>
          {busy ? 'Reading…' : 'Pull from Etsy'}
        </Button>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      {product.variations.length === 0 ? (
        <p className="text-sm text-ink-500">
          None yet.{' '}
          {product.etsy_links.length
            ? 'Pull them from Etsy — every combination the listing sells becomes a row.'
            : 'Link an Etsy listing below first; variations come from the listing.'}
        </p>
      ) : (
        <ul className="space-y-2">
          {product.variations.map((variation) => (
            <li
              key={variation.id}
              className={cx(
                'rounded-md border p-2',
                variation.active ? 'border-ink-200' : 'border-ink-200 bg-ink-50',
              )}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink-800">
                  {variation.label}
                </span>
                {!variation.active ? (
                  <Badge className="bg-amber-100 text-amber-800 ring-amber-300">
                    no longer sold on Etsy
                  </Badge>
                ) : null}
                {variation.etsy_product_id ? (
                  <span className="font-mono text-[11px] text-ink-400">
                    {variation.etsy_product_id}
                  </span>
                ) : null}
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  disabled={busy}
                  onClick={() => remove(variation.id)}
                >
                  Remove
                </Button>
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-ink-500">Is:</span>
                {variation.variant_product_id ? (
                  <>
                    <span className="text-ink-700">
                      {variation.variant_product_name}
                    </span>
                    <Badge>{variation.variant_product_fulfillment}</Badge>
                    <span className="text-ink-500">
                      — edit it in the list to set its components
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => detachProduct(variation)}
                    >
                      Detach
                    </Button>
                  </>
                ) : (
                  <>
                    <span className="text-ink-500">the product above</span>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => makeProduct(variation, 'bundle')}
                    >
                      Give it its own components
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => makeProduct(variation, product.fulfillment)}
                    >
                      …or its own {product.fulfillment} product
                    </Button>
                  </>
                )}
              </div>

              {product.fulfillment === 'printed' && !variation.variant_product_id ? (
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-ink-500">Prints:</span>
                  {variation.bambuddy_archive_id ? (
                    <>
                      <span className="text-ink-700">
                        {variation.bambuddy_archive_name ??
                          `archive ${variation.bambuddy_archive_id}`}
                        {variation.plate_number ? `, plate ${variation.plate_number}` : ''}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() =>
                          save(variation, {
                            bambuddy_archive_id: null,
                            bambuddy_archive_name: null,
                            plate_number: null,
                            units_per_plate: null,
                            preferred_printer_id: null,
                          })
                        }
                      >
                        Use the product's file
                      </Button>
                    </>
                  ) : (
                    <>
                      <span className="text-ink-500">the product's file</span>
                      <Button size="sm" variant="ghost" onClick={() => setPicking(variation)}>
                        Use a different one
                      </Button>
                    </>
                  )}
                </div>
              ) : null}

              {product.fulfillment === 'stocked' && !variation.variant_product_id ? (
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-ink-500">Stock:</span>
                  <span className="text-ink-700">
                    {variation.qbo_item_name ??
                      (variation.qbo_item_id
                        ? `item ${variation.qbo_item_id}`
                        : "the product's QuickBooks item")}
                  </span>
                  {variation.qbo_item_id ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() =>
                        save(variation, { qbo_item_id: null, qbo_item_name: null })
                      }
                    >
                      Use the product's item
                    </Button>
                  ) : (
                    <Button size="sm" variant="ghost" onClick={() => setPicking(variation)}>
                      Track separately
                    </Button>
                  )}
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {picking && product.fulfillment === 'printed' ? (
        <ArchivePicker
          onClose={() => setPicking(null)}
          onPick={async (archive, printerId) => {
            const variation = picking
            setPicking(null)
            await save(variation, {
              bambuddy_archive_id: archive.id,
              bambuddy_archive_name: archive.name,
              plate_number: variation.plate_number ?? 1,
              units_per_plate: variation.units_per_plate ?? 1,
              preferred_printer_id: printerId,
            })
          }}
        />
      ) : null}

      {picking && product.fulfillment === 'stocked' ? (
        <QboItemPicker
          onClose={() => setPicking(null)}
          onPick={async (item) => {
            const variation = picking
            setPicking(null)
            await save(variation, { qbo_item_id: item.id, qbo_item_name: item.name })
          }}
        />
      ) : null}
    </div>
  )
}

/** The Etsy listings that resolve to this product.
 *
 * This is how orders find their way here: a receipt carries the listing and
 * variation ids, and these links say what those ids mean.
 */
function EtsyLinksEditor({
  product,
  onSaved,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [listingId, setListingId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const add = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<Product & { also_fixed?: number }>(
        `/api/products/${product.id}/etsy-links`,
        { etsy_listing_id: Number(listingId) },
      )
      await onSaved(saved)
      setListingId('')
      const fixed = saved.also_fixed ?? 0
      if (fixed > 0) {
        setNotice(
          `${fixed} order line${fixed === 1 ? '' : 's'} that were waiting on this ` +
            'listing now match.',
        )
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (linkId: string) => {
    setBusy(true)
    setNotice(null)
    try {
      await onSaved(
        await api.del<Product>(`/api/products/${product.id}/etsy-links/${linkId}`),
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-ink-200 p-3">
      <div>
        <h3 className="text-sm font-semibold text-ink-800">Etsy listings</h3>
        <p className="text-xs text-ink-500">
          Orders match on these. The listing id is the number at the end of its
          Etsy URL. Links are also made for you by <em>Check against Etsy</em>, and
          from the order drawer when you match a line and tick “remember this
          listing”.
        </p>
      </div>

      {product.etsy_links.length === 0 ? (
        <p className="text-sm text-ink-500">
          No listings linked — no Etsy order can reach this product. Use
          <em>Check against Etsy</em>, or paste a listing id below.
        </p>
      ) : (
        <ul className="space-y-1 text-sm">
          {product.etsy_links.map((link) => (
            <li
              key={link.id}
              className="flex flex-wrap items-center gap-2 border-b border-ink-100 pb-1"
            >
              <a
                className="font-mono text-ink-800 underline"
                href={`https://www.etsy.com/listing/${link.etsy_listing_id}`}
                target="_blank"
                rel="noreferrer"
              >
                {link.etsy_listing_id}
              </a>
              {link.etsy_product_id ? (
                <span className="text-ink-600">
                  variation {link.etsy_product_id} only
                </span>
              ) : null}
              <span className="min-w-0 flex-1 truncate text-ink-500">
                {link.listing_title ?? ''}
              </span>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => remove(link.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      {error ? <Alert tone="error">{error}</Alert> : null}
      {notice ? <Alert tone="success">{notice}</Alert> : null}

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-40 flex-1">
          <Field label="Listing id">
            <input
              className={inputClass}
              inputMode="numeric"
              placeholder="1895497697"
              value={listingId}
              onChange={(e) => setListingId(e.target.value.replace(/\D/g, ''))}
            />
          </Field>
        </div>
        <Button onClick={add} disabled={busy || !listingId}>
          {busy ? 'Linking…' : 'Link listing'}
        </Button>
      </div>
    </div>
  )
}

const CATALOG_STATUS: Record<
  CatalogRow['status'],
  { label: string; className: string }
> = {
  matched: { label: 'matched', className: 'bg-emerald-100 text-emerald-800 ring-emerald-300' },
  linked: { label: 'linked listing', className: 'bg-sky-100 text-sky-800 ring-sky-300' },
  missing: { label: 'no product', className: 'bg-red-100 text-red-800 ring-red-300' },
  no_sku: { label: 'no product', className: 'bg-amber-100 text-amber-800 ring-amber-300' },
}

/** What Etsy sells, lined up against what PrintFlow can make.
 *
 * Intake already reports a line with no product, but only once a real order has
 * arrived and stalled on it. This is the same comparison up front, while there
 * is still time to fix it.
 */
function CatalogCheck({
  allProducts,
  onClose,
  onChanged,
}: {
  allProducts: Product[]
  onClose: () => void
  onChanged: () => void | Promise<void>
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [filter, setFilter] = useState<'all' | 'missing'>('missing')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setCatalog(null)
    try {
      setCatalog(await api.get<Catalog>('/api/integrations/etsy/catalog'))
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  /** Build products from Etsy's own listings, each one linked as it is created.
   *
   * One product per listing, not per variation — Etsy reissues a variation's
   * product id whenever the listing's options are edited, and what differs
   * between variations belongs in option rules. */
  const create = async (listingIds: number[], fulfillment: Fulfillment) => {
    if (!listingIds.length) return
    setBusy(listingIds.length === 1 ? String(listingIds[0]) : 'all')
    setError(null)
    setNotice(null)
    try {
      const result = await api.post<{
        created: unknown[]
        skipped: unknown[]
        also_fixed: number
      }>('/api/integrations/etsy/catalog/import', {
        listing_ids: listingIds,
        fulfillment,
      })
      const made = result.created.length
      const fixed = result.also_fixed
      setNotice(
        `Created ${made} product${made === 1 ? '' : 's'} from Etsy` +
          (fixed
            ? fixed === 1
              ? ', and one order line that was waiting now matches.'
              : `, and ${fixed} order lines that were waiting now match.`
            : '.'),
      )
      await onChanged()
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  /** Point a listing at a product that already exists, so its orders match. */
  const link = async (row: CatalogRow, productId: string) => {
    if (!row.listing_id || !productId) return
    setBusy(String(row.listing_id))
    setError(null)
    setNotice(null)
    try {
      await api.post<Product>(`/api/products/${productId}/etsy-links`, {
        etsy_listing_id: row.listing_id,
        listing_title: row.title,
      })
      await onChanged()
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const rows = (catalog?.rows ?? []).filter(
    (row) => filter === 'all' || !(row.status === 'matched' || row.status === 'linked'),
  )

  // Listings with no product yet, deduplicated — several variations of one
  // listing are one product.
  const needProducts = [
    ...new Set(
      (catalog?.rows ?? [])
        .filter((row) => row.status === 'missing' || row.status === 'no_sku')
        .map((row) => row.listing_id)
        .filter((id): id is number => typeof id === 'number'),
    ),
  ]

  return (
    <Modal open wide title="Check products against Etsy" onClose={onClose}>
      <div className="space-y-3">
        {catalog === null && !error ? (
          <div className="flex items-center gap-2 py-6 text-sm text-ink-600">
            <Spinner className="h-4 w-4" />
            Reading your Etsy listings…
          </div>
        ) : null}

        {catalog?.error ? (
          <Alert tone={catalog.needs_reconnect ? 'warning' : 'error'}>
            {catalog.error}
            {catalog.needs_reconnect ? (
              <span className="mt-1 block text-xs">
                Order polling is unaffected and keeps working — only this screen
                needs the extra permission.
              </span>
            ) : null}
          </Alert>
        ) : null}
        {error ? <Alert tone="error">{error}</Alert> : null}

        {catalog && !catalog.error ? (
          <>
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-ink-700">
                <strong>{catalog.counts.variants ?? 0}</strong> sellable variants
                across <strong>{catalog.counts.listings ?? 0}</strong> listings
              </span>
              <Badge className={CATALOG_STATUS.matched.className}>
                {catalog.counts.matched ?? 0} matched
              </Badge>
              {catalog.counts.linked ? (
                <Badge className={CATALOG_STATUS.linked.className}>
                  {catalog.counts.linked} by linked listing
                </Badge>
              ) : null}
              {catalog.counts.missing ? (
                <Badge className={CATALOG_STATUS.missing.className}>
                  {catalog.counts.missing} with no product
                </Badge>
              ) : null}
              {catalog.counts.no_sku ? (
                <Badge className={CATALOG_STATUS.no_sku.className}>
                  {catalog.counts.no_sku} not set up
                </Badge>
              ) : null}
              <div className="ml-auto flex gap-1 text-xs">
                {(['missing', 'all'] as const).map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setFilter(key)}
                    className={
                      filter === key
                        ? 'rounded-md bg-ink-900 px-2.5 py-1 font-medium text-white'
                        : 'rounded-md px-2.5 py-1 font-medium text-ink-600 hover:bg-ink-100'
                    }
                  >
                    {key === 'missing' ? 'Needs attention' : 'Everything'}
                  </button>
                ))}
              </div>
            </div>

            {catalog.notes.map((note, i) => (
              <p key={i} className="text-xs text-ink-500">
                {note}
              </p>
            ))}

            {needProducts.length ? (
              // The setup path for a shop that has never used SKUs: build the
              // products from the listings themselves, in one go.
              <div className="flex flex-wrap items-center gap-2 rounded-md bg-ink-100 p-2.5 text-sm">
                <span className="text-ink-700">
                  <strong>{needProducts.length}</strong> listing
                  {needProducts.length === 1 ? '' : 's'} with no product yet. Create
                  them from Etsy — each one is linked to its listing as it is made.
                </span>
                <div className="ml-auto flex gap-1">
                  <Button
                    size="sm"
                    disabled={busy !== null}
                    onClick={() => create(needProducts, 'printed')}
                  >
                    {busy === 'all' ? 'Creating…' : 'Create all as printed'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy !== null}
                    onClick={() => create(needProducts, 'stocked')}
                  >
                    As stocked
                  </Button>
                </div>
              </div>
            ) : null}

            {notice ? <Alert tone="success">{notice}</Alert> : null}

            {rows.length === 0 ? (
              <Alert tone="success">
                Every listing Etsy sells reaches a product here. Orders will match
                on arrival.
              </Alert>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase tracking-wide text-ink-500">
                    <tr>
                      <th className="py-1 pr-3 font-medium">Etsy listing</th>
                      <th className="py-1 pr-3 font-medium">Title</th>
                      <th className="py-1 pr-3 font-medium">Options</th>
                      <th className="py-1 pr-3 font-medium">In PrintFlow</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, i) => (
                      <tr key={`${row.listing_id}-${row.sku}-${i}`} className="border-t border-ink-100">
                        <td className="py-1.5 pr-3 align-top font-mono text-xs text-ink-600">
                          {row.listing_id ?? <span className="text-ink-400">—</span>}
                        </td>
                        <td className="py-1.5 pr-3 align-top">
                          <span className="text-ink-700">{row.title}</span>
                        </td>
                        <td className="py-1.5 pr-3 align-top text-xs text-ink-600">
                          {row.options.length
                            ? row.options.map((o) => `${o.name}: ${o.value}`).join(' · ')
                            : '—'}
                        </td>
                        <td className="py-1.5 pr-3 align-top">
                          <Badge className={CATALOG_STATUS[row.status].className}>
                            {CATALOG_STATUS[row.status].label}
                          </Badge>
                          {row.product_sku ? (
                            <div className="mt-0.5 text-xs text-ink-500">
                              {row.product_sku} · {row.fulfillment}
                            </div>
                          ) : null}
                        </td>
                        <td className="py-1.5 text-right align-top">
                          {(row.status === 'missing' || row.status === 'no_sku') &&
                          row.listing_id ? (
                            <div className="flex flex-col items-end gap-1">
                              <div className="flex gap-1">
                                <Button
                                  size="sm"
                                  disabled={busy !== null}
                                  onClick={() => create([row.listing_id!], 'printed')}
                                >
                                  Create printed
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={busy !== null}
                                  onClick={() => create([row.listing_id!], 'stocked')}
                                >
                                  Create stocked
                                </Button>
                              </div>
                              <select
                                className={cx(inputClass, 'text-xs')}
                                value=""
                                disabled={busy !== null}
                                onChange={(e) => link(row, e.target.value)}
                              >
                                <option value="">…or link an existing product</option>
                                {allProducts.map((product) => (
                                  <option key={product.id} value={product.id}>
                                    {product.name} ({product.sku})
                                  </option>
                                ))}
                              </select>
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {catalog.unused_products.length ? (
              <details className="text-xs text-ink-600">
                <summary className="cursor-pointer">
                  {catalog.unused_products.length} products no live listing sells
                </summary>
                <p className="mt-1">
                  Usually components of a bundle, which is expected. A finished
                  good here is worth a look — a retired listing, or a typo.
                </p>
                <ul className="mt-1 space-y-0.5">
                  {catalog.unused_products.map((product) => (
                    <li key={product.id}>
                      <span className="font-mono">{product.sku}</span> — {product.name}{' '}
                      <span className="text-ink-400">({product.fulfillment})</span>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        ) : null}

        <div className="flex gap-2">
          <Button onClick={load} disabled={catalog === null && !error}>
            Refresh from Etsy
          </Button>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </Modal>
  )
}
