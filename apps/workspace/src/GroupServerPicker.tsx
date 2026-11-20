// The Server: control of a Group connection (one connection group opened as
// one connection, api.ts GroupConnection). Single-server places (the Server
// place's views, Tuning, the Query editor) work on one named member at a
// time, starting on the primary or source; this picker chooses that member.
// Segmented buttons up to 4 members in a wide window, a select in a narrow
// one or above 4. Choosing a member is an explicit open of that saved
// connection; members not saved in the group are never listed.
import type { GroupConnection, GroupConnectionMember } from "./api";
import { Segmented, Select } from "./kit";

/** "prod-db-2 · db-2:3306 · SECONDARY": how a group member is named in view
 * headers and the picker (the role comes from the last probe; absent before
 * one). */
export function groupMemberLabel(m: GroupConnectionMember): string {
  const role = (m.role || "").toUpperCase();
  return `${m.profile_name} · ${m.host}:${m.port}${role ? ` · ${role}` : ""}`;
}

export function GroupServerPicker({
  group,
  value,
  onChoose,
  narrow = false,
}: {
  group: GroupConnection;
  /** The profile id of the member the place works on. */
  value: string;
  onChoose: (profileId: string) => void;
  /** Narrow window: the select, never the segmented buttons. */
  narrow?: boolean;
}) {
  const members = group.members;
  return (
    <span className="group-server-picker">
      <span className="group-server-label" aria-hidden="true">Server:</span>
      {!narrow && members.length <= 4 ? (
        <Segmented
          label="Server"
          className="group-server-segmented"
          value={value}
          options={members.map((m) => ({ value: m.profile_id, label: m.profile_name, title: groupMemberLabel(m) }))}
          onChange={onChoose}
        />
      ) : (
        <Select
          label="Server"
          className="group-server-select"
          value={value}
          options={members.map((m): [string, string] => [m.profile_id, groupMemberLabel(m)])}
          onChange={onChoose}
        />
      )}
    </span>
  );
}
