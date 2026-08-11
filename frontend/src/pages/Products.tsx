import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type {
  BambuddyArchive,
  BambuddyFile,
  BambuddyFileTree,
  BambuddyPrinter,
  BambuddyPrinterModel,
  BambuddyRawListing,
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

/** The Products screen splits by what a product *is*, because the three kinds
 *  are worked on at different times: print files for one, stock levels for
 *  another, a bill of materials for the third. */
const TABS: { key: Fulfillment | 'all'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'printed', label: 'Printed items' },
  { key: 'stocked', label: 'Stocked items' },
  { key: 'bundle', label: 'Bundled items' },
]

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
  const [tab, setTab] = useState<Fulfillment | 'all'>('all')

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

  const shown = (products ?? []).filter(
    (product) => tab === 'all' || product.fulfillment === tab,
  )

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

        <div className="flex flex-wrap gap-1 text-xs">
          {TABS.map((entry) => {
            const count =
              entry.key === 'all'
                ? (products?.length ?? 0)
                : (products ?? []).filter((p) => p.fulfillment === entry.key).length
            return (
              <button
                key={entry.key}
                type="button"
                onClick={() => setTab(entry.key)}
                className={cx(
                  'rounded-md px-2.5 py-1 font-medium',
                  tab === entry.key
                    ? 'bg-ink-900 text-white'
                    : 'text-ink-600 hover:bg-ink-100',
                )}
              >
                {entry.label}
                {count ? <span className="ml-1 opacity-60">{count}</span> : null}
              </button>
            )
          })}
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}
        {notice ? <Alert tone="info">{notice}</Alert> : null}

        {!products ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : shown.length === 0 ? (
          <EmptyState
            title={
              query || tab !== 'all'
                ? 'No products match'
                : 'No products yet'
            }
            description="Products map Etsy listings to what actually gets made: a printed part, a stocked item, or a bundle with a bill of materials."
            action={
              <Button variant="primary" onClick={() => setEditing('new')}>
                Create the first product
              </Button>
            }
          />
        ) : (
          <Card className="divide-y divide-ink-200">
            {nest(shown).map(({ product, depth }) => (
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
        <PrintFilePicker
          choice={{
            printer_id: product.print_mapping?.bambuddy_printer_id ?? null,
            printer_models: product.print_mapping?.printer_models ?? [],
          }}
          onClose={() => setArchivePickerOpen(false)}
          onPick={async (picked) => {
            setArchivePickerOpen(false)
            const saved = await api.put<Product>(`/api/products/${product.id}/print-mapping`, {
              bambuddy_archive_id: picked.archive_id,
              bambuddy_archive_name: picked.name,
              bambuddy_file_path: picked.file_path,
              bambuddy_printer_id: picked.printer_id,
              plate_number: product.print_mapping?.plate_number ?? 1,
              units_per_plate: product.print_mapping?.units_per_plate ?? 1,
              print_options: product.print_mapping?.print_options ?? {},
              printer_models: picked.printer_models,
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
  const [notice, setNotice] = useState<string | null>(null)
  const [qboOpen, setQboOpen] = useState(false)

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

  /** Put a QuickBooks inventory item on the BOM.
   *
   * The materials a bundle eats are already in QuickBooks, and that is the copy
   * the stock check reads — so pick from there rather than retyping each one as
   * a product and linking it back. */
  const addFromQbo = async (item: QboItem) => {
    setQboOpen(false)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<Product & { created_product: boolean }>(
        `/api/products/${product.id}/bom/from-qbo`,
        { qbo_item_id: item.id, qbo_item_name: item.name, quantity },
      )
      await onSaved(saved)
      setQuantity(1)
      if (saved.created_product) {
        setNotice(
          `“${item.name}” was added as a stocked product, linked to that ` +
            'QuickBooks item so its stock is checked from there.',
        )
      }
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  return (
    <section className="rounded-lg bg-ink-50 p-3">
      <h3 className="text-sm font-semibold text-ink-800">Bill of materials</h3>
      <p className="mt-0.5 text-xs text-ink-500">
        One level only. Ordering this bundle creates a component line for each row.
        Materials can come straight from QuickBooks inventory — a product is made
        for anything that does not have one yet.
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
        <Button variant="ghost" onClick={() => setQboOpen(true)}>
          From QuickBooks…
        </Button>
      </div>
      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {notice ? (
        <div className="mt-2">
          <Alert tone="info">{notice}</Alert>
        </div>
      ) : null}

      {qboOpen ? (
        <QboItemPicker onClose={() => setQboOpen(false)} onPick={addFromQbo} />
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
  const [printerModels, setPrinterModels] = useState<string[]>(mapping?.printer_models ?? [])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPlate(mapping?.plate_number ?? 1)
    setUnits(mapping?.units_per_plate ?? 1)
    setOptions(JSON.stringify(mapping?.print_options ?? {}, null, 0))
    setPrinterModels(mapping?.printer_models ?? [])
  }, [mapping])

  const save = async () => {
    setError(null)
    try {
      const saved = await api.put<Product>(`/api/products/${product.id}/print-mapping`, {
        bambuddy_archive_id: mapping?.bambuddy_archive_id,
        bambuddy_archive_name: mapping?.bambuddy_archive_name,
        bambuddy_file_path: mapping?.bambuddy_file_path,
        bambuddy_printer_id: mapping?.bambuddy_printer_id,
        plate_number: plate,
        units_per_plate: units,
        print_options: options.trim() ? JSON.parse(options) : {},
        printer_models: printerModels,
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
            No file attached — orders needing this product cannot be queued.
          </p>
          <Button variant="primary" size="sm" onClick={onOpenPicker}>
            Browse Bambuddy files
          </Button>
        </div>
      ) : (
        <div className="mt-2 space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-ink-700">
              {mapping.bambuddy_archive_name ??
                mapping.bambuddy_file_path ??
                `Archive ${mapping.bambuddy_archive_id}`}
            </span>
            {mapping.bambuddy_archive_id !== null ? (
              <Badge>id {mapping.bambuddy_archive_id}</Badge>
            ) : null}
            <Button size="sm" onClick={onOpenPicker}>
              Change file
            </Button>
            <Button size="sm" variant="ghost" onClick={clear}>
              Remove mapping
            </Button>
          </div>
          {mapping.bambuddy_file_path ? (
            <p className="-mt-1 truncate font-mono text-[11px] text-ink-400">
              {mapping.bambuddy_file_path}
            </p>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
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
          </div>
          {mapping.bambuddy_printer_id !== null ? (
            <p className="text-xs text-ink-500">
              This file lives on printer {mapping.bambuddy_printer_id}, so that is
              where the plate goes. Change the file to print it elsewhere.
            </p>
          ) : (
            <PrinterModelPicker value={printerModels} onChange={setPrinterModels} />
          )}
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

/** Which kinds of machine can take this plate. Nothing ticked means any of them.
 *
 * The farm runs more than one model and a plate that fits one often fits
 * another, so this is a set rather than a single printer: dispatch picks a free
 * machine of any ticked model. Models the farm no longer reports still appear,
 * ticked — dropping one silently would quietly widen the job to everything.
 */
function PrinterModelPicker({
  value,
  onChange,
}: {
  value: string[]
  onChange: (models: string[]) => void
}) {
  const [models, setModels] = useState<BambuddyPrinterModel[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)

  useEffect(() => {
    api
      .get<{ models: BambuddyPrinterModel[] }>('/api/integrations/bambuddy/printer-models')
      .then((data) => {
        setModels(data.models)
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }, [])

  const has = (model: string) =>
    value.some((chosen) => chosen.toLowerCase() === model.toLowerCase())

  const rows: BambuddyPrinterModel[] = [
    ...models,
    ...value
      .filter((chosen) => !models.some((m) => m.model.toLowerCase() === chosen.toLowerCase()))
      .map((model) => ({ model, printers: 0, online: 0, names: [] })),
  ]

  const toggle = (model: string) =>
    onChange(
      has(model)
        ? value.filter((chosen) => chosen.toLowerCase() !== model.toLowerCase())
        : [...value, model],
    )

  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-medium text-ink-700">Printer models</span>
        <span className="text-xs text-ink-500">
          {value.length ? 'Any free machine of these.' : 'Nothing ticked — any printer.'}
        </span>
      </div>
      {error ? (
        <div className="mt-1">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      {busy ? <Spinner /> : null}
      {!busy && rows.length === 0 ? (
        <p className="mt-1 text-xs text-ink-500">
          Bambuddy reported no printers, so there are no models to choose from.
        </p>
      ) : null}
      <div className="mt-1 flex flex-wrap gap-1.5">
        {rows.map((row) => (
          <label
            key={row.model}
            className={cx(
              'flex cursor-pointer items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
              has(row.model)
                ? 'border-sky-400 bg-sky-50 text-sky-900'
                : 'border-ink-200 text-ink-700 hover:bg-ink-50',
            )}
          >
            <input
              type="checkbox"
              className="h-3.5 w-3.5"
              checked={has(row.model)}
              onChange={() => toggle(row.model)}
            />
            <span>{row.model}</span>
            <span className="text-ink-400">
              {row.printers ? `${row.online}/${row.printers} online` : 'none on the farm'}
            </span>
          </label>
        ))}
      </div>
    </div>
  )
}

/** What picking a print file settles: which file, and where it prints. */
export interface FileChoice {
  archive_id: number | null
  file_path: string | null
  name: string
  /** Set when the file came off one machine — that machine gets the plate. */
  printer_id: number | null
  printer_models: string[]
}

/** Where the file is being looked for. */
type FileSource =
  | { kind: 'library' }
  | { kind: 'files' }
  | { kind: 'printer'; id: number; label: string }

const sourceKey = (source: FileSource) =>
  source.kind === 'printer' ? `printer:${source.id}` : source.kind

function folderOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut <= 0 ? '/' : path.slice(0, cut)
}

/** Children by folder, with the files nobody can print left out.
 *
 * Folders sort above files at every level, the way a file manager draws them —
 * otherwise a nested folder hides in the middle of its siblings' filenames. */
function byFolder(nodes: BambuddyFile[]): Map<string, BambuddyFile[]> {
  const map = new Map<string, BambuddyFile[]>()
  for (const node of nodes) {
    if (node.kind === 'file' && !node.printable) continue
    const siblings = map.get(node.parent)
    if (siblings) siblings.push(node)
    else map.set(node.parent, [node])
  }
  for (const siblings of map.values()) {
    siblings.sort(
      (a, b) =>
        Number(a.kind !== 'folder') - Number(b.kind !== 'folder') ||
        a.name.toLowerCase().localeCompare(b.name.toLowerCase()),
    )
  }
  return map
}

/** The rows a tree shows right now: every child of every open folder, in order.
 *
 * Flattened rather than drawn recursively so that one scroll container holds the
 * whole structure and each row is a plain button — indentation is the only thing
 * that says how deep it sits.
 */
function openRows(
  children: Map<string, BambuddyFile[]>,
  open: Set<string>,
  parent = '/',
  depth = 0,
): { file: BambuddyFile; depth: number }[] {
  const rows: { file: BambuddyFile; depth: number }[] = []
  for (const file of children.get(parent) ?? []) {
    rows.push({ file, depth })
    if (file.kind === 'folder' && open.has(file.path)) {
      rows.push(...openRows(children, open, file.path, depth + 1))
    }
  }
  return rows
}

function humanSize(bytes: number | null): string | null {
  if (bytes === null || bytes === undefined || bytes < 0) return null
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

/** Bambuddy's file manager, folders and all, plus the flat archive library.
 *
 * Two things are being chosen here and they are not independent, which is why
 * they share a screen: *which printer* and *which file*. Pick a machine and you
 * are browsing that machine's file manager, and the plate can only go there —
 * a file on one printer's storage does not exist on another. Pick the library
 * or the shared file manager and any capable machine will do, so the printer
 * models come back as the thing to narrow instead.
 *
 * The file manager is where this opens, showing the whole structure Bambuddy
 * has — folders closed, so what you see first is the shape of the library
 * rather than every file in it. A shop that has sorted its files into folders
 * reaches for the folder it wants; Expand all is there for the times it does
 * not. A picker that makes you type before it shows you anything is a picker
 * for people who already know the answer, so searching is still there and still
 * cuts across the whole tree, but nothing depends on it.
 */
function PrintFilePicker({
  choice,
  onClose,
  onPick,
}: {
  choice: Pick<FileChoice, 'printer_id' | 'printer_models'>
  onClose: () => void
  onPick: (picked: FileChoice) => void | Promise<void>
}) {
  const [printers, setPrinters] = useState<BambuddyPrinter[]>([])
  const [source, setSource] = useState<FileSource>(
    choice.printer_id !== null
      ? { kind: 'printer', id: choice.printer_id, label: `Printer ${choice.printer_id}` }
      : { kind: 'files' },
  )
  // True until the operator picks a source themselves. Only an automatic choice
  // is allowed to fall back on its own; an explicit one gets the error.
  const [autoSource, setAutoSource] = useState(choice.printer_id === null)
  const [fellBack, setFellBack] = useState(false)
  const [printerModels, setPrinterModels] = useState<string[]>(choice.printer_models)
  const [query, setQuery] = useState('')
  const [openFolders, setOpenFolders] = useState<Set<string>>(new Set())

  const [archives, setArchives] = useState<BambuddyArchive[]>([])
  const [archivesTruncated, setArchivesTruncated] = useState(false)
  const [tree, setTree] = useState<BambuddyFileTree | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)

  useEffect(() => {
    api
      .get<{ printers: BambuddyPrinter[] }>('/api/integrations/bambuddy/printers')
      .then((data) => setPrinters(data.printers.filter((p) => p.id !== null)))
      .catch(() => undefined)
  }, [])

  // One load per source. Switching back to one already read still refetches,
  // which costs a call and buys a file manager that is not stale by an hour.
  const key = sourceKey(source)
  useEffect(() => {
    let live = true
    setBusy(true)
    setError(null)
    setOpenFolders(new Set())
    setTree(null)
    const request =
      source.kind === 'library'
        ? api
            .get<{ archives: BambuddyArchive[]; truncated: boolean }>(
              '/api/integrations/bambuddy/archives',
            )
            .then((data) => {
              if (!live) return
              setArchives(data.archives.filter((a) => a.id !== null))
              setArchivesTruncated(data.truncated)
            })
        : api
            .get<BambuddyFileTree>(
              '/api/integrations/bambuddy/files' +
                (source.kind === 'printer' ? `?printer_id=${source.id}` : ''),
            )
            .then((data) => {
              if (!live) return
              setTree(data)
              // Closed. The top level is the shape of the library, and a shop
              // that has sorted its files into folders reaches for the folder
              // it wants — not for a wall of every file it owns. Expand all is
              // one click for the times you do want the lot.
              setOpenFolders(new Set())
            })
    request
      .catch((err) => {
        if (!live) return
        // The file manager is where this opens, and not every Bambuddy has one.
        // Falling back beats greeting everyone with an error they did not ask
        // for — but say what happened, because it is still a real difference.
        if (autoSource && source.kind === 'files') {
          setSource({ kind: 'library' })
          setFellBack(true)
          return
        }
        setError(errorMessage(err))
      })
      .finally(() => {
        if (live) setBusy(false)
      })
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const pickSource = (value: string) => {
    setQuery('')
    setAutoSource(false)
    setFellBack(false)
    if (value === 'library') return setSource({ kind: 'library' })
    if (value === 'files') return setSource({ kind: 'files' })
    const printer = printers.find((p) => String(p.id) === value)
    if (printer)
      setSource({
        kind: 'printer',
        id: Number(printer.id),
        label: printer.name ?? `Printer ${printer.id}`,
      })
  }

  const toggleFolder = (path: string) =>
    setOpenFolders((prev) => {
      const next = new Set(prev)
      if (!next.delete(path)) next.add(path)
      return next
    })

  const needle = query.trim().toLowerCase()
  const printerId = source.kind === 'printer' ? source.id : null

  const take = (file: BambuddyFile) =>
    onPick({
      archive_id: file.archive_id,
      file_path: file.path,
      name: file.name,
      printer_id: printerId,
      printer_models: printerId === null ? printerModels : [],
    })

  const nodes = tree?.files ?? []
  const children = byFolder(nodes)
  const rows = openRows(children, openFolders)
  const matches = needle
    ? nodes.filter((file) => file.printable && file.path.toLowerCase().includes(needle))
    : []
  const hidden = nodes.filter((file) => file.kind === 'file' && !file.printable).length
  const folders = nodes.filter((file) => file.kind === 'folder')
  const allOpen = folders.length > 0 && folders.every((file) => openFolders.has(file.path))

  return (
    <Modal open title="Pick a print file" onClose={onClose} wide>
      <Field
        label="Where the file is"
        hint={
          printerId === null
            ? 'Shared across the farm, so any capable machine can take the plate.'
            : tree?.shared
              ? 'The plate goes to this printer.'
              : "This machine's own files — the plate goes to this printer."
        }
      >
        <select
          className={inputClass}
          value={source.kind === 'printer' ? String(source.id) : source.kind}
          onChange={(e) => pickSource(e.target.value)}
        >
          <option value="files">File manager (all printers)</option>
          {printers.length ? (
            <optgroup label="One printer's files">
              {printers.map((printer) => (
                <option key={printer.id} value={String(printer.id)}>
                  {printer.name ?? `Printer ${printer.id}`}
                  {printer.model ? ` — ${printer.model}` : ''}
                  {printer.online ? '' : ' (offline)'}
                </option>
              ))}
            </optgroup>
          ) : null}
          <option value="library">Bambuddy library (flat archive list)</option>
        </select>
      </Field>

      {fellBack ? (
        <div className="mt-3">
          <Alert tone="info">
            This Bambuddy has no file manager PrintFlow can read, so this is the
            archive list instead. Naming the endpoint under Settings → Bambuddy →
            Advanced brings the folders back.
          </Alert>
        </div>
      ) : null}

      {printerId === null ? (
        <div className="mt-3">
          <PrinterModelPicker value={printerModels} onChange={setPrinterModels} />
        </div>
      ) : null}

      <input
        className={cx(inputClass, 'mt-3')}
        placeholder={
          source.kind === 'library'
            ? 'Search archived 3MF files…'
            : 'Optional: search every folder at once…'
        }
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {error ? (
        <div className="mt-3 space-y-2">
          <Alert tone="error">{error}</Alert>
          {source.kind !== 'library' ? (
            // Not every Bambuddy has a file manager, and a product still has to
            // get a print file today. The library is always there.
            <Button size="sm" onClick={() => setSource({ kind: 'library' })}>
              Use the Bambuddy library instead
            </Button>
          ) : null}
        </div>
      ) : null}

      {source.kind === 'library' ? (
        <ArchiveList
          archives={archives}
          truncated={archivesTruncated}
          busy={busy}
          needle={needle}
          onPick={(archive) =>
            onPick({
              archive_id: archive.id,
              file_path: null,
              name: archive.name ?? `Archive ${archive.id}`,
              printer_id: null,
              printer_models: printerModels,
            })
          }
        />
      ) : (
        <>
          {tree?.truncated ? (
            <div className="mt-3">
              <Alert tone="warning">
                This file manager is bigger than PrintFlow walks in one go, so some
                folders are missing. Narrow it in Bambuddy, or pick from the library.
              </Alert>
            </div>
          ) : null}

          {tree?.flat ? (
            <div className="mt-3">
              <Alert tone="warning">
                This Bambuddy answered every folder request with its top level, so
                only the top level is here and its folders would not open. The files
                below are real; anything inside a folder is not reachable this way.
              </Alert>
            </div>
          ) : null}

          {tree?.shared && printerId !== null ? (
            <div className="mt-3">
              <Alert tone="info">
                This Bambuddy keeps one library for the whole farm rather than files
                per machine, so these are the shared files. The plate still goes to{' '}
                {source.kind === 'printer' ? source.label : 'this printer'}.
              </Alert>
            </div>
          ) : null}

          {!needle && folders.length ? (
            <div className="mt-3 flex items-center gap-2 text-xs text-ink-500">
              <span>
                {source.kind === 'printer' ? source.label : 'File manager'} — the whole
                structure, as Bambuddy has it.
              </span>
              <Button
                size="sm"
                variant="ghost"
                className="ml-auto"
                onClick={() =>
                  setOpenFolders(allOpen ? new Set() : new Set(folders.map((f) => f.path)))
                }
              >
                {allOpen ? 'Collapse all' : 'Expand all'}
              </Button>
            </div>
          ) : null}

          <div className="mt-2 max-h-[26rem] space-y-0.5 overflow-y-auto">
            {busy ? <Spinner /> : null}
            {!busy && needle && matches.length === 0 ? (
              <p className="py-4 text-center text-sm text-ink-500">
                No print file anywhere in here matches that.
              </p>
            ) : null}
            {/* Folders with nothing printable in them is as much a dead end as
                no folders at all, and needs the same explanation. */}
            {!busy && !needle && tree && tree.printable === 0 ? (
              <EmptyFileManager tree={tree} printerId={printerId} />
            ) : null}

            {needle
              ? matches.map((file) => (
                  <button
                    key={file.path}
                    type="button"
                    onClick={() => take(file)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left hover:bg-ink-50"
                  >
                    <span className="text-ink-400">▤</span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-ink-800">{file.name}</span>
                      <span className="block truncate font-mono text-[11px] text-ink-400">
                        {folderOf(file.path)}
                      </span>
                    </span>
                    {humanSize(file.size) ? <Badge>{humanSize(file.size)}</Badge> : null}
                  </button>
                ))
              : rows.map(({ file, depth }) =>
                  file.kind === 'folder' ? (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => toggleFolder(file.path)}
                      style={{ paddingLeft: 8 + depth * 18 }}
                      className="flex w-full items-center gap-2 rounded-md py-1.5 pr-2 text-left hover:bg-ink-50"
                    >
                      <span className="w-3 text-ink-400">
                        {openFolders.has(file.path) ? '▾' : '▸'}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-ink-800">
                        {file.name}
                      </span>
                      {file.unreadable ? (
                        <Badge className="bg-amber-100 text-amber-800 ring-amber-300">
                          could not be read
                        </Badge>
                      ) : (
                        <Badge>{(children.get(file.path) ?? []).length} items</Badge>
                      )}
                    </button>
                  ) : (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => take(file)}
                      style={{ paddingLeft: 8 + depth * 18 }}
                      className="flex w-full items-center gap-2 rounded-md py-1.5 pr-2 text-left hover:bg-ink-50"
                    >
                      <span className="w-3 text-ink-400">▤</span>
                      <span className="min-w-0 flex-1 truncate text-sm text-ink-800">
                        {file.name}
                      </span>
                      {humanSize(file.size) ? <Badge>{humanSize(file.size)}</Badge> : null}
                    </button>
                  ),
                )}
          </div>

          {!busy && tree ? (
            <div className="mt-2 flex flex-wrap items-baseline gap-x-2">
              <p className="min-w-48 flex-1 text-xs text-ink-500">
                {tree.printable} print file{tree.printable === 1 ? '' : 's'} in{' '}
                {tree.folders} folder{tree.folders === 1 ? '' : 's'}.
                {hidden
                  ? ` ${hidden} other file${hidden === 1 ? ' is' : 's are'} not something a printer takes.`
                  : ''}
              </p>
              {/* Always here, not only when the tree is visibly empty: a tree
                  that is subtly wrong needs the replies just as much, and the
                  operator is the only one who can see them. */}
              {tree.printable > 0 ? <WhatBambuddySent printerId={printerId} /> : null}
            </div>
          ) : null}
        </>
      )}
    </Modal>
  )
}

/** Why the file manager is empty — and, failing that, what Bambuddy actually sent.
 *
 * "No files" has three quite different causes and they are indistinguishable
 * from a blank list: the folder really is empty, the reply held rows in a shape
 * PrintFlow could not read, or the endpoint answers but is not the file manager
 * at all. Every instance is self-hosted and none are quite the same, so the
 * third one cannot be diagnosed from here — but it can be shown, which turns a
 * dead end into something somebody can act on.
 */
function EmptyFileManager({
  tree,
  printerId,
}: {
  tree: BambuddyFileTree
  printerId: number | null
}) {
  const unreadable = tree.root_rows > tree.root_named
  const foldersOnly = tree.folders > 0 && tree.printable === 0

  return (
    <div className="space-y-2 py-3 text-sm text-ink-600">
      <p>
        {foldersOnly
          ? `Bambuddy listed ${tree.folders} folder${tree.folders === 1 ? '' : 's'} but no ` +
            'print files in any of them.'
          : unreadable
            ? `Bambuddy sent ${tree.root_rows} entr${tree.root_rows === 1 ? 'y' : 'ies'}, ` +
              'but PrintFlow could not read any of them as a file or a folder.'
            : 'Bambuddy listed nothing here.'}
      </p>
      <p className="text-xs text-ink-500">
        Read from <span className="font-mono">{tree.endpoint ?? 'the file manager'}</span>.
        {foldersOnly
          ? ' The folders arrived, so the address is right and it is the file list' +
            ' that came back empty.'
          : unreadable
            ? ' The endpoint answers but its reply is a shape PrintFlow has not seen.'
            : ' If that is not your file manager, set the right endpoint under Settings →' +
              ' Bambuddy → Advanced.'}
      </p>
      <WhatBambuddySent printerId={printerId} />
    </div>
  )
}

/** The replies behind the tree, verbatim.
 *
 * Always reachable, not only when something is visibly broken. Every one of
 * these instances is self-hosted and none of them are quite the same shape, so
 * a tree that is subtly wrong — a folder in the wrong place, files missing from
 * one branch — cannot be diagnosed from the outside at all. It can be shown,
 * and that turns a round of guessing into one screenshot.
 */
function WhatBambuddySent({ printerId }: { printerId: number | null }) {
  const [raw, setRaw] = useState<BambuddyRawListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const show = () => {
    setBusy(true)
    api
      .get<BambuddyRawListing>(
        '/api/integrations/bambuddy/files/raw' +
          (printerId !== null ? `?printer_id=${printerId}` : ''),
      )
      .then((data) => {
        setRaw(data)
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }

  return (
    <div className="space-y-2">
      {!raw ? (
        <Button size="sm" variant="ghost" onClick={show} disabled={busy}>
          {busy ? 'Asking…' : 'Show what Bambuddy sent'}
        </Button>
      ) : (
        <Button size="sm" variant="ghost" onClick={() => setRaw(null)}>
          Hide what Bambuddy sent
        </Button>
      )}
      {error ? <Alert tone="error">{error}</Alert> : null}
      {raw?.probes.map((probe) => (
        <div key={probe.endpoint + (probe.body ?? '')} className="space-y-1">
          <p className="text-xs text-ink-500">
            <span className="font-mono">{probe.endpoint}</span>
            {probe.error
              ? ` failed: ${probe.error}`
              : ` returned ${probe.kind}` +
                (probe.keys?.length ? ` with keys: ${probe.keys.join(', ')}` : '') +
                ` — ${probe.rows_found} row${probe.rows_found === 1 ? '' : 's'} read` +
                (probe.total !== null && probe.total !== undefined
                  ? ` of ${probe.total}`
                  : '') +
                '.'}
          </p>
          {probe.body ? (
            <pre className="max-h-48 overflow-auto rounded-md bg-ink-900 p-2 text-[11px] leading-snug text-ink-100">
              {probe.body}
              {probe.body_truncated ? '\n… (truncated)' : ''}
            </pre>
          ) : null}
        </div>
      ))}
    </div>
  )
}

/** The flat archive library — the same list Bambuddy's own archives page shows. */
function ArchiveList({
  archives,
  truncated,
  busy,
  needle,
  onPick,
}: {
  archives: BambuddyArchive[]
  truncated: boolean
  busy: boolean
  needle: string
  onPick: (archive: BambuddyArchive) => void
}) {
  const shown = needle
    ? archives.filter((archive) =>
        `${archive.name ?? ''} ${archive.id ?? ''}`.toLowerCase().includes(needle),
      )
    : archives

  return (
    <>
      {truncated ? (
        <div className="mt-3">
          <Alert tone="warning">
            Bambuddy has more archives than PrintFlow reads in one go — search for the
            file by name if it is not in this list.
          </Alert>
        </div>
      ) : null}
      <div className="mt-3 max-h-80 space-y-0.5 overflow-y-auto">
        {busy ? <Spinner /> : null}
        {!busy && shown.length === 0 ? (
          <p className="py-4 text-center text-sm text-ink-500">
            {archives.length ? 'Nothing matches that.' : 'No archives found.'}
          </p>
        ) : null}
        {shown.map((archive) => (
          <button
            key={String(archive.id)}
            type="button"
            onClick={() => onPick(archive)}
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
      {!busy && archives.length ? (
        <p className="mt-2 text-xs text-ink-500">
          {shown.length === archives.length
            ? `${archives.length} archive${archives.length === 1 ? '' : 's'} on Bambuddy.`
            : `${shown.length} of ${archives.length} archives.`}
        </p>
      ) : null}
    </>
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
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [qboOpen, setQboOpen] = useState(false)

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

  /** Bring in a QuickBooks item for this option.
   *
   * A variation exists because it needs something the base build does not — the
   * fan, the bigger magnet, the second colour. That thing is by definition not
   * on the BOM, so a list of what is already there is the wrong list. */
  const addFromQbo = async (item: QboItem) => {
    setQboOpen(false)
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<Product & { created_product: boolean }>(
        `/api/products/${product.id}/option-rules/from-qbo`,
        {
          option_name: optionName.trim(),
          option_value: optionValue.trim(),
          replaces_id: replacesId || null,
          quantity: quantity ? Number(quantity) : null,
          qbo_item_id: item.id,
          qbo_item_name: item.name,
        },
      )
      await onSaved(saved)
      setOptionValue('')
      setComponentId('')
      setQuantity('')
      if (saved.created_product) {
        setNotice(
          `“${item.name}” was added as a stocked product, linked to that ` +
            'QuickBooks item so its stock is checked from there.',
        )
      }
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
        <Field
          label="Use this component"
          hint="Or take it straight from QuickBooks, below — a variation usually needs something the BOM does not have."
        >
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
      {notice ? <Alert tone="info">{notice}</Alert> : null}
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={add}
          disabled={busy || !optionName.trim() || !optionValue.trim() || !componentId}
        >
          {busy ? 'Adding…' : 'Add rule'}
        </Button>
        <Button
          variant="ghost"
          onClick={() => setQboOpen(true)}
          disabled={busy || !optionName.trim() || !optionValue.trim()}
        >
          Add from QuickBooks…
        </Button>
      </div>

      {qboOpen ? (
        <QboItemPicker onClose={() => setQboOpen(false)} onPick={addFromQbo} />
      ) : null}
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
            bambuddy_file_path: body.bambuddy_file_path,
            bambuddy_printer_id: body.bambuddy_printer_id,
            plate_number: body.plate_number,
            units_per_plate: body.units_per_plate,
            printer_models: body.printer_models ?? [],
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
                  {variation.bambuddy_archive_id || variation.bambuddy_file_path ? (
                    <>
                      <span className="text-ink-700">
                        {variation.bambuddy_archive_name ??
                          variation.bambuddy_file_path ??
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
                            bambuddy_file_path: null,
                            bambuddy_printer_id: null,
                            plate_number: null,
                            units_per_plate: null,
                            printer_models: [],
                          })
                        }
                      >
                        Use the product's file
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => setPicking(variation)}
                      >
                        Change
                      </Button>
                      <span className="basis-full">
                        {variation.bambuddy_printer_id !== null
                          ? `On: printer ${variation.bambuddy_printer_id}, where the file lives`
                          : variation.printer_models?.length
                            ? `On: ${variation.printer_models.join(', ')}`
                            : "On: whatever the product's mapping says"}
                      </span>
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
        <PrintFilePicker
          choice={{
            printer_id: picking.bambuddy_printer_id,
            printer_models: picking.printer_models ?? [],
          }}
          onClose={() => setPicking(null)}
          onPick={async (picked) => {
            const variation = picking
            setPicking(null)
            await save(variation, {
              bambuddy_archive_id: picked.archive_id,
              bambuddy_archive_name: picked.name,
              bambuddy_file_path: picked.file_path,
              bambuddy_printer_id: picked.printer_id,
              plate_number: variation.plate_number ?? 1,
              units_per_plate: variation.units_per_plate ?? 1,
              printer_models: picked.printer_models,
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
