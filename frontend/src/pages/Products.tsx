import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type {
  BomEntry,
  Catalog,
  CatalogRow,
  Fulfillment,
  ObservedOption,
  PrintFile,
  Product,
  ProductVariation,
  QboItem,
  WixCatalog,
  WixCatalogItem,
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
import PrintFilePicker, {
  PrinterModelPicker,
  type FileChoice,
} from '../components/PrintFilePicker'

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

/** What the QuickBooks item actually does, which depends on what this is.
 *
 * It has two jobs and they land on different products. On anything with stock
 * it is where quantity on hand is read and where printed units are taken back
 * out. On every product it is what an invoice line names — which is why a
 * bundle wants one too, even though a bundle has no stock of its own. */
const QBO_ITEM_HINTS: Record<Fulfillment, string> = {
  printed:
    'Quantity on hand is read here, printed units are taken back out of it, and an invoice line for this product names it. With no item linked this always prints in full and moves nothing in QuickBooks.',
  stocked:
    'Quantity on hand is read here, and an invoice line for this product names it.',
  bundle:
    'A bundle has no stock of its own — decisioning and stock removal go through its components — so this is only what an invoice line for the bundle names. Assembling to stock through a made-items sheet? Use the assembled inventory item. Assembling to order? Use a Service item: the components were already taken out of stock when they were printed, and an inventory item here would take the assembled thing out as well.',
}

/** Is this something the shop sells, rather than something it makes?
 *
 * A product linked to an Etsy listing arrives as a top-level order line, and
 * top-level lines are what an invoice bills. Components reached through a
 * bundle's BOM are never invoiced on their own — the buyer bought the bundle —
 * so a component with no QuickBooks item is ordinary rather than a gap.
 */
function sellable(product: Product): boolean {
  return product.etsy_links.length > 0
}

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
  const [checkingWix, setCheckingWix] = useState(false)
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
          <Button onClick={() => setCheckingWix(true)}>Check against Wix</Button>
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
                      product.print_files.length
                        ? 'bg-emerald-100 text-emerald-800 ring-emerald-300'
                        : 'bg-red-100 text-red-800 ring-red-300'
                    }
                  >
                    {product.print_files.length
                      ? `${product.print_files.length} file${product.print_files.length === 1 ? '' : 's'}`
                      : 'no print file'}
                  </Badge>
                ) : null}
                {product.qbo_item_id ? <Badge>QBO linked</Badge> : null}
                {/* Items chosen by what the buyer picked count every bit as
                    much as one on the product — they are how a listing sold in
                    several scales is billed at all. Shown alongside a product
                    item rather than instead of it: the product's is what an
                    option nobody mapped falls back to. */}
                {product.option_items.length ? (
                  <Badge
                    className="bg-sky-100 text-sky-800 ring-sky-300"
                    title={product.option_items
                      .map(
                        (row) =>
                          `${row.options
                            .map((o) => `${o.name}: ${o.value}`)
                            .join(' and ')} → ${row.qbo_item_name ?? row.qbo_item_id}`,
                      )
                      .join('\n')}
                  >
                    {product.option_items.length} by option
                  </Badge>
                ) : null}
                {/* Only when there is no item anywhere, and only on something
                    the shop actually sells. A component with none is ordinary —
                    nothing invoices it — but a listing with none is an invoice
                    that cannot be raised until a fallback is set, and finding
                    that out at the moment of invoicing is finding out too late. */}
                {!product.qbo_item_id &&
                !product.option_items.length &&
                sellable(product) ? (
                  <Badge
                    className="bg-amber-100 text-amber-800 ring-amber-300"
                    title="An invoice line for this product has no item to name. It will fall back to the item set under Settings → QuickBooks, or block the invoice if there is none."
                  >
                    no QuickBooks item
                  </Badge>
                ) : null}
                {!product.active ? <Badge>inactive</Badge> : null}
                </button>
              </div>
            ))}
          </Card>
        )}
      </div>

      {checkingWix ? (
        <WixCatalogCheck onClose={() => setCheckingWix(false)} onChanged={load} />
      ) : null}

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

        {/* Offered for every fulfillment, bundles included. The item used to
            be hidden on a bundle because a bundle has no quantity on hand of
            its own — decisioning goes through its components — which was the
            whole job the field did. It has a second job now: an invoice line
            for this product names it, and hiding the field left "link the
            product to an item" as advice nobody could act on. */}
        <Field label="QuickBooks item" hint={QBO_ITEM_HINTS[fulfillment]}>
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
        {/* Only where it changes what to pick. A shop that assembles to stock
            through a made-items sheet wants the assembled item here; one that
            assembles to order wants a service item, because its components
            were already taken out of stock when they were printed. Getting
            this the wrong way round shows up as a playset going negative. */}
        {/* Only where it is still true. A product billed by option has its
            items already — this one is what an option nobody mapped falls back
            to, which is a different and much smaller worry. */}
        {fulfillment === 'bundle' && !qboItem && !product?.option_items.length ? (
          <p className="-mt-1 text-xs text-ink-500">
            Without one, an invoice for this bundle falls back to the item set
            under Settings → QuickBooks → Orders in the books, and will not be
            raised at all if there is no fallback either.
          </p>
        ) : null}
        {fulfillment === 'bundle' && !qboItem && product?.option_items.length ? (
          <p className="-mt-1 text-xs text-ink-500">
            Not needed while every option a buyer can pick is mapped below — this
            is only what an unmapped one would fall back to.
          </p>
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
          <PrintFilesEditor
            product={product}
            onSaved={onSaved}
            onAddFile={() => setArchivePickerOpen(true)}
          />
        ) : null}

        {/* Every fulfillment, because every product is invoiced. */}
        {product ? <OptionItemsEditor product={product} onSaved={onSaved} /> : null}

        {product ? <EtsyLinksEditor product={product} onSaved={onSaved} /> : null}
        {product ? <WixLinksEditor product={product} onSaved={onSaved} /> : null}

        {/* Bundles included. This used to be hidden for them, on the grounds
            that a bundle's options change its BOM — which is what option rules
            above are for — and a variation's other jobs, picking a print file
            and carrying its own stock, are things a bundle does not do.
            A variation has a third job now: naming the invoice line. That one
            very much applies to a bundle. One listing sold in HO and 1:64 is
            two things in QuickBooks, and there was no way to say so. */}
        {product ? <VariationsEditor product={product} onSaved={onSaved} /> : null}

        {!product ? (
          <Alert tone="info">
            Save the product first — the BOM editor and Bambuddy files appear afterwards.
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
          // A new file starts from nothing rather than from the last one: it is
          // there because this product prints somewhere else too.
          choice={{ printer_id: null, printer_models: [] }}
          onClose={() => setArchivePickerOpen(false)}
          onPick={async (picked: FileChoice) => {
            setArchivePickerOpen(false)
            const saved = await api.post<Product>(
              `/api/products/${product.id}/print-files`,
              {
                bambuddy_archive_id: picked.archive_id,
                bambuddy_archive_name: picked.name,
                bambuddy_file_path: picked.file_path,
                bambuddy_printer_id: picked.printer_id,
                plate_number: 1,
                units_per_plate: 1,
                print_options: {},
                printer_models: picked.printer_models,
              },
            )
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

/** Every way this product can be printed, and which machines each one is for.
 *
 * A farm whose library is sorted by printer model has the same part sliced once
 * per machine. They are alternatives, not a sequence: a plate uses whichever
 * one names a printer that is free, decided when it is actually sent. So this
 * is a list, and each row answers the same two questions — which file, and
 * which machines will take it.
 */
function PrintFilesEditor({
  product,
  onSaved,
  onAddFile,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
  onAddFile: () => void
}) {
  const files = product.print_files

  // What the files add up to. Each row says where it alone can go; between them
  // they say what the product can be made on, which is the thing an operator
  // actually wants to know and is nowhere on a single row.
  const machines = (() => {
    if (!files.length) return null
    if (files.some((file) => file.bambuddy_printer_id === null && !file.printer_models.length))
      return 'any printer'
    const named = new Set<string>()
    for (const file of files) {
      if (file.bambuddy_printer_id !== null) named.add(`printer ${file.bambuddy_printer_id}`)
      for (const model of file.printer_models) named.add(model)
    }
    return [...named].join(', ')
  })()

  return (
    <section className="rounded-lg bg-ink-50 p-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-48 flex-1">
          <h3 className="text-sm font-semibold text-ink-800">Bambuddy print files</h3>
          <p className="text-xs text-ink-500">
            One per way this product can be made. A plate goes to whichever
            machine is free and has a file for it.
          </p>
          {machines ? (
            <p className="mt-1 text-xs text-ink-600">
              Can be printed on: <span className="font-medium">{machines}</span>
            </p>
          ) : null}
        </div>
        <Button size="sm" variant={files.length ? undefined : 'primary'} onClick={onAddFile}>
          {files.length ? 'Add another file' : 'Browse Bambuddy files'}
        </Button>
      </div>

      {!files.length ? (
        <p className="mt-2 text-sm text-red-800">
          No file attached — orders needing this product cannot be queued.
        </p>
      ) : (
        <ul className="mt-2 space-y-2">
          {files.map((file) => (
            <li key={file.id} className="rounded-md border border-ink-200 bg-white p-2">
              <PrintFileRow product={product} file={file} onSaved={onSaved} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function PrintFileRow({
  product,
  file,
  onSaved,
}: {
  product: Product
  file: PrintFile
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [plate, setPlate] = useState(file.plate_number)
  const [units, setUnits] = useState(file.units_per_plate)
  const [options, setOptions] = useState(JSON.stringify(file.print_options ?? {}, null, 0))
  const [printerModels, setPrinterModels] = useState<string[]>(file.printer_models)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    setPlate(file.plate_number)
    setUnits(file.units_per_plate)
    setOptions(JSON.stringify(file.print_options ?? {}, null, 0))
    setPrinterModels(file.printer_models)
  }, [file])

  const save = async () => {
    setError(null)
    try {
      await onSaved(
        await api.put<Product>(`/api/products/${product.id}/print-files/${file.id}`, {
          bambuddy_archive_id: file.bambuddy_archive_id,
          bambuddy_archive_name: file.bambuddy_archive_name,
          bambuddy_file_path: file.bambuddy_file_path,
          bambuddy_printer_id: file.bambuddy_printer_id,
          plate_number: plate,
          units_per_plate: units,
          print_options: options.trim() ? JSON.parse(options) : {},
          printer_models: printerModels,
        }),
      )
      setOpen(false)
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  const remove = async () =>
    onSaved(
      await api.del<Product>(`/api/products/${product.id}/print-files/${file.id}`),
    )

  const goesTo =
    file.bambuddy_printer_id !== null
      ? `printer ${file.bambuddy_printer_id}`
      : file.printer_models.length
        ? file.printer_models.join(', ')
        : 'any printer'

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="min-w-0 flex-1 truncate text-ink-800">
          {file.bambuddy_archive_name ??
            file.bambuddy_file_path ??
            `Archive ${file.bambuddy_archive_id}`}
        </span>
        <Badge>{goesTo}</Badge>
        <Button size="sm" variant="ghost" onClick={() => setOpen(!open)}>
          {open ? 'Done' : 'Edit'}
        </Button>
        <Button size="sm" variant="ghost" onClick={remove}>
          Remove
        </Button>
      </div>
      {file.bambuddy_file_path ? (
        <p className="truncate font-mono text-[11px] text-ink-400">
          {file.bambuddy_file_path}
        </p>
      ) : null}

      {open ? (
        <div className="mt-2 space-y-3 border-t border-ink-200 pt-2">
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
          {file.bambuddy_printer_id !== null ? (
            <p className="text-xs text-ink-500">
              This file lives on printer {file.bambuddy_printer_id}, so that is where
              the plate goes. Attach it again from elsewhere to print it elsewhere.
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
            Save this file
          </Button>
        </div>
      ) : null}
    </>
  )
}

/** Add a variation by hand, for what Etsy will not hand over.
 *
 * Pulling from Etsy stays the right way round and the default — Etsy states
 * exactly which combinations it sells, and a typo in an option name is silent.
 * But it only works for a listing whose options Etsy models as inventory. One
 * that names its scales in the title, or offers them made-to-order, has no
 * combinations to read, and until now that left the product with no variations
 * at all and nowhere to put a QuickBooks item.
 *
 * Orders match on the option values when there is no Etsy id to match on, so a
 * variation typed here picks up orders exactly as a pulled one does — provided
 * the wording matches. Which is why the form says so rather than hoping.
 */
function NewVariationForm({
  product,
  onSaved,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
}) {
  const blank = [{ name: '', value: '' }]
  const [open, setOpen] = useState(false)
  const [label, setLabel] = useState('')
  const [options, setOptions] = useState(blank)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const usable = options.filter((row) => row.name.trim() && row.value.trim())

  const reset = () => {
    setOpen(false)
    setLabel('')
    setOptions(blank)
    setError(null)
  }

  const add = async () => {
    setBusy(true)
    setError(null)
    try {
      await onSaved(
        await api.post<Product>(`/api/products/${product.id}/variations`, {
          label: label.trim() || null,
          options: usable.map((row) => ({
            name: row.name.trim(),
            value: row.value.trim(),
          })),
        }),
      )
      reset()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
        Add one by hand
      </Button>
    )
  }

  return (
    <div className="space-y-2 rounded-md bg-ink-50 p-3">
      <p className="text-xs text-ink-500">
        For a listing whose options Etsy does not hand over. The option names and
        values have to match what Etsy sends on an order, word for word, or the
        order will not find this — copy them from an order that has already
        arrived where you can.
      </p>

      {options.map((row, index) => (
        <div key={index} className="grid gap-2 sm:grid-cols-2">
          <Field label={index === 0 ? 'Option' : ''}>
            <input
              className={inputClass}
              placeholder="Scale"
              value={row.name}
              onChange={(e) =>
                setOptions((rows) =>
                  rows.map((r, i) => (i === index ? { ...r, name: e.target.value } : r)),
                )
              }
            />
          </Field>
          <Field label={index === 0 ? 'Value' : ''}>
            <input
              className={inputClass}
              placeholder="1:64"
              value={row.value}
              onChange={(e) =>
                setOptions((rows) =>
                  rows.map((r, i) => (i === index ? { ...r, value: e.target.value } : r)),
                )
              }
            />
          </Field>
        </div>
      ))}

      <Button
        size="sm"
        variant="ghost"
        onClick={() => setOptions((rows) => [...rows, { name: '', value: '' }])}
      >
        Another option
      </Button>

      <Field
        label="Name (optional)"
        hint="What this combination is called on screen. Left blank, it is named after the options."
      >
        <input
          className={inputClass}
          placeholder="1:64 Scale"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>

      {error ? <Alert tone="error">{error}</Alert> : null}

      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="primary"
          disabled={busy || usable.length === 0}
          onClick={add}
        >
          {busy ? 'Adding…' : 'Add variation'}
        </Button>
        <Button size="sm" variant="ghost" onClick={reset} disabled={busy}>
          Cancel
        </Button>
      </div>
    </div>
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

/** Which QuickBooks item each chosen option is sold as.
 *
 * One listing is often several things on the books — a playset in HO and in
 * 1:64 — and the option the buyer picked is what says which. So the mapping
 * belongs on the option, and a product carries as many as it has options worth
 * telling apart. That is the whole point: several QuickBooks items on one
 * product, chosen by what the buyer picked.
 *
 * Set by hand and only by hand. Nothing derives these, because which of a
 * shop's items a combination is sold as is a decision about their books.
 *
 * Not the same thing as the option rules above, which answer a different
 * question about the same option: those say what comes off the shelf to make
 * it, these say what it is sold as. A colour usually changes the first and not
 * the second; a scale usually changes both.
 */
function OptionItemsEditor({
  product,
  onSaved,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
}) {
  const observed = useObservedOptions(product.id)
  const [optionName, setOptionName] = useState('')
  const [optionValue, setOptionValue] = useState('')
  // Conditions staged so far. A mapping can pin several — "1:64 *and* the
  // loadout" — and every one of them has to be among the buyer's choices.
  const [pending, setPending] = useState<{ name: string; value: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)

  const values = observed?.find((o) => o.name === optionName)?.values ?? []
  const typed = Boolean(optionName.trim() && optionValue.trim())
  const ready = pending.length > 0 || typed

  const call = async (run: Promise<Product>) => {
    setBusy(true)
    setError(null)
    try {
      await onSaved(await run)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  /** Whatever is staged, plus whatever is half-typed in the row below it. */
  const conditions = () =>
    typed
      ? [...pending, { name: optionName.trim(), value: optionValue.trim() }]
      : pending

  const add = async (item: QboItem) => {
    setPicking(false)
    await call(
      api.post<Product>(`/api/products/${product.id}/option-items`, {
        options: conditions(),
        qbo_item_id: item.id,
        qbo_item_name: item.name,
      }),
    )
    setPending([])
    setOptionValue('')
  }

  return (
    <div className="space-y-2 rounded-md border border-ink-200 p-3">
      <h3 className="text-sm font-semibold text-ink-800">Sold as, by option</h3>
      <p className="text-xs text-ink-500">
        Which QuickBooks item an invoice line names, decided by what the buyer
        picked. A listing sold in two scales is two items on the books — put both
        here and each order bills against the right one. The same item is what
        printed units are taken out of stock from, so the two can never disagree.
      </p>

      {product.option_items.length === 0 ? (
        <p className="text-sm text-ink-500">
          None — every order for this product bills against the product's own
          QuickBooks item.
        </p>
      ) : (
        <ul className="space-y-1">
          {product.option_items.map((row, index) => (
            <li
              key={row.id}
              className="flex flex-wrap items-center gap-2 rounded-md bg-ink-50 px-2 py-1.5 text-xs"
            >
              <span className="text-ink-500">When</span>
              <span className="font-medium text-ink-800">
                {row.options
                  .map((option) => `${option.name}: ${option.value}`)
                  .join(' and ')}
              </span>
              <span className="text-ink-500">bill as</span>
              <span className="font-medium text-ink-800">
                {row.qbo_item_name ?? `item ${row.qbo_item_id}`}
              </span>
              {product.option_items.length > 1 ? (
                <span className="ml-auto flex items-center gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || index === 0}
                    onClick={() =>
                      call(
                        api.post<Product>(
                          `/api/products/${product.id}/option-items/${row.id}/move?up=true`,
                        ),
                      )
                    }
                  >
                    ↑
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || index === product.option_items.length - 1}
                    onClick={() =>
                      call(
                        api.post<Product>(
                          `/api/products/${product.id}/option-items/${row.id}/move?up=false`,
                        ),
                      )
                    }
                  >
                    ↓
                  </Button>
                </span>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                className={product.option_items.length > 1 ? '' : 'ml-auto'}
                disabled={busy}
                onClick={() =>
                  call(
                    api.del<Product>(
                      `/api/products/${product.id}/option-items/${row.id}`,
                    ),
                  )
                }
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      {product.option_items.length > 1 ? (
        <p className="text-xs text-ink-500">
          A buyer can match more than one of these. The mapping naming the most
          options wins — so "1:64 and the loadout" beats plain "1:64", and a
          general rule can still have an exception. Between mappings that name
          the same number, the first here wins, and the arrows say which.
        </p>
      ) : null}

      {/* Conditions staged so far, above the row that adds the next one. */}
      {pending.length ? (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-ink-500">When</span>
          {pending.map((row, index) => (
            <span key={index} className="flex items-center gap-1">
              {index ? <span className="text-ink-500">and</span> : null}
              <span className="font-medium text-ink-800">
                {row.name}: {row.value}
              </span>
              <button
                type="button"
                className="text-ink-400 hover:text-ink-700"
                aria-label={`Drop ${row.name}: ${row.value}`}
                onClick={() =>
                  setPending((rows) => rows.filter((_, i) => i !== index))
                }
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-2">
        <Field label={pending.length ? 'And also' : 'Option'}>
          <select
            className={inputClass}
            value={optionName}
            onChange={(e) => {
              setOptionName(e.target.value)
              setOptionValue('')
            }}
          >
            <option value="">Choose an option…</option>
            {(observed ?? []).map((option) => (
              <option key={option.name} value={option.name}>
                {option.name}
              </option>
            ))}
          </select>
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
                  {entry.orders ? ` (${entry.orders} orders)` : ''}
                </option>
              ))}
            </select>
          ) : (
            <input
              className={inputClass}
              placeholder="1:64"
              value={optionValue}
              onChange={(e) => setOptionValue(e.target.value)}
            />
          )}
        </Field>
      </div>

      {observed !== null && observed.length === 0 ? (
        <p className="text-xs text-ink-500">
          No options seen yet, from an order or from the listing. Type the name
          and value exactly as Etsy sends them.
        </p>
      ) : null}
      {observed !== null && observed.length === 0 ? (
        <Field label="Option name">
          <input
            className={inputClass}
            placeholder="Scale"
            value={optionName}
            onChange={(e) => setOptionName(e.target.value)}
          />
        </Field>
      ) : null}

      {error ? <Alert tone="error">{error}</Alert> : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={busy || !ready} onClick={() => setPicking(true)}>
          {busy ? 'Saving…' : 'Choose the QuickBooks item…'}
        </Button>
        {/* For a combination: pin this one and keep going. Only offered once
            there is something to pin, so it never looks like a required step. */}
        {typed ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setPending((rows) => [
                ...rows,
                { name: optionName.trim(), value: optionValue.trim() },
              ])
              setOptionName('')
              setOptionValue('')
            }}
          >
            …and another option
          </Button>
        ) : null}
        {pending.length ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setPending([])
              setOptionName('')
              setOptionValue('')
            }}
          >
            Start over
          </Button>
        ) : null}
      </div>

      {picking ? (
        <QboItemPicker onClose={() => setPicking(false)} onPick={add} />
      ) : null}
    </div>
  )
}

/** The options this product is actually sold with, from both places they exist.
 *
 * What past orders carried, and what the Etsy listing offers. The listing is
 * the one that works before a single order has arrived, which is exactly when
 * these need setting up; the order counts are the more informative of the two
 * where both know a value, so those win.
 *
 * Shared by the two editors that map an option to something — a BOM component,
 * or a QuickBooks item — so neither can offer a name the other does not.
 */
function useObservedOptions(productId: string): ObservedOption[] | null {
  const [observed, setObserved] = useState<ObservedOption[] | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api
        .get<{ options: ObservedOption[] }>(`/api/products/${productId}/observed-options`)
        .then((d) => d.options)
        .catch(() => [] as ObservedOption[]),
      api
        .get<Catalog>('/api/integrations/etsy/catalog')
        .then((c) =>
          c.error
            ? []
            : c.rows
                .filter((row) => row.product_id === productId)
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
  }, [productId])

  return observed
}


function OptionRulesEditor({
  product,
  allProducts,
  onSaved,
}: {
  product: Product
  allProducts: Product[]
  onSaved: (product: Product) => void | Promise<void>
}) {
  const observed = useObservedOptions(product.id)
  const [optionName, setOptionName] = useState('')
  const [optionValue, setOptionValue] = useState('')
  const [replacesId, setReplacesId] = useState('')
  const [componentId, setComponentId] = useState('')
  const [quantity, setQuantity] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [qboOpen, setQboOpen] = useState(false)

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
  // Two pickers, not one. They used to share a variable because no product
  // offered both: a printed variation picked a file, a stocked one picked an
  // item. A printed variation can now want its own item as well — its colour
  // is its own thing in QuickBooks — so which picker is open has to be two
  // questions rather than one answered by the product's fulfillment.
  const [pickingFile, setPickingFile] = useState<ProductVariation | null>(null)
  const [pickingItem, setPickingItem] = useState<ProductVariation | null>(null)

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
            ? 'Pull them from Etsy — every combination the listing sells becomes a row. If the listing has none to give, add one below.'
            : 'Link an Etsy listing below to pull them, or add one by hand.'}
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
                    {/* On a bundle the two would be the same button, since a
                        bundle's own product is one with components. */}
                    {product.fulfillment !== 'bundle' ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => makeProduct(variation, product.fulfillment)}
                      >
                        …or its own {product.fulfillment} product
                      </Button>
                    ) : null}
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
                        onClick={() => setPickingFile(variation)}
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
                      <Button size="sm" variant="ghost" onClick={() => setPickingFile(variation)}>
                        Use a different one
                      </Button>
                    </>
                  )}
                </div>
              ) : null}

              {/* Every variation, with no exceptions left. It was stocked-only
                  when drawing stock down was the only thing a variation's item
                  did; it is also what an invoice line names, and that applies
                  to every kind of product — one listing sold in two scales is
                  two things in QuickBooks. The last exception was a variation
                  promoted to its own product, on the grounds that the item
                  belongs on that product. It can, and it still does when this
                  is left alone — but "go and edit a different product" is a
                  worse answer than a field that is already here, and a shop
                  may well bill the combination as something other than what it
                  makes. Where they differ, the more specific one wins. */}
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-ink-500">
                  {product.fulfillment === 'bundle' ? 'Invoices as:' : 'QuickBooks:'}
                </span>
                <span className="text-ink-700">
                  {variation.qbo_item_name ??
                    (variation.qbo_item_id
                      ? `item ${variation.qbo_item_id}`
                      : variation.variant_product_qbo_item_name ??
                        (variation.variant_product_qbo_item_id
                          ? `item ${variation.variant_product_qbo_item_id}`
                          : variation.variant_product_id
                            ? 'nothing — its own product has no item either'
                            : "the product's QuickBooks item"))}
                </span>
                {variation.variant_product_id && !variation.qbo_item_id ? (
                  <span className="text-ink-500">from its own product</span>
                ) : null}
                {variation.qbo_item_id ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() =>
                      save(variation, { qbo_item_id: null, qbo_item_name: null })
                    }
                  >
                    {variation.variant_product_id
                      ? "Use its own product's item"
                      : "Use the product's item"}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setPickingItem(variation)}
                  >
                    {product.fulfillment === 'bundle'
                      ? 'Invoice this one separately'
                      : 'Track separately'}
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <NewVariationForm product={product} onSaved={onSaved} />

      {pickingFile ? (
        <PrintFilePicker
          choice={{
            printer_id: pickingFile.bambuddy_printer_id,
            printer_models: pickingFile.printer_models ?? [],
          }}
          onClose={() => setPickingFile(null)}
          onPick={async (picked: FileChoice) => {
            const variation = pickingFile
            setPickingFile(null)
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

      {pickingItem ? (
        <QboItemPicker
          onClose={() => setPickingItem(null)}
          onPick={async (item) => {
            const variation = pickingItem
            setPickingItem(null)
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

/** The Wix catalogue items that resolve to this product.
 *
 * The same job as the Etsy editor above, with one difference that decides the
 * whole design: a Wix catalogue id is a GUID. Nobody types a GUID, and nobody
 * should be asked to — so the way in is a search over the live catalogue, and
 * pasting an id is the fallback rather than the path.
 */
function WixLinksEditor({
  product,
  onSaved,
}: {
  product: Product
  onSaved: (product: Product) => void | Promise<void>
}) {
  const [picking, setPicking] = useState(false)
  const [itemId, setItemId] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const link = async (wixItemId: string, title?: string | null) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const saved = await api.post<Product & { also_fixed?: number }>(
        `/api/products/${product.id}/wix-links`,
        { wix_catalog_item_id: wixItemId, item_title: title ?? null },
      )
      await onSaved(saved)
      setItemId('')
      setPicking(false)
      const fixed = saved.also_fixed ?? 0
      if (fixed > 0) {
        setNotice(
          `${fixed} order line${fixed === 1 ? '' : 's'} that were waiting on this ` +
            'item now match.',
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
        await api.del<Product>(`/api/products/${product.id}/wix-links/${linkId}`),
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
        <h3 className="text-sm font-semibold text-ink-800">Wix items</h3>
        <p className="text-xs text-ink-500">
          Wix orders match on these, the same way Etsy orders match on the
          listings above. Links are also made for you by <em>Check against Wix</em>,
          and from the order drawer when you match a line and tick “remember this
          item”.
        </p>
      </div>

      {product.wix_links.length === 0 ? (
        <p className="text-sm text-ink-500">
          No Wix items linked. If this product also sells on Wix under the same
          code, <em>Check against Wix</em> will find it — or search for it below.
        </p>
      ) : (
        <ul className="space-y-1 text-sm">
          {product.wix_links.map((row) => (
            <li
              key={row.id}
              className="flex flex-wrap items-center gap-2 border-b border-ink-100 pb-1"
            >
              <span className="min-w-0 flex-1 truncate text-ink-800">
                {row.item_title ?? row.wix_catalog_item_id}
              </span>
              {row.wix_variant_id ? (
                <Badge className="bg-ink-100 text-ink-700 ring-ink-300">
                  one variant only
                </Badge>
              ) : null}
              <code className="truncate font-mono text-xs text-ink-400">
                {row.wix_catalog_item_id}
              </code>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => remove(row.id)}
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
        <Button onClick={() => setPicking(true)} disabled={busy}>
          Search the Wix catalogue
        </Button>
        <span className="text-xs text-ink-400">or</span>
        <div className="min-w-40 flex-1">
          <Field label="Catalogue item id">
            <input
              className={inputClass}
              placeholder="00000000-0000-0000-0000-000000000000"
              value={itemId}
              onChange={(e) => setItemId(e.target.value)}
            />
          </Field>
        </div>
        <Button onClick={() => link(itemId.trim())} disabled={busy || !itemId.trim()}>
          {busy ? 'Linking…' : 'Link item'}
        </Button>
      </div>

      {picking ? (
        <WixItemPicker
          product={product}
          onClose={() => setPicking(false)}
          onPick={link}
        />
      ) : null}
    </div>
  )
}

/** Search the live Wix catalogue and pick the item this product is sold as.
 *
 * Opens pre-filtered by the product's own code, because the overwhelmingly
 * common case — a Wix catalogue imported from Etsy — means the counterpart is
 * sitting there under exactly that code and the operator should not have to
 * type anything at all.
 */
function WixItemPicker({
  product,
  onClose,
  onPick,
}: {
  product: Product
  onClose: () => void
  onPick: (wixItemId: string, title: string | null) => void | Promise<void>
}) {
  const [rows, setRows] = useState<WixCatalogItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState(product.sku ?? '')

  useEffect(() => {
    api
      .get<{ items: WixCatalogItem[]; error: string | null }>(
        '/api/integrations/wix/catalog',
      )
      .then((data) => {
        if (data.error) setError(data.error)
        setRows(data.items)
      })
      .catch((err) => setError(errorMessage(err)))
  }, [])

  const needle = query.trim().toLowerCase()
  const shown = (rows ?? []).filter(
    (row) =>
      !needle ||
      (row.name ?? '').toLowerCase().includes(needle) ||
      (row.sku ?? '').toLowerCase().includes(needle),
  )

  return (
    <Modal open title="Find this product in the Wix catalogue" onClose={onClose}>
      <input
        className={inputClass}
        placeholder="Search by name or code…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {error ? (
        <div className="mt-3">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}
      <div className="mt-3 max-h-80 space-y-1 overflow-y-auto">
        {rows === null && !error ? <Spinner /> : null}
        {rows !== null && shown.length === 0 ? (
          <p className="py-4 text-center text-sm text-ink-500">
            {rows.length === 0
              ? 'The Wix catalogue is empty, or the API key cannot read it.'
              : 'Nothing in the catalogue matches that.'}
          </p>
        ) : null}
        {shown.map((row) => {
          const taken = row.status === 'linked'
          return (
            <button
              key={row.wix_catalog_item_id}
              type="button"
              disabled={taken}
              onClick={() => onPick(row.wix_catalog_item_id, row.name)}
              className={cx(
                'flex w-full flex-wrap items-center gap-2 rounded-md px-2 py-2 text-left',
                taken ? 'opacity-50' : 'hover:bg-ink-50',
              )}
            >
              <span className="min-w-0 flex-1 truncate text-sm text-ink-800">
                {row.name ?? row.wix_catalog_item_id}
              </span>
              {row.sku ? (
                <span className="font-mono text-xs text-ink-500">{row.sku}</span>
              ) : null}
              {taken ? (
                <Badge className="bg-ink-100 text-ink-600 ring-ink-300">
                  linked to {row.product_sku ?? 'another product'}
                </Badge>
              ) : null}
            </button>
          )
        })}
      </div>
    </Modal>
  )
}


/** What Wix sells, lined up against what PrintFlow can make.
 *
 * The Etsy version of this screen exists to *create* products, because Etsy is
 * usually where a shop's catalogue came from. This one exists to *recognise*
 * them: a shop running both channels almost always built its Wix catalogue by
 * importing from Etsy, so every Wix item already has a product here under
 * another name. The work is joining them up, and there is no reason for that to
 * be several hundred clicks.
 *
 * Nothing is linked without being confirmed. A wrong link sends real orders to
 * the wrong product, and is noticed when the wrong thing comes off a printer.
 */
function WixCatalogCheck({
  onClose,
  onChanged,
}: {
  onClose: () => void
  onChanged: () => void | Promise<void>
}) {
  const [data, setData] = useState<WixCatalog | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<string | null>(null)

  const load = useCallback(async () => {
    setData(null)
    setError(null)
    try {
      const found = await api.get<WixCatalog>('/api/integrations/wix/catalog')
      setData(found)
      if (found.error) setError(found.error)
      // Everything it recognised, ticked. The operator's job is to untick what
      // looks wrong, which is far less work than ticking what looks right.
      setChosen(
        new Set(
          found.items
            .filter((row) => row.product_id && row.status !== 'linked')
            .map((row) => row.wix_catalog_item_id),
        ),
      )
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const linkChosen = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.post<{
        linked: unknown[]
        skipped: unknown[]
        also_fixed: number
      }>('/api/integrations/wix/catalog/link', {
        wix_catalog_item_ids: [...chosen],
      })
      const fixed = result.also_fixed
      setDone(
        `Linked ${result.linked.length} item${result.linked.length === 1 ? '' : 's'}` +
          (fixed ? `, and ${fixed} waiting order line${fixed === 1 ? '' : 's'} now match` : '') +
          '.',
      )
      await onChanged()
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  const toggle = (id: string) => {
    setChosen((was) => {
      const next = new Set(was)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const rows = data?.items ?? []
  const proposals = rows.filter((row) => row.product_id && row.status !== 'linked')
  const unmatched = rows.filter((row) => !row.product_id)
  const already = rows.filter((row) => row.status === 'linked')

  return (
    <Modal open title="Check against Wix" onClose={onClose}>
      <p className="mb-3 text-sm text-ink-600">
        Every item your Wix site sells, and the product PrintFlow thinks it is.
        Matches are found by product code first, then by the title of the Etsy
        listing the item was imported from, then by the product's own name.
      </p>

      {error ? <Alert tone="error">{error}</Alert> : null}
      {done ? <Alert tone="success">{done}</Alert> : null}
      {data === null && !error ? <Spinner /> : null}

      {data ? (
        <>
          {/* gap-x rather than interpuncts between spans: the row wraps on a
              narrow screen, and a separator that wraps to the end of a line is
              a line ending in a dot for no reason. */}
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-500">
            <span>{data.total} in the Wix catalogue</span>
            <span>{already.length} already linked</span>
            <span>{proposals.length} can be linked now</span>
            {unmatched.length ? (
              <span>{unmatched.length} unrecognised</span>
            ) : null}
            {data.api_version ? (
              <span className="ml-auto font-mono">Stores {data.api_version}</span>
            ) : null}
          </div>

          {proposals.length ? (
            <div className="max-h-72 space-y-1 overflow-y-auto rounded-md border border-ink-200 p-2">
              {proposals.map((row) => (
                <label
                  key={row.wix_catalog_item_id}
                  className="flex flex-wrap items-center gap-2 rounded px-1 py-1 text-sm hover:bg-ink-50"
                >
                  <input
                    type="checkbox"
                    checked={chosen.has(row.wix_catalog_item_id)}
                    onChange={() => toggle(row.wix_catalog_item_id)}
                  />
                  <span className="min-w-0 flex-1 truncate text-ink-800">
                    {row.name ?? row.wix_catalog_item_id}
                  </span>
                  <span aria-hidden className="text-ink-400">
                    →
                  </span>
                  <span className="font-mono text-xs text-ink-700">
                    {row.product_sku}
                  </span>
                  <Badge className={WIX_MATCH_CLASSES[row.status] ?? ''}>
                    {row.matched_on ?? 'matched'}
                  </Badge>
                </label>
              ))}
            </div>
          ) : (
            <p className="rounded-md border border-dashed border-ink-300 px-3 py-6 text-center text-sm text-ink-500">
              {rows.length
                ? 'Nothing left to link — every Wix item PrintFlow recognises already is.'
                : 'No catalogue items to show.'}
            </p>
          )}

          {unmatched.length ? (
            <details className="mt-3">
              <summary className="cursor-pointer text-xs text-ink-500">
                {unmatched.length} Wix item{unmatched.length === 1 ? '' : 's'} nothing
                matches — a Wix order for these will stall
              </summary>
              <ul className="mt-1 space-y-0.5 text-xs text-ink-600">
                {unmatched.map((row) => (
                  <li key={row.wix_catalog_item_id} className="truncate">
                    {row.name ?? row.wix_catalog_item_id}
                    {row.sku ? (
                      <span className="ml-2 font-mono text-ink-400">{row.sku}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={busy || chosen.size === 0}
              onClick={linkChosen}
            >
              {busy ? 'Linking…' : `Link ${chosen.size} item${chosen.size === 1 ? '' : 's'}`}
            </Button>
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
          </div>
        </>
      ) : null}
    </Modal>
  )
}

const WIX_MATCH_CLASSES: Record<string, string> = {
  sku: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  etsy_title: 'bg-sky-100 text-sky-800 ring-sky-300',
  product_name: 'bg-amber-100 text-amber-800 ring-amber-300',
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
