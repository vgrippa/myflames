import { useEffect, useRef, useState } from 'react';
import { type Connection } from './api';
import { connectionKey } from './binding';
import { ConfirmSheet, copyToClipboard, useFocusScope } from './kit';
import { Icon } from './components';
import { csvCell } from './reportTableExport';
import { tsv } from './ResultsPane';
import { tableData, type Cell, type Change, type TablePage, type TableReview, type TableOutcome } from './tableDataApi';
import './table-data.css';

/** An explicit base-table viewer. Drafts belong to the captured connection;
 * editor query results are never treated as editable table rows. */
export function TableData({ connection, currentConnectionKey, schema, table, onClose }: {
  connection: Connection; currentConnectionKey: string; schema: string; table: string; onClose(): void;
}) {
  const [page, setPage] = useState<TablePage | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Change>>({});
  const [inserts, setInserts] = useState<{ id: string; values: Record<string, Cell> }[]>([]);
  const [review, setReview] = useState<TableReview | null>(null);
  const [outcome, setOutcome] = useState<TableOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [discard, setDiscard] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    rowId: string;
    colIndex: number;
    inserted: boolean;
    opener: HTMLElement;
  } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useFocusScope(menuRef, {
    open: !!contextMenu, onClose: () => setContextMenu(null), menu: true,
    returnFocus: () => contextMenu?.opener,
  });
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number | null>(null);

  const showToast = (msg: string) => {
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = window.setTimeout(() => setToast(null), 1800);
  };

  const copyWithFeedback = async (text: string, msg?: string) => {
    setContextMenu(null);
    const ok = await copyToClipboard(text);
    showToast(ok ? (msg || "Copied to clipboard") : "Could not copy to clipboard");
  };

  useEffect(() => {
    if (!contextMenu) return;
    const handleOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (!target?.closest(".context-menu")) {
        setContextMenu(null);
      }
    };
    window.addEventListener("pointerdown", handleOutside);
    return () => {
      window.removeEventListener("pointerdown", handleOutside);
    };
  }, [contextMenu]);
  const heading = useRef<HTMLHeadingElement>(null);
  const alive = useRef(true);
  const pending = useRef(false);
  const polling = useRef(false);
  const owner = useRef(connection);
  const snapshot = useRef<string | null>(null);
  const requestEpoch = useRef(0);
  const mismatch = currentConnectionKey !== connectionKey(connection);
  const changed = Object.keys(drafts).length + inserts.length;
  const running = outcome?.status === 'running';
  const editable = !!page?.editable && !mismatch && !busy && !running && !unknown && !outcome;
  useEffect(() => { alive.current = true; heading.current?.focus(); void load(0); return () => {
    alive.current = false; requestEpoch.current += 1;
    if (snapshot.current) void tableData('close', owner.current, { snapshot_id: snapshot.current }).catch(() => {});
  }; }, []);
  async function load(offset: number) {
    if (pending.current || mismatch || changed || running || unknown) return;
    pending.current = true; setBusy(true); setError('');
    const epoch = ++requestEpoch.current;
    const previous = snapshot.current;
    try {
      const data = await tableData<TablePage>('open', owner.current, { schema, table, offset, limit: 50 });
      if (!alive.current || epoch !== requestEpoch.current) {
        void tableData('close', owner.current, { snapshot_id: data.snapshot_id }).catch(() => {}); return;
      }
      snapshot.current = data.snapshot_id;
      setPage(data); setOutcome(null);
      if (previous) void tableData('close', owner.current, { snapshot_id: previous }).catch(() => {});
    } catch (failure) { if (alive.current) setError((failure as Error).message); }
    finally { pending.current = false; if (alive.current) setBusy(false); }
  }
  function update(rowId: string, column: string, value: Cell) {
    if (!drafts[rowId] && changed >= 20) { setError('Review at most 20 row changes at a time.'); return; }
    setReview(null);
    setDrafts(current => ({ ...current, [rowId]: { kind: 'update', row_id: rowId, values: { ...current[rowId]?.values, [column]: value } } }));
  }
  async function prepare() {
    if (!page || pending.current || !editable || !changed) return;
    pending.current = true; setBusy(true); setError('');
    try {
      const data = await tableData<TableReview>('prepare', owner.current, { snapshot_id: page.snapshot_id,
        changes: [...Object.values(drafts), ...inserts.map(row => ({ kind: 'insert', values: row.values }))] });
      if (alive.current) setReview(data);
    } catch (failure) { if (alive.current) setError((failure as Error).message); }
    finally { pending.current = false; if (alive.current) setBusy(false); }
  }
  async function decline() {
    if (!review || pending.current) return;
    const current = review; setReview(null);
    try { await tableData('decline', owner.current, { snapshot_id: current.snapshot_id, review_token: current.review_token }); }
    catch (failure) { if (alive.current) setError((failure as Error).message); }
  }
  async function status() {
    if (!snapshot.current || polling.current) return;
    polling.current = true;
    try {
      const result = await tableData<TableOutcome>('status', owner.current, { snapshot_id: snapshot.current });
      if (!alive.current) return;
      setOutcome(result); setUnknown(result.status === 'unknown');
      if (result.status === 'complete') { setDrafts({}); setInserts([]); }
      return result;
    } catch (failure) { if (alive.current) { setUnknown(true); setError('Could not verify the outcome. Changes may have committed. Check status or close and inspect the table afresh; do not resend this batch. ' + (failure as Error).message); } }
    finally { polling.current = false; }
  }
  async function apply() {
    if (!review || mismatch || pending.current) return;
    const current = review;
    pending.current = true; setBusy(true); setError(''); setReview(null);
    try {
      const result = await tableData<TableOutcome>('apply', owner.current, { snapshot_id: current.snapshot_id, review_token: current.review_token, confirmed: true });
      if (alive.current) setOutcome(result);
    } catch { if (alive.current) { setUnknown(true); setError('The reply was lost. Checking the existing operation without sending it again.'); await status(); } }
    finally { pending.current = false; if (alive.current) setBusy(false); }
  }
  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const timer = window.setInterval(() => { if (!cancelled) void status(); }, 600);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [running]);
  async function cancel() {
    if (!snapshot.current) return;
    try { await tableData('cancel', owner.current, { snapshot_id: snapshot.current }); }
    catch (failure) { setError((failure as Error).message); }
  }
  function close() {
    if (running && !unknown) { setError('Wait for the operation outcome before closing this table.'); return; }
    if (unknown || changed) setDiscard(true); else onClose();
  }
  function resetDrafts() { setDrafts({}); setInserts([]); setReview(null); }
  const openCellMenu = (opener: HTMLElement, rowId: string, colIndex: number, inserted: boolean, clientX: number, clientY: number) => {
    const menuW = 240;
    const menuH = 340;
    const x = Math.max(8, Math.min(clientX, window.innerWidth - menuW - 16));
    const y = Math.max(8, Math.min(clientY, window.innerHeight - menuH - 16));
    setContextMenu({
      x,
      y,
      rowId,
      colIndex,
      inserted,
      opener,
    });
  };
  const onCellContextMenu = (e: React.MouseEvent<HTMLTableCellElement>, rowId: string, colIndex: number, inserted: boolean) => {
    e.preventDefault();
    e.stopPropagation();
    openCellMenu(e.currentTarget, rowId, colIndex, inserted, e.clientX, e.clientY);
  };
  const onCellKey = (e: React.KeyboardEvent<HTMLTableCellElement>, rowId: string, colIndex: number, inserted: boolean) => {
    if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      e.preventDefault();
      e.stopPropagation();
      const box = e.currentTarget.getBoundingClientRect();
      openCellMenu(e.target as HTMLElement, rowId, colIndex, inserted, box.left, box.bottom);
    } else if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === 'F2')) {
      e.preventDefault();
      // Native editors already exist. Prefer the value, or its type selector
      // for NULL/default cells, without bypassing read-only/deleted guards.
      (e.currentTarget.querySelector<HTMLInputElement>('input:not([readonly])') ||
        e.currentTarget.querySelector<HTMLSelectElement>('select:not(:disabled)'))?.focus();
    }
  };

  const applyCellUpdate = (rowId: string, colName: string, next: Cell | undefined, inserted: boolean) => {
    if (inserted) {
      setInserts(rows => rows.map(row => {
        if (row.id !== rowId) return row;
        const values = { ...row.values };
        if (next) values[colName] = next; else delete values[colName];
        return { ...row, values };
      }));
    } else if (next) {
      update(rowId, colName, next);
    } else {
      setDrafts(current => {
        if (!current[rowId]) return current;
        const values = { ...current[rowId].values };
        delete values[colName];
        if (Object.keys(values).length === 0) {
          const nextDrafts = { ...current };
          delete nextDrafts[rowId];
          return nextDrafts;
        }
        return { ...current, [rowId]: { ...current[rowId], values } };
      });
    }
  };

  const renderCell = (rowId: string, index: number, initial: Cell, inserted = false) => {
    const column = page!.columns[index];
    const insert = inserts.find(row => row.id === rowId);
    const value = inserted ? insert?.values[column.name] : drafts[rowId]?.values?.[column.name] || initial;
    const deleted = !inserted && drafts[rowId]?.kind === 'delete';
    const set = (next: Cell | undefined) => {
      if (inserted) setInserts(rows => rows.map(row => {
        if (row.id !== rowId) return row;
        const values = { ...row.values };
        if (next) values[column.name] = next; else delete values[column.name];
        return { ...row, values };
      }));
      else if (next) update(rowId, column.name, next);
    };
    return <td key={column.name} tabIndex={0} className={deleted ? 'table-data-deleted' : ''}
      onKeyDown={e => onCellKey(e, rowId, index, inserted)} onContextMenu={e => onCellContextMenu(e, rowId, index, inserted)}>
      <select aria-label={`${column.name} value type for ${rowId}`} disabled={!editable || deleted}
        value={value?.kind || 'default'} onChange={event => set(event.target.value === 'default' ? undefined :
          { kind: event.target.value as Cell['kind'], value: event.target.value === 'null' ? null : '' })}>
        {inserted && <option value="default">Default</option>}
        <option value={column.kind}>{column.kind === 'hex' ? 'Hex' : 'Value'}</option>
        {column.nullable && <option value="null">NULL</option>}
      </select>
      {value?.kind === 'null' ? <span className="table-data-null">NULL</span> : !value ? <span className="table-data-null">Default</span> :
        <input aria-label={`${column.name} for ${rowId}`} value={value.value || ''} readOnly={!editable || deleted}
          onChange={event => set({ kind: value.kind, value: event.target.value })} spellCheck={false} />}
    </td>;
  };
  const activeCol = contextMenu && page ? page.columns[contextMenu.colIndex] : null;
  const activeRow = contextMenu && page ? page.rows.find(r => r.id === contextMenu.rowId) : null;
  const activeInsert = contextMenu && contextMenu.inserted ? inserts.find(r => r.id === contextMenu.rowId) : null;
  const activeVal = activeCol && contextMenu
    ? (contextMenu.inserted ? activeInsert?.values[activeCol.name] : drafts[contextMenu.rowId]?.values?.[activeCol.name] || (activeRow ? activeRow.values[contextMenu.colIndex] : undefined))
    : undefined;
  const activeDeleted = !contextMenu?.inserted && contextMenu && drafts[contextMenu.rowId]?.kind === 'delete';
  // null is SQL NULL (empty in TSV and CSV like the editor's export, null in JSON);
  // an unset cell on a new row is the column default.
  const raw = (v: Cell | undefined): string | null => v?.kind === 'null' ? null : !v ? 'Default' : v.value || '';
  const activeCellText = raw(activeVal) ?? '';

  const activeRowVals = page && contextMenu
    ? page.columns.map((c, i) => {
        const v = contextMenu.inserted
          ? activeInsert?.values[c.name]
          : drafts[contextMenu.rowId]?.values?.[c.name] || (activeRow ? activeRow.values[i] : undefined);
        return raw(v);
      })
    : [];

  const targetColIndex = contextMenu ? contextMenu.colIndex : -1;
  const activeColVals = page && activeCol && targetColIndex >= 0
    ? [
        ...page.rows.map(r => {
          const v = drafts[r.id]?.values?.[activeCol.name] || r.values[targetColIndex];
          return raw(v);
        }),
        ...inserts.map(ins => {
          const v = ins.values[activeCol.name];
          return raw(v);
        }),
      ]
    : [];

  const tableTsv = page
    ? tsv([
        page.columns.map(c => c.name),
        ...page.rows.map(r => page.columns.map((c, i) => raw(drafts[r.id]?.values?.[c.name] || r.values[i]))),
        ...inserts.map(ins => page.columns.map(c => raw(ins.values[c.name]))),
      ])
    : "";

  return <section className="table-data-panel" role="region" aria-labelledby="table-data-title">
    <header><div><h2 id="table-data-title" tabIndex={-1} ref={heading}>{schema}.{table}</h2><p>Table data · UTC · {page?.rows.length ?? 0} loaded rows</p></div>
      <button className="button" onClick={close}>Close table</button></header>
    {mismatch && <p role="alert">This table belongs to another connection. Return to that connection to continue; staged changes are preserved.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {page?.reason && <p className="notice">{page.reason}</p>}
    {outcome && <div className="notice" role="status">{outcome.message || (running ? 'Applying the reviewed changes…' : outcome.status)}
      {running && <button className="button small" onClick={() => void cancel()}>Cancel changes</button>}
      {(unknown || outcome.status === 'unknown') && <button className="button small" onClick={() => void status()}>Check status</button>}
    </div>}
    {unknown && !outcome && <button className="button" onClick={() => void status()}>Check status</button>}
    <div className="table-data-actions">
      <button className="button" disabled={!editable || changed >= 20} onClick={() => setInserts(rows => [...rows, { id: `new-${crypto.randomUUID()}`, values: {} }])}>Add row</button>
      <button className="button primary" disabled={!editable || !changed} onClick={() => void prepare()}>Review {changed || ''} changes</button>
      <button className="button" disabled={!changed || busy || running || unknown} onClick={resetDrafts}>Discard changes</button>
      <button className="button" disabled={!!changed || busy || running || mismatch || unknown} onClick={() => void load(page?.offset || 0)}>Reload rows</button>
    </div>
    <div className="table-data-scroll" tabIndex={0} aria-label="Table rows">
      <table><thead><tr><th>Row</th>{page?.columns.map(column => <th key={column.name}>{column.name}<small>{column.type}{page.key.includes(column.name) ? ' · Key' : ''}</small></th>)}</tr></thead>
        <tbody>{page?.rows.map((row, index) => <tr key={row.id} data-row-id={row.id}><td tabIndex={0} onKeyDown={e => onCellKey(e, row.id, 0, false)} onContextMenu={e => onCellContextMenu(e, row.id, 0, false)}><span>{page.offset + index + 1}</span>
          <button className="text-button" disabled={!editable || (!drafts[row.id] && changed >= 20)} onClick={() => setDrafts(current => {
            if (current[row.id]?.kind === 'delete') { const next = { ...current }; delete next[row.id]; return next; }
            return { ...current, [row.id]: { kind: 'delete', row_id: row.id } };
          })}>{drafts[row.id]?.kind === 'delete' ? 'Undo delete' : 'Delete'}</button></td>
          {row.values.map((value, cellIndex) => renderCell(row.id, cellIndex, value))}</tr>)}
          {inserts.map(row => <tr key={row.id} data-row-id={row.id}><td tabIndex={0} onKeyDown={e => onCellKey(e, row.id, 0, true)} onContextMenu={e => onCellContextMenu(e, row.id, 0, true)}>New<button className="text-button" disabled={!editable} onClick={() => setInserts(rows => rows.filter(value => value.id !== row.id))}>Remove</button></td>
            {page?.columns.map((_column, index) => renderCell(row.id, index, { kind: 'null', value: null }, true))}</tr>)}
        </tbody></table>
    </div>
    <footer><button className="button" disabled={!page || page.offset === 0 || !!changed || busy || running || mismatch || unknown} onClick={() => void load(Math.max(0, (page?.offset || 0) - 50))}>Previous</button>
      <span>{busy ? 'Loading…' : `Rows ${(page?.offset || 0) + (page?.rows.length ? 1 : 0)} to ${(page?.offset || 0) + (page?.rows.length || 0)}`}</span>
      <button className="button" disabled={!page?.has_more || !!changed || busy || running || mismatch || unknown} onClick={() => void load((page?.offset || 0) + 50)}>Next</button></footer>
    {review && <ConfirmSheet title="Apply table changes?" description="Review the exact SQL. A changed row or table definition refuses the whole batch." confirmLabel="Apply changes" busy={busy} confirmDisabled={mismatch}
      onConfirm={() => void apply()} onCancel={() => void decline()}>
      <p>{review.connection.user}@{review.connection.host}:{review.connection.port} · {review.connection_type}</p>
      {review.warnings.map(warning => <p className="hint" key={warning}>{warning}</p>)}
      {review.privileges.filter(answer => answer.state !== 'allowed').map((answer, index) => <p className="hint" key={index}>{answer.reason}</p>)}
      <pre className="sql-preview table-data-sql">{review.statements.map(sql => sql + ';').join('\n\n')}</pre>
    </ConfirmSheet>}
    {discard && <ConfirmSheet title={unknown ? "Close with an unknown outcome?" : "Discard staged changes?"} description={unknown ? "Changes may have committed. Closing discards only the local draft. Open the table afresh and inspect its rows before making new changes. Do not resend this batch." : "These changes have not been applied."} confirmLabel={unknown ? "Close and inspect separately" : "Discard and close"} onConfirm={() => { resetDrafts(); onClose(); }} onCancel={() => setDiscard(false)} />}
    {toast && <div className="table-copy-toast" role="status">{toast}</div>}
    {contextMenu && activeCol && (
      <div
        ref={menuRef}
        className="context-menu"
        role="menu"
        aria-label="Cell actions"
        tabIndex={-1}
        style={{ left: contextMenu.x, top: contextMenu.y }}
      >
        <div className="context-menu-header">{activeCol.name}</div>
        <button
          type="button"
          role="menuitem"
          onClick={() => void copyWithFeedback(activeCellText, "Cell value copied")}
        >
          <span className="context-menu-icon"><Icon name="copy" /></span>
          <span>Copy cell value</span>
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => void copyWithFeedback(tsv([activeRowVals]), "Row copied (Excel TSV)")}
        >
          <span className="context-menu-icon"><Icon name="copy" /></span>
          <span>Copy row (Excel TSV)</span>
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            const csv = activeRowVals.map(v => csvCell(v)).join(",");
            void copyWithFeedback(csv, "Row copied as CSV");
          }}
        >
          <span className="context-menu-icon"><Icon name="statements" /></span>
          <span>Copy row as CSV</span>
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            const obj: Record<string, string | null> = {};
            page?.columns.forEach((c, i) => { obj[c.name] = activeRowVals[i] ?? null; });
            void copyWithFeedback(JSON.stringify(obj, null, 2), "Row copied as JSON");
          }}
        >
          <span className="context-menu-icon"><Icon name="query" /></span>
          <span>Copy row as JSON</span>
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => void copyWithFeedback(tsv(activeColVals.map(v => [v])), `Column ${activeCol.name} copied`)}
        >
          <span className="context-menu-icon"><Icon name="copy" /></span>
          <span>Copy column ({activeCol.name})</span>
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => void copyWithFeedback(tableTsv, "Table copied (Excel TSV)")}
        >
          <span className="context-menu-icon"><Icon name="copy" /></span>
          <span>Copy table with headers</span>
        </button>

        {editable && (
          <>
            <div className="context-menu-divider" />
            {activeCol.nullable && !activeDeleted && (
              <button
                type="button"
                role="menuitem"
                disabled={activeVal?.kind === 'null'}
                onClick={() => {
                  applyCellUpdate(contextMenu.rowId, activeCol.name, { kind: 'null', value: null }, contextMenu.inserted);
                  setContextMenu(null);
                }}
              >
                <span className="context-menu-icon">∅</span>
                <span>Set to NULL</span>
              </button>
            )}
            {!activeDeleted && (contextMenu.inserted || drafts[contextMenu.rowId]?.values?.[activeCol.name]) && (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  applyCellUpdate(contextMenu.rowId, activeCol.name, undefined, contextMenu.inserted);
                  setContextMenu(null);
                }}
              >
                <span className="context-menu-icon"><Icon name={contextMenu.inserted ? "refresh" : "undo"} /></span>
                <span>{contextMenu.inserted ? "Set to Default" : "Revert change"}</span>
              </button>
            )}
            {contextMenu.inserted ? (
              <button
                type="button"
                role="menuitem"
                className="is-danger"
                onClick={() => {
                  setInserts(rows => rows.filter(r => r.id !== contextMenu.rowId));
                  setContextMenu(null);
                }}
              >
                <span className="context-menu-icon"><Icon name="trash" /></span>
                <span>Remove row</span>
              </button>
            ) : (
              <button
                type="button"
                role="menuitem"
                className={activeDeleted ? undefined : "is-danger"}
                onClick={() => {
                  if (activeDeleted) {
                    setDrafts(c => { const n = { ...c }; delete n[contextMenu.rowId]; return n; });
                  } else {
                    setDrafts(c => ({ ...c, [contextMenu.rowId]: { kind: 'delete', row_id: contextMenu.rowId } }));
                  }
                  setContextMenu(null);
                }}
              >
                <span className="context-menu-icon"><Icon name={activeDeleted ? "undo" : "trash"} /></span>
                <span>{activeDeleted ? "Undo delete" : "Delete row"}</span>
              </button>
            )}
          </>
        )}
      </div>
    )}
  </section>;
}
