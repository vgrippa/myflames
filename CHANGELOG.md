# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### New features

- Adds **Group connections** in the connection picker, opening a connection group under the strictest safety of its members with a **Server:** picker in single-server places and the Query editor that starts on the primary or source and names the server in every view header and query tab. **Edit group…** in the picker, the status bar and Home's **Connections** place renames the group, adds or removes saved connections, or deletes the group while keeping its saved connections; removing a member moves open views to the primary. In **Cluster**, member actions in a Group connection run through that member's saved connection and name that saved server in the review, or, when no saved connection in the group is confirmed as that member, say why with **Edit group…** beside the reason. **Rejoin to the cluster…** runs through the active ONLINE member with quorum and names the missing instance.
- Settings > Appearance now has a Text group: choose the interface font and text size (12 to 16 px) and the code font and text size (11 to 20 px) for the Query editor and all SQL and code text, with a live preview and a Restore default text settings button. The Inter and JetBrains Mono fonts are included with the app, and fonts already installed on the computer are offered too.
- **Settings > Client tools…** can now get MySQL Shell for you: when no usable MySQL Shell is found on this computer, **Get MySQL Shell** downloads Oracle's pinned MySQL Shell 9.7.0 build from Oracle into myflames's data folder (nothing is redistributed by myflames), with a progress bar, a Stop button and checksum verification before anything runs. The downloaded copy is detected immediately and used in preference to other MySQL Shell installations.

### Improvements

- The workspace loads Plans and Learn on demand, reducing the code needed to open the first screen (the entry chunk goes from 508,046 to 479,634 bytes).
- The installed version moved from the window status bar to the top of the sidebar beside the myflames name; choosing it opens **Settings > Updates and news**, which now holds the update status, **Check for updates** and **View releases**. When an update is available, downloading or ready, the badge beside the logo alerts you and names the new version. On the compact icon rail the version text hides and the logo's tooltip names the version.
- The Cluster, Replication, ProxySQL and Readyset topologies have a new three-dimensional look: the diagram sits on a lit stage, servers float above it, and the group's line lights up only when every member is healthy. The Depth and Pause motion buttons are gone; motion still stops with your system's reduced motion setting, and each place's ⓘ explains what it means.
- The Query editor toolbar has **Find** and **Find and replace** buttons, and an **Editing actions** menu lists every editing command with its shortcut, including **Toggle Tab moving focus**.

- **Cluster** now keeps every action for the cluster kind visible on the Cluster overview (**Switch to single-primary…**, **Switch to multi-primary…**, and ClusterSet switchover, failover and rejoin) and in each member's or node's inspector (**Make primary…**, **Stop Group Replication…**, **Start Group Replication…** and **Rejoin to the cluster…**, or on MariaDB Galera **Desync this node…**, **Resync this node…** and **Bootstrap a Primary component…**). Actions that cannot run now stay disabled with the reason and fix in plain words under them, and when the connection's account lacks a privilege they show the exact `GRANT` statement for that account to copy ("Run it as an administrator, then Refresh.").

- **Cluster** now shows InnoDB ClusterSet topology when connected to an InnoDB Cluster read replica, reading the replica's replicated metadata copy and noting that it may be behind the primary cluster. After a Cluster action runs, the before-and-after table states why any value was not reported in plain words instead of saying "Not readable".

### Security fixes

- In a Group connection, **Users and roles**, Kill, Variables and replication skips re-check group membership and the group's strictest write policy at every login. A removed member or a changed policy refuses the earlier review, including when a password was typed. Every review expires if the group is deleted and recreated, even with the same name and members.
- Opening a Group connection re-checks both the active member and each probed member before every login. Removing either member, changing its saved address or recreating the group prevents later logins under the old consent. A stored server identity can no longer be overwritten by an older in-flight or cached probe.
- Query editor Stop and Close confirm the server on the connection sending `KILL QUERY` before interrupting a statement. If the server cannot be confirmed, no KILL is sent. A tab whose member left its group can still roll back, stop and close its existing session to release locks, while new statements stay refused.
- Query editor Stop and Close on MariaDB Galera also confirm the node UUID, preventing an interrupt from reaching another node with the same server name, id and port.

### Bug fixes

- Query editor tabs open when optional server identity variables cannot be read, including MariaDB with custom version strings. If the required identity is unavailable, Stop and Close report that the server could not be confirmed before sending an interrupt.
- Users and roles returns a generated password when MariaDB accepted it but a later identity check prevented unlocking the new account. The password is shown once and is never recorded in the audit.
- MySQL Shell exception messages containing text such as `at STDIN:1` no longer supply a false error location. An error without Shell's own call-site line keeps an unknown outcome and still reads the cluster for visible partial changes.

- In a Group connection, a running replication skip loop no longer disappears when the Server: control shows another member: the banner stays, names the server the loop runs on, and Stop still reaches it, and the sidebar's loop badge stays while the loop runs.

- **Cluster**: on a Group connection the cluster-wide actions now offer **Edit group…** and the group-limited **Use <connection>** link like the member actions, and a topology card's Use suggestion names only the group's members. Testing or saving a connection from Home no longer sends the stored group to the server.

- Cluster, **Users and roles**, Kill, Variables and replication skip actions whose identity check cannot run now report the server's reason and record that the identity was unavailable. A different server still reports an identity mismatch.

- MySQL Shell errors without a statement location keep an unknown outcome. Only the final stderr location and its adjacent exception supply a statement error; progress and earlier echoed text cannot turn it into an identity refusal or change its error number. A read after the error reports any visible partial change.

- Cluster GRANT suggestions escape backslashes and schema wildcards on MySQL 5.7, MySQL before 8.0.16 and MariaDB, where schema grants always use wildcard matching. Privilege reads avoid Galera status unless a node UUID is needed and work when MariaDB has a custom version string.

- Cluster actions require MySQL Shell 8.0.32 or later to read their private credentials file. Older installations show the version found and offer **Get MySQL Shell**. Rejoin requires metadata 2.0.0 or later and stays disabled unless the cluster's ClusterSet status can be verified as exactly OK.

- Rejoining a missing InnoDB Cluster member in a Group connection now uses the active ONLINE member with quorum. Desync and Resync for another Galera node both stay available for review, which checks that node's own state.

- Cluster actions in a Group connection can review a member through a unique matching saved address. Review confirms that connection's server identity before allowing the change, without extra background logins. Group topology reads also remember identities confirmed by existing probes. Query editor tabs refuse runs after their member leaves the group, and refused Cluster actions keep the group's safety type in the audit.

- Fixed Save investigation becoming unavailable when Plans could not load, a delayed notebook taking focus from another dialog, and files dropped into Plans being ignored while it loaded. A chart that cannot load now leaves the rest of Plans available.
- Fixed an issue where long version labels could push the update badge outside the sidebar. Update alerts remain visible when the sidebar is hidden, and screen readers announce a new update and when it is ready.
- Fixed an issue where choosing the version while **Updates and news** was already open did not move keyboard focus to its heading. Empty status bars are skipped by Tab.
- Fixed an issue where the workspace could fail to load a view or the Query editor under load, leaving the page stuck on Loading. The local server now queues connection bursts instead of refusing them; when every worker is busy it answers with a plain "try again" response, which the workspace retries on its own while opening and otherwise shows as an error with a **Try again** button.
- Fixed an issue where the table data editor's review button read **Review 1 changes** for a single change.
- Fixed an issue where myflames could fail to open, with **Could not open myflames: The Python backend did not become ready in time.**, on a computer where DNS lookups are slow. The workspace no longer looks up a host name for its local address when it starts.

### Security fixes

- Cluster changes through MySQL Shell check the global session's server UUID, then the group UUID reported by the returned AdminAPI Cluster object. ClusterSet actions compare the returned set's name and every member cluster's group UUID with the reviewed metadata. The action uses that checked object. Galera SQL identity guards also compare the node UUID, so matching server names, ids and ports cannot authorize a different node.

- Suggested Cluster GRANTs read the account, grants and schema wildcard setting in one login and refuse changed identity snapshots. Schema names containing backslashes, underscores or percent signs are escaped according to that setting, preventing a suggestion from granting access to other matching schemas.
