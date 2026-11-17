# Connections and files

Task guides for saved connections, connection groups and moving files over SSH. A saved connection is logged in to only when it is the connection in use or is in the same connection group as the connection in use; the group is your consent.

- [Group saved connections for topology reads](howto-connections.md#group-saved-connections-for-topology-reads)
- [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server)
- [Edit a connection group](howto-connections.md#edit-a-connection-group)
- [Move files between your computer and a server](howto-connections.md#move-files-between-your-computer-and-a-server)
- [Import saved connections from MySQL Workbench or DBeaver](howto-connections.md#import-saved-connections-from-mysql-workbench-or-dbeaver)

## Group saved connections for topology reads

Use this when Replication, Cluster, ProxySQL or Readyset should draw the whole environment, not only the server you connected to: myflames may read other saved connections, but only ones in the same group as the connection in use.

1. Open **Replication**, **Cluster**, **ProxySQL** or **Readyset**. The group bar sits above **Topology** and says how cross-server reads are scoped.
2. When the bar says "No connection group: myflames reads only this connection.", click **Create a group…**. When it says saved connections are part of this topology, click **Review…** to see them, pre-ticked, with the evidence for each.
3. In the **Create a connection group** sheet, name the **Group**: the connection in use joins it (marked "This connection"). To add other saved connections, open **Review…** from the bar instead and tick the ones that belong to this environment.
4. Click **Add N to <group>**.

What you see: the place re-reads and draws the full topology, and the group bar lists which group members were read, which were not reached and which failed. When the connection you opened belongs to another group's topology, the bar instead offers **Add to <group>** and **Not now**.

Good to know:

- The group is the consent. myflames logs in with saved passwords only to saved connections in the same group as the connection in use, never to the connection in use itself, and to nothing when the connection in use is unsaved or has no group. Removing a connection from the group stops those reads.
- Suggestions are found without logging in to other servers, from identities remembered when you opened a connection and from addresses the open server reports.
- You can also set or change a connection's group directly: open **Home**, select the connection, edit its **Group** field and click **Save connection**.

Related: [Connection groups and topology reads](safety.md#connection-groups-and-topology-consent).

## Open a Group connection and work on any server

Use this when your saved connections belong to one cluster or replication group and you want to open the group once, then pick which server each view or query tab works on.

1. Open the **Connection in use** picker in the top bar.
2. Under **Group connections**, click the group you want to open (each row shows its server count and write-safety tags).
3. Open a single-server place such as **Server**, **Workload**, **Tuning**, **Database diagram** or **Query editor**.
4. In the **Server:** picker in the view header or the **Query editor** tab bar, keep the default server (it starts on the primary or replication source, or the first member when none is marked primary or source) or choose another member of the group.
5. In **Query editor**, click **+** (**New query tab**) to open a tab bound to the member chosen in **Server:**; each tab shows ` · <connection>` after its name and keeps running on that server even if you later change **Server:** for new tabs.

What you see: the top bar and window status bar show `Group connection <group> · <N> servers` under the strictest write safety of the group's members, and every single-server view header shows **Server:** alongside the member it reads (`<connection> · <host>:<port> · <ROLE>` when the role is known). **Server** views and **Query editor** keep their own **Server:** choice while the group stays open.

Good to know:

- The strictest write safety wins across the group: if any member in the group is **Production**, the whole **Group connection** is **Production** and asks for confirmation before every change, even when you work on a **Development** member. The group is **Development** only when every member is **Development**. A member with **Read-only** turned on still refuses every change on that server, and the group row shows **Read-only** when every member is read-only.
- Only saved connections that belong to the group appear in **Server:**. Other servers seen in the cluster or replication topology are never listed or logged in to until you save them and add them to the group.
- A **Group connection** lives in memory for the current window. If you switch to a single connection or reopen the app (which restores the active member as a plain connection), a **Query editor** tab that was bound to a group member shows `This tab ran on <member>, which is not open now.` and refuses **Run**, **Run all**, **Explain** and **Run analysis** until you open the **Group connection** again or click **Use this connection** to run the tab on the connection in use.

Related: [Edit a connection group](howto-connections.md#edit-a-connection-group), [Connection groups](server.md#connection-groups) and [Connection groups and topology consent](safety.md#connection-groups-and-topology-consent).

## Edit a connection group

Use this to rename a connection group, add or remove saved connections, or delete the group without deleting its saved connections.

1. Open the group editor from any of three places: click **Edit group…** beside the group under **Group connections** in the **Connection in use** picker, click **Edit group…** in the window status bar while a **Group connection** is open, or open **Home** and click **Edit group…** beside the group under **Group connections** above the **Connections** table.
2. In **Edit group `<group>`**, edit **Group name** if you want to rename the group.
3. Check or uncheck saved connections in the list to add or remove members (a connection that belongs to another group notes `Moves it from group <other>`), then click **Save group**. To add topology suggestions with the matching servers pre-ticked and their evidence shown, open **Replication**, **Cluster**, **ProxySQL** or **Readyset**, click **Review…** in the connection group bar above **Topology**, and click **Add `<N>` to `<group>`**.
4. To remove the group itself, click **Delete group…** at the bottom of **Edit group `<group>`**, then click **Delete group** in **Delete group `<group>`?**.

What you see: saving updates the group at once across the picker, **Home** and any open **Group connection**. Deleting the group removes only the group; its saved connections and their saved passwords stay on this computer, and if the group was open, its active member stays in use as a plain connection.

Good to know:

- A group must contain between 1 and 16 saved connections: unchecking every connection (`A group needs at least one saved connection.`), checking more than 16 (`A group can contain at most 16 saved connections. Uncheck <N> to save.`), or leaving **Group name** empty keeps **Save group** disabled, and a name already used by another group is refused.
- When you remove a member that **Server**, **Workload**, **Tuning** or **Database diagram** was viewing, those open views move to the group's primary or source and show `<member> is not in this group any more; its views moved to <primary>.` in the header. If you remove the active member of the **Group connection**, the **Group connection** moves to the remaining primary or first member.
- In **Query editor**, a tab bound to a removed member does not switch servers on its own: it shows `<member> is not in this group any more. Pick a server for this tab to run it.` and refuses **Run**, **Run all**, **Explain** and **Run analysis** until you choose a remaining member in **Pick a server…** (if the tab already had an open session, **Stop** and **Roll back** still work so you can release an open transaction).

Related: [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server), [Group saved connections for topology reads](howto-connections.md#group-saved-connections-for-topology-reads) and [Connection groups and topology consent](safety.md#connection-groups-and-topology-consent).

## Move files between your computer and a server

Use this to copy logs, dumps or configuration files between this computer and a server over SSH, without giving myflames any new credentials.

1. Open **File transfer** in the sidebar.
2. Click **New SSH connection** (or **New** above the **SSH connections** list when you already have some).
3. Fill **Host**, **Port** and **User**, or pick a **Start from** entry to copy them from a saved database connection or a ~/.ssh/config alias. Choose the sign-in under **Authentication**.
4. Click **Test connection**, then **Save**.
5. Select the connection in the list. On the first connection to a server, compare the fingerprint under **Trust this server?** and click **Trust and connect**.
6. Select files in **This computer** or in the server pane and click **Upload** or **Download**, or drag rows from one pane to the other.
7. Watch progress in the **Transfers** panel below the panes.

What you see: the two panes show this computer and the server side by side; each transfer lists its progress, rate and time left when known, with **Cancel** while it runs and **Retry** after a failure.

Good to know:

- File transfer uses your own SSH config, keys and agent; it does not use the database connection in use.
- On Windows, uploads and downloads are not available yet.

Related: [File transfer and SSH](files.md).

## Import saved connections from MySQL Workbench or DBeaver

Use this to bring connections you already keep in another client into myflames, instead of typing them again.

1. Open **Home** in the sidebar.
2. In the Connections header, open the menu (the ⋯ button) and choose **Import from MySQL Workbench or DBeaver…**.
3. In **From**, choose **MySQL Workbench** or **DBeaver**.
4. Review the preview: each connection's name, endpoint, default database and SSH settings, with a note where something needs attention.
5. Tick the connections to import and click **Import N connections**.

What you see: the imported connections appear in the Connections table, ready to open; passwords are asked when you first connect.

Good to know:

- The import reads only Workbench's connections.xml or DBeaver's data-sources.json from their standard locations. Password stores and SSH keys are never read, so review TLS and SSH settings before connecting.
- Imported connections start with no connection group: nothing logs in to them until you open one yourself or group it deliberately.
