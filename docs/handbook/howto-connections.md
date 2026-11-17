# Connections and files

Task guides for saved connections, connection groups and moving files over SSH. A saved connection is logged in to only when it is the connection in use or is in the same connection group as the connection in use; the group is your consent.

- [Group saved connections for topology reads](howto-connections.md#group-saved-connections-for-topology-reads)
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
