// The one group editor sheet, reused by every entry point (the connection
// picker, the status bar of a Group connection, the Connections place):
// rename a group, add and remove members, or delete the group (its saved
// connections stay). Edits go through /api/profiles/groups/save and
// /api/profiles/groups/delete and apply at once.
import { useState } from "react";
import {
  deleteConnectionGroup,
  saveConnectionGroup,
  type GroupSaveResult,
  type Profile,
} from "./api";
import { connectionTarget, SafetyTags } from "./Connections";
import { ActionStatus, Checkbox, ConfirmSheet, useAction } from "./kit";
import { refreshSavedConnections } from "./savedConnections";
import "./group-connections.css";

export function GroupEditorSheet({
  group,
  profiles,
  onSaved,
  onDeleted,
  onClose,
}: {
  /** The group's current name. */
  group: string;
  /** Every saved connection (the checklist, with type and address). */
  profiles: Profile[];
  /** After a save: the backend's answer and the name the group had. */
  onSaved?: (result: GroupSaveResult, previousName: string) => void | Promise<unknown>;
  /** After a delete: the group's name. Its saved connections stay. */
  onDeleted?: (name: string) => void | Promise<unknown>;
  onClose: () => void;
}) {
  const [name, setName] = useState(group);
  const [members, setMembers] = useState<Set<string>>(
    () => new Set(profiles.filter((p) => (p.group || "").trim() === group).map((p) => p.id)),
  );
  const [confirmDelete, setConfirmDelete] = useState(false);
  const action = useAction(`group-editor:${group}`);
  const remove = useAction(`group-editor-delete:${group}`);

  const trimmed = name.replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 80);
  const ticked = profiles.filter((p) => members.has(p.id));
  const saveDisabled = !trimmed || ticked.length === 0 || ticked.length > 16;

  function toggle(id: string, checked: boolean) {
    setMembers((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function save() {
    if (saveDisabled) return;
    void action
      .run(
        async (signal) => {
          const result = await saveConnectionGroup(trimmed, ticked.map((p) => p.id), group, signal);
          await refreshSavedConnections().catch(() => {});
          await onSaved?.(result, group);
          return result;
        },
        {
          pending: `Saving group ${trimmed}…`,
          done: (result) =>
            result.name === group
              ? `Group ${result.name} saved.`
              : `Group renamed to ${result.name}.`,
        },
      )
      .then((res) => {
        if (res.ok) onClose();
      });
  }

  function drop() {
    void remove
      .run(
        async () => {
          const result = await deleteConnectionGroup(group);
          await refreshSavedConnections().catch(() => {});
          await onDeleted?.(group);
          return result;
        },
        {
          pending: `Deleting group ${group}…`,
          done: () => `Group ${group} deleted. Its saved connections stay.`,
        },
      )
      .then((res) => {
        if (res.ok) onClose();
      });
  }

  if (confirmDelete) {
    return (
      <ConfirmSheet
        key="delete"
        className="group-editor-sheet"
        title={`Delete group ${group}?`}
        description={`Only the group is removed. Its saved connections stay, with their saved passwords; myflames just stops reading them together.`}
        confirmTone="danger"
        confirmLabel="Delete group"
        busy={remove.pending}
        busyLabel="Deleting…"
        onCancel={() => setConfirmDelete(false)}
        onConfirm={drop}
      >
        <ActionStatus state={remove} />
      </ConfirmSheet>
    );
  }

  return (
    <ConfirmSheet
      key="edit"
      className="group-editor-sheet"
      title={`Edit group ${group}`}
      description="A group opens as one Group connection from the connection picker. Connections in the same group may be read together to draw a cluster or replication topology."
      confirmTone="primary"
      confirmLabel="Save group"
      busy={action.pending}
      busyLabel="Saving…"
      confirmDisabled={saveDisabled}
      onCancel={onClose}
      onConfirm={save}
    >
      <label className="field group-editor-name">
        Group name
        <input
          type="text"
          maxLength={80}
          value={name}
          disabled={action.pending}
          aria-label="Group name"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <ul className="group-editor-list" aria-label="Saved connections in this group">
        {profiles.map((p) => {
          const other = (p.group || "").trim();
          return (
            <li key={p.id} className="group-editor-item" data-profile-id={p.id}>
              <Checkbox
                checked={members.has(p.id)}
                busy={action.pending}
                onChange={(checked) => toggle(p.id, checked)}
              >
                <span className="group-editor-item-name">{p.name || connectionTarget(p)}</span>
              </Checkbox>
              <SafetyTags type={p.type === "development" ? "development" : "production"} readOnly={p.read_only === true} />
              <code className="group-editor-addr">{connectionTarget(p)}</code>
              {other && other !== group && (
                <span className="group-editor-move">Moves it from group {other}</span>
              )}
            </li>
          );
        })}
      </ul>
      {ticked.length === 0 && (
        <p className="hint" role="note">A group needs at least one saved connection.</p>
      )}
      {ticked.length > 16 && (
        <p className="hint" role="alert">A group can contain at most 16 saved connections. Uncheck {ticked.length - 16} to save.</p>
      )}
      <div className="group-editor-footer">
        <button
          type="button"
          className="text-button danger group-editor-delete"
          disabled={action.pending}
          onClick={() => setConfirmDelete(true)}
        >
          Delete group…
        </button>
      </div>
      <ActionStatus state={action} />
    </ConfirmSheet>
  );
}
