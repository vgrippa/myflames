// The members x items matrix the Topology place's Parameters, Accounts and
// Plugins tabs share: the first column names the item, one column per
// compared member (profile names in the headers). A Show filter starts on
// Differences (rows whose status is not same; per-server rows stay hidden)
// and All shows everything, per-server rows with a Per server pill. Members
// the read could not compare are listed under the table with their reason,
// and the backend's notes show once above it.
import { useMemo, useState, type ReactNode } from "react";
import { DataTable, Pill, Segmented, type Column, type Selection } from "./kit";
import type { DriftStatus, NotCompared, TopologyNote } from "./topologyApi";

export type DriftMember = { id: string; name: string };

export function GroupDriftTable<T extends { status: DriftStatus }>({
  id,
  label,
  itemLabel,
  rows,
  members,
  notCompared,
  notes,
  memberName,
  rowKey,
  itemName,
  cell,
  selection,
}: {
  /** The table's own column layout (ColumnStoreContext), one per tab. */
  id: string;
  label: string;
  /** The first column's header ("Parameter", "Account", "Plugin"). */
  itemLabel: string;
  rows: T[];
  /** The compared members, in column order. */
  members: DriftMember[];
  notCompared: NotCompared[];
  notes?: TopologyNote[];
  /** A profile id's display name (for the not-compared list). */
  memberName: (profileId: string) => string;
  rowKey: (row: T) => string;
  itemName: (row: T) => string;
  /** The item's value on one compared member (null: not present there). */
  cell: (row: T, profileId: string) => ReactNode;
  selection: Selection;
}) {
  const [show, setShow] = useState<"differences" | "all">("differences");
  const [search, setSearch] = useState("");
  const query = search.trim().toLowerCase();
  const filtered = useMemo(() => rows.filter((row) => {
    if (query && !itemName(row).toLowerCase().includes(query)) return false;
    if (show === "differences") return row.status !== "same" && row.status !== "per_server";
    return true;
  }), [rows, show, query, itemName]);

  const columns: Column<T>[] = [
    {
      key: "item", label: itemLabel, primary: true, flex: true, minWidth: 140,
      sort: (row) => itemName(row),
      render: (row) => (
        <span className="drift-item">
          <span className="cell-truncate" title={itemName(row)}>{itemName(row)}</span>
          {show === "all" && row.status === "per_server" && <Pill tone="info">Per server</Pill>}
        </span>
      ),
    },
    ...members.map((member): Column<T> => ({
      key: `member:${member.id}`,
      label: member.name,
      name: member.name,
      render: (row: T) => cell(row, member.id),
    })),
  ];

  return (
    <section className="drift-table" aria-label={label}>
      <div className="drift-controls">
        <Segmented<"differences" | "all">
          label="Show"
          value={show}
          onChange={setShow}
          options={[
            { value: "differences", label: "Differences", title: "Rows whose status is not same; per-server rows stay hidden" },
            { value: "all", label: "All", title: "Every row, with per-server rows marked" },
          ]}
        />
        <input
          type="search"
          className="drift-search"
          aria-label={`Search ${label.toLowerCase()}`}
          placeholder={`Search ${label.toLowerCase()}…`}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>
      {notes && notes.length > 0 && (
        <div className="drift-notes">
          {notes.map((note, index) => (
            <p key={`${note.profile_id}-${index}`} className="hint">{note.message}</p>
          ))}
        </div>
      )}
      <DataTable id={id} label={label} columns={columns} rows={filtered} rowKey={rowKey} selection={selection} />
      {notCompared.length > 0 && (
        <div className="drift-not-compared">
          <span className="drift-row-label">Not compared</span>
          <ul>
            {notCompared.map((item) => (
              <li key={item.profile_id}>
                <strong>{memberName(item.profile_id)}</strong>: {item.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
