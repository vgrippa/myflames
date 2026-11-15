import { useId, useState } from "react";
import {
  api,
  type Connection,
  type ConnectionGroup,
  type Profile,
} from "./api";
import { connectionKey } from "./binding";
import { ActionButton, ActionStatus, Checkbox, ConfirmSheet, useAction } from "./kit";
import { refreshSavedConnections, useSavedConnections } from "./savedConnections";
import "./topology-presentation.css";

export function setProfilesGroup(
  profile_ids: string[],
  group: string,
  signal?: AbortSignal,
) {
  return api<{ updated: string[]; group: string }>(
    "profiles/group",
    { profile_ids, group },
    signal,
  );
}

const dismissedJoins = new Set<string>();

export function resetDismissedGroupJoinsForTests() {
  dismissedJoins.clear();
}

function typeLabel(type?: string | null): "Development" | "Production" {
  return type === "development" ? "Development" : "Production";
}

function typeClass(type?: string | null): string {
  return `type-tag is-${type === "development" ? "development" : "production"}`;
}

export interface ConnectionGroupBarProps {
  group?: ConnectionGroup | null;
  connection: Connection;
  profiles?: Profile[];
  disabled?: boolean;
  onReload?: () => void | Promise<unknown>;
}

export function ConnectionGroupBar({
  group,
  connection,
  profiles,
  disabled = false,
  onReload,
}: ConnectionGroupBarProps) {
  const saved = useSavedConnections(false);
  const allProfiles = profiles || saved.items;
  // A group grants consent to use saved credentials. Only the explicit id
  // identifies the saved connection the user chose, never its display fields.
  const currentProfileId = connection.profile_id || "";
  const currentProfile = allProfiles.find((p) => p.id === currentProfileId);
  const currentGroupName = (
    group?.name ??
    currentProfile?.group ??
    connection.group ??
    ""
  ).trim();

  const scope = `${currentProfileId || connectionKey(connection)}`;
  const action = useAction(scope);
  const [, setDismissVersion] = useState(0);
  const [sheetMode, setSheetMode] = useState<"review" | "create" | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(() => new Set());
  const [targetGroup, setTargetGroup] = useState("");
  const datalistId = useId();

  const existingGroups = Array.from(
    new Set(
      allProfiles
        .map((p) => (p.group || "").trim())
        .filter((g): g is string => g.length > 0),
    ),
  ).sort((a, b) => a.localeCompare(b));

  const readItems = group?.read || [];
  const readOk = readItems.filter((r) => r.outcome === "read");
  const readProblems = readItems.filter((r) => r.outcome !== "read");
  const suggestions = group?.suggestions || [];
  const visibleJoins = (currentProfileId ? group?.join || [] : []).filter(
    (j) => j.group && !dismissedJoins.has(`${scope}:${j.group}`),
  );

  function dismissJoin(groupName: string) {
    dismissedJoins.add(`${scope}:${groupName}`);
    setDismissVersion((v) => v + 1);
  }

  function handleJoin(groupName: string) {
    if (!currentProfileId) return;
    void action.run(
      async (signal) => {
        const res = await setProfilesGroup([currentProfileId], groupName, signal);
        await refreshSavedConnections().catch(() => {});
        await onReload?.();
        return res;
      },
      {
        pending: `Adding to group ${groupName}…`,
        done: () => `Added to group ${groupName}.`,
      },
    );
  }

  function openReviewSheet() {
    action.reset();
    const target = currentGroupName || (group?.suggested_name || "").trim();
    setTicked(new Set(suggestions
      .filter((s) => !s.group?.trim() || s.group.trim() === target)
      .map((s) => s.id)));
    setTargetGroup(target);
    setSheetMode("review");
  }

  function openCreateSheet() {
    action.reset();
    setTicked(new Set());
    setTargetGroup((group?.suggested_name || currentGroupName || "").trim());
    setSheetMode("create");
  }

  function toggleSuggestion(id: string, checked: boolean) {
    setTicked((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const sheetSuggestions = sheetMode === "review" ? suggestions : [];
  const includeCurrent = !!currentProfileId && !currentGroupName;
  const tickedIds = sheetSuggestions
    .filter((s) => ticked.has(s.id))
    .map((s) => s.id);
  const profileIds = Array.from(
    new Set([
      ...(includeCurrent && currentProfileId ? [currentProfileId] : []),
      ...tickedIds,
    ]),
  );
  const trimmedGroup = targetGroup
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .trim()
    .slice(0, 80);
  const confirmCount = profileIds.length;
  const confirmDisabled =
    !trimmedGroup ||
    profileIds.length === 0 ||
    (sheetMode === "review" &&
      sheetSuggestions.length > 0 &&
      tickedIds.length === 0);

  function handleConfirmSheet() {
    if (confirmDisabled) return;
    void action
      .run(
        async (signal) => {
          const res = await setProfilesGroup(profileIds, trimmedGroup, signal);
          await refreshSavedConnections().catch(() => {});
          await onReload?.();
          return res;
        },
        {
          pending: `Adding ${confirmCount} ${confirmCount === 1 ? "connection" : "connections"} to ${trimmedGroup}…`,
          done: () =>
            `Added ${confirmCount} ${confirmCount === 1 ? "connection" : "connections"} to ${trimmedGroup}.`,
        },
      )
      .then((res) => {
        if (res.ok) setSheetMode(null);
      });
  }

  const currentDisplayName =
    currentProfile?.name ||
    connection.name ||
    `${connection.host}:${connection.port}`;
  const currentAddress = `${connection.host}:${connection.port}`;
  const currentType = currentProfile?.type || connection.type || "production";

  return (
    <div
      className="connection-group-bar"
      role="region"
      aria-label="Connection group"
    >
      {!currentProfileId && (
        <div className="connection-group-line is-quiet" data-state="unsaved-connection">
          <span className="connection-group-summary">
            Save this connection to add it to a group.
          </span>
        </div>
      )}
      {currentGroupName && (
        <div className="connection-group-line" data-state="group-read">
          <div className="connection-group-copy">
            <span className="connection-group-summary">
              {readOk.length > 0
                ? `Group ${currentGroupName} · read ${readOk.map((r) => r.name).join(", ")}`
                : `Group ${currentGroupName}`}
            </span>
            {readProblems.length > 0 && (
              <span className="connection-group-outcomes">
                {readProblems.map((item) => {
                  const verb =
                    item.outcome === "failed" ? "failed" : "not reached";
                  return (
                    <span
                      key={item.id || item.name}
                      className="connection-group-outcome is-problem"
                    >
                      {item.message
                        ? `${item.name} ${verb}: ${item.message}`
                        : `${item.name} ${verb}`}
                    </span>
                  );
                })}
              </span>
            )}
          </div>
        </div>
      )}

      {visibleJoins.map((j) => (
        <div
          key={j.group}
          className="connection-group-line is-join"
          data-state="group-join"
        >
          <div className="connection-group-copy">
            <span>
              {`This connection is part of the topology of group ${j.group}${j.evidence_text ? ` (${j.evidence_text})` : ""}. Add it to ${j.group}?`}
            </span>
          </div>
          <div className="connection-group-actions">
            <ActionButton
              small
              primary
              data-action="join-add"
              busy={action.pending}
              busyLabel="Adding…"
              disabled={!action.pending && (disabled || !currentProfileId)}
              onClick={() => handleJoin(j.group)}
            >
              {`Add to ${j.group}`}
            </ActionButton>
            <button
              type="button"
              className="button small"
              data-action="join-dismiss"
              disabled={action.pending}
              onClick={() => dismissJoin(j.group)}
            >
              Not now
            </button>
          </div>
        </div>
      ))}

      {suggestions.length > 0 && (
        <div className="connection-group-line" data-state="group-suggestions">
          <div className="connection-group-copy">
            <span className="connection-group-summary">
              {`${suggestions.length} saved ${suggestions.length === 1 ? "connection is" : "connections are"} part of this topology`}
            </span>
          </div>
          <div className="connection-group-actions">
            <button
              type="button"
              className="button small"
              data-action="review-group-suggestions"
              disabled={disabled || action.pending}
              onClick={openReviewSheet}
            >
              Review…
            </button>
          </div>
        </div>
      )}

      {currentProfileId && !currentGroupName && suggestions.length === 0 && (
        <div className="connection-group-line is-quiet" data-state="no-group">
          <div className="connection-group-copy">
            <span className="connection-group-summary">
              No connection group: myflames reads only this connection.
            </span>
          </div>
          <div className="connection-group-actions">
            <button
              type="button"
              className="button small"
              data-action="create-group"
              disabled={disabled || action.pending}
              onClick={openCreateSheet}
            >
              Create a group…
            </button>
          </div>
        </div>
      )}

      {!sheetMode && <ActionStatus state={action} />}

      {sheetMode && (
        <ConfirmSheet
          className="connection-group-sheet"
          title={
            sheetMode === "create"
              ? "Create a connection group"
              : currentGroupName
                ? `Add connections to ${currentGroupName}`
                : "Review topology connections"
          }
          description="Connections in the same group may be read together to draw a cluster or replication topology. myflames never reads a saved connection outside the group of the connection in use."
          confirmTone="primary"
          confirmLabel={`Add ${confirmCount} to ${trimmedGroup || "group"}`}
          busy={action.pending}
          busyLabel="Saving…"
          confirmDisabled={confirmDisabled}
          onCancel={() => {
            if (!action.pending) setSheetMode(null);
          }}
          onConfirm={handleConfirmSheet}
        >
          <label className="connection-group-field">
            <span>Group</span>
            <input
              type="text"
              list={datalistId}
              maxLength={80}
              value={targetGroup}
              disabled={action.pending}
              placeholder="Group name"
              onChange={(e) => setTargetGroup(e.target.value)}
            />
          </label>
          <datalist id={datalistId}>
            {existingGroups.map((g) => (
              <option key={g} value={g} />
            ))}
          </datalist>

          <ul
            className="connection-group-list"
            aria-label="Connections for this group"
          >
            {includeCurrent && (
              <li
                className="connection-group-item is-current"
                data-current-connection={currentProfileId}
              >
                <div className="connection-group-item-head">
                  <span className="connection-group-item-name">
                    {currentDisplayName}
                  </span>
                  <span className={typeClass(currentType)}>
                    {typeLabel(currentType)}
                  </span>
                  <code className="connection-group-addr">{currentAddress}</code>
                  <span className="connection-group-current">
                    This connection
                  </span>
                </div>
              </li>
            )}
            {sheetSuggestions.map((s) => (
              <li
                key={s.id}
                className="connection-group-item"
                data-suggestion-id={s.id}
              >
                <div className="connection-group-item-head">
                  <Checkbox
                    checked={ticked.has(s.id)}
                    busy={action.pending}
                    onChange={(checked) => toggleSuggestion(s.id, checked)}
                  >
                    <span className="connection-group-item-name">{s.name}</span>
                  </Checkbox>
                  <span className={typeClass(s.type)}>
                    {typeLabel(s.type)}
                  </span>
                  <code className="connection-group-addr">{s.address}</code>
                  {s.group ? (
                    <span className="connection-group-current">
                      {s.group.trim() !== trimmedGroup
                        ? `Moves it from group ${s.group}`
                        : `Group ${s.group}`}
                    </span>
                  ) : null}
                </div>
                {s.evidence_text && (
                  <div className="connection-group-evidence">
                    {s.evidence_text}
                  </div>
                )}
              </li>
            ))}
          </ul>

          <ActionStatus state={action} />
        </ConfirmSheet>
      )}
    </div>
  );
}
