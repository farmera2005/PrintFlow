import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type {
  BambuddyArchive,
  BambuddyFile,
  BambuddyFileTree,
  BambuddyPrinter,
  BambuddyPrinterModel,
} from '../lib/types'
import { Alert, Badge, Button, Field, Modal, Spinner, cx, inputClass } from './ui'
import RawReplies from './RawReplies'

/** Which kinds of machine can take this plate. Nothing ticked means any of them.
 *
 * The farm runs more than one model and a plate that fits one often fits
 * another, so this is a set rather than a single printer: dispatch picks a free
 * machine of any ticked model. Models the farm no longer reports still appear,
 * ticked — dropping one silently would quietly widen the job to everything.
 */
export function PrinterModelPicker({
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
export default function PrintFilePicker({
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

/** The replies behind the tree, verbatim — see RawReplies. */
function WhatBambuddySent({ printerId }: { printerId: number | null }) {
  return (
    <RawReplies
      endpoint={
        '/api/integrations/bambuddy/files/raw' +
        (printerId !== null ? `?printer_id=${printerId}` : '')
      }
    />
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
