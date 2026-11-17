# Replication, clusters and proxies

Task guides for the Replication, Cluster, ProxySQL and Readyset places. Reads in these places never change a server; the changes below show the exact statements, ask first and are recorded in the Audit log.

- [Skip a failing replication transaction](howto-topology.md#skip-a-failing-replication-transaction)
- [Switch the primary or mode of a cluster](howto-topology.md#switch-the-primary-or-mode-of-a-cluster)
- [Run a cluster action on another member](howto-topology.md#run-a-cluster-action-on-another-member)
- [Give an account a missing privilege for a cluster action](howto-topology.md#give-an-account-a-missing-privilege-for-a-cluster-action)
- [Take a ProxySQL backend out of traffic](howto-topology.md#take-a-proxysql-backend-out-of-traffic)
- [Create a Readyset cache for a query](howto-topology.md#create-a-readyset-cache-for-a-query)

## Skip a failing replication transaction

Use this when a replica's applier stopped on one transaction you can safely skip, for example a row that already exists.

1. Open **Replication** in the sidebar. **Topology** shows the sources and this replica; **Channels** (**Connections** on MariaDB) lists the thread states and the last error; **Lag** and **GTID** give the positions.
2. Switch to **Recovery** and select the stopped channel to inspect the failing GTID or event and its error message.
3. Click **Skip this transaction…** to skip the one failing transaction, or **Skip until stopped…** to keep skipping each failure until you stop the loop.
4. Check the exact statements in the sheet, then click **Skip transaction** (or **Start skipping**).

What you see: the sheet re-reads the failure before it opens and is planned again when the server or the statements changed, so what you confirm is what runs. Afterwards the channel's applier starts again and the view shows it catching up.

Good to know:

- A skipped transaction is missing on the replica: skip only when the data is already there or no longer wanted.
- On MySQL 8.0 the skip uses SET GTID_NEXT and needs SUPER, SYSTEM_VARIABLES_ADMIN, SESSION_VARIABLES_ADMIN or REPLICATION_APPLIER; on MySQL 5.7 and on the counter method, SET GLOBAL sql_slave_skip_counter needs SUPER; on MariaDB, START SLAVE needs REPLICATION SLAVE ADMIN.

## Switch the primary or mode of a cluster

Use this for a planned switchover before maintenance, or to change between single-primary and multi-primary.

1. Open **Cluster** in the sidebar. **Topology** shows the members and their states; **Members** (**Nodes** on Galera and Percona XtraDB Cluster) lists each one; **Routers** appears for InnoDB Cluster. Below the summary line, a sentence says which server this connection reaches (`You are connected to <member>…`), and that server is marked **This connection** on its card, in the table and in its inspector.
2. To move the primary to another server, select an `ONLINE` secondary member on **Topology** or in **Members** to open its inspector, click **Make primary…**, check the statement or MySQL Shell call in the sheet, and click **Make `<member>` primary**. The current primary becomes a read-only secondary once its running transactions finish.
3. To change the mode of the cluster or run an InnoDB ClusterSet change, scroll to **Changes · the whole cluster** on the **Cluster** overview below the tab content, click **Switch to multi-primary…** or **Switch to single-primary…** (or in a ClusterSet, **Make `<cluster>` the primary cluster…**, **Fail over to this cluster…** or **Rejoin `<cluster>` to the ClusterSet…**), type the cluster or member name to confirm, and click **Switch to multi-primary** or **Switch to single-primary**.

What you see: every action for the cluster kind stays visible on the overview under **Changes · the whole cluster** or in each member's or node's inspector (**Make primary…**, **Stop Group Replication…**, **Start Group Replication…** and **Rejoin to the cluster…**, or on MariaDB Galera **Desync this node…**, **Resync this node…** and **Bootstrap a Primary component…**). Any action that cannot run now stays visible and disabled with the reason and the fix in plain words under it. After a confirmed change runs, the **What changed** table shows the state before and after, explaining in plain words why any value was not reported instead of saying "Not readable".

Good to know:

- InnoDB Cluster mode switches, member rejoin (**Rejoin to the cluster…**) and ClusterSet changes (**Make primary cluster…**, **Fail over to this cluster…** and **Rejoin cluster to the ClusterSet…**) run through MySQL Shell installed on this computer (use **Get MySQL Shell** in **Settings** > **Client tools** > **Client tools…** if needed; see [Get MySQL Shell](howto-server.md#get-mysql-shell)) and stay disabled over an SSH connection, because MySQL Shell connects to the other members directly outside the tunnel.
- Mode switches are refused inside an InnoDB ClusterSet, on an InnoDB Cluster unless every member is `ONLINE`, on a read replica, or for **Switch to multi-primary…** while `group_replication_preemptive_garbage_collection` is `ON`. On MariaDB Galera there is no primary to switch; a node's inspector offers **Desync this node…**, **Resync this node…** and **Bootstrap a Primary component…**.

Related: [Run a cluster action on another member](howto-topology.md#run-a-cluster-action-on-another-member), [Cluster](server.md#cluster) and [Reviewed changes](safety.md#reviewed-changes).

## Run a cluster action on another member

Use this when you want to stop or start Group Replication, desync or resync a Galera node, or run another member action on a server other than the one you first connected to.

1. Open **Cluster** in the sidebar and select the other member or node on **Topology** or in **Members** (**Nodes** on Galera) to open its inspector.
2. When a **Group connection** is open (see [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server)), the line under the summary says `Each member's actions run through its own saved connection in <group>.`. Click the action in the member's inspector (such as **Stop Group Replication…**, **Start Group Replication…**, **Desync this node…** or **Resync this node…**). If the member was matched only by its `host:port` address, a note under the button says `Review checks that <connection> (<host:port>) is this member.`; on a remote MariaDB Galera node, **Desync this node…** and **Resync this node…** note `Review checks wsrep_desync and the state on this node's own connection.`.
3. In the review sheet, verify **Target** and **Runs on** (which name the member's saved connection `<connection> (<host:port>)`), **Server UUID** or **Galera node UUID**, and **Listed in the cluster as**. Type the saved connection's name if the action asks for confirmation, then confirm.
4. When you are using a single connection instead of a **Group connection**, local member actions on other servers stay disabled because they run on the server they change (`<Action> runs on the server/node it changes, <member>; this connection is to <current>.`). When a saved connection on this computer matches that member's address, click **Use `<connection>`** under the disabled action in the inspector (or on the member's card in **Topology**) to switch to that server and run the action there.

What you see: in a **Group connection**, clicking the action logs in to that member's saved connection in the group, verifies that its live `server_uuid` (or `wsrep_gcomm_uuid` on Galera) matches the selected member and that the member is local to that session, and opens the review sheet bound to that saved connection. Without a **Group connection**, **Use `<connection>`** switches the active connection to that server and reloads **Cluster** from it.

Good to know:

- In a **Group connection**, **Rejoin to the cluster…** on a `MISSING`, `OFFLINE` or `ERROR` InnoDB Cluster member is the exception: it runs through the active `ONLINE` member with quorum (using MySQL Shell) and names the missing member in **Instance**, so it does not need a saved connection for the missing server.
- A member action in a **Group connection** stays disabled when no saved connection in the group can run it safely: if no saved connection in the group matches the member, it says `Add a saved connection for <member> to this group.` and offers **Edit group…**; if unconfirmed connections exist in the group, it asks you to open the one that reaches that member on its own and view **Cluster** once to confirm it; if the matching connection has no saved password, it asks you to save its password first; and if two saved connections in the group reach the same member (for example a direct connection and a MySQL Router or ProxySQL connection), it names both and offers **Use `<connection>`**.
- If the member's saved connection reaches a different server when planning starts, or if the connection leaves the group before you confirm, myflames refuses the action with nothing sent.

Related: [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server), [Edit a connection group](howto-connections.md#edit-a-connection-group) and [Cluster](server.md#cluster).

## Give an account a missing privilege for a cluster action

Use this when a cluster action is disabled because the connected account (or the target member's saved connection in a group) lacks a required MySQL or MariaDB privilege.

1. Open **Cluster** in the sidebar and find the disabled action under **Changes · the whole cluster** on the overview or in the selected member's inspector.
2. Read the reason under the disabled button: it names the missing privilege (for example `Needs GROUP_REPLICATION_ADMIN or SUPER.` on MySQL or `Needs SUPER.` on MariaDB Galera) and says `Run the statement below as an administrator, then Refresh.`.
3. Click **Copy** beside the `GRANT` statement box under the reason to copy the exact `GRANT` built for that session's `CURRENT_USER()` account.
4. Grant the privilege as an administrator on that server: either switch to an administrator connection and run the copied `GRANT` in **Query editor** (or grant the privilege in **Server** > **Users and roles** under **Grants** and click **Review changes**), or run it in another SQL client.
5. Return to **Cluster** on the original connection and click **Refresh** in the toolbar.

What you see: after **Refresh**, myflames re-reads `SHOW GRANTS` for the account that runs the action; once the required privileges are present, the action button becomes enabled so you can click it to review and confirm the change.

Good to know:

- myflames never runs the suggested `GRANT` statement as part of a cluster action. For InnoDB Cluster mode switches and replica-cluster switchover through MySQL Shell, the suggestion includes both the global privileges and the `mysql` and `mysql_innodb_cluster_metadata*` schema privileges with `WITH GRANT OPTION`, escaping `_` and `%` in schema names when `partial_revokes` is `OFF`.
- When `SHOW GRANTS` or `CURRENT_USER()` could not be read or verified, myflames does not disable an otherwise eligible action. Instead, it shows `Privileges could not be read; the server refuses the change if one is missing.` under the action (and in the review sheet's warnings), and the server itself enforces the privilege check when the statement runs.
- For MySQL Shell actions whose privileges MySQL Shell checks only at runtime (**Rejoin to the cluster…**, **Make `<cluster>` the primary cluster…**, **Fail over to this cluster…** and **Rejoin `<cluster>` to the ClusterSet…**), the action notes `MySQL Shell checks the privileges it needs when it runs.`. If a schema-scoped `GRANT` needs wildcard escaping and `partial_revokes` could not be read in the session, myflames omits the `GRANT` box and asks you to **Refresh** and try again.

Related: [Create a database user and edit its grants](howto-server.md#create-a-database-user-and-edit-its-grants), [Users and roles](server.md#users-and-roles) and [Cluster](server.md#cluster).

## Take a ProxySQL backend out of traffic

Use this to drain a backend before restarting it, or to check where the query rules send each statement.

1. Open **ProxySQL** in the sidebar, connected to the ProxySQL Admin interface (port 6032 by default).
2. Check **Topology**, **Servers**, **Hostgroups**, **Query rules** and **Query digests**.
3. In **Servers**, select the backend and click **Set OFFLINE_SOFT…** in the inspector; **Change weight…** shifts traffic gradually instead.
4. Review the statements and confirm: the change edits the memory copy and loads it to runtime in one reviewed step.
5. On the **Configuration of servers** line, click **Save servers to disk…** so the change survives a ProxySQL restart.
6. Open **Query digests** to see which rule evaluated each statement and where it was routed.

What you see: an OFFLINE_SOFT backend gets no new traffic while queries and transactions already on it finish; the Servers list shows it out of the runtime pool.

Good to know:

- ProxySQL keeps three copies of its configuration (memory, runtime and disk). Save to disk is a separate, separately reviewed change; the result of a change offers it as the next step.
- **Set OFFLINE_SOFT…** is not offered for a server a monitor manages (for example the Galera monitor), because the monitor would put it back in traffic within seconds.

## Create a Readyset cache for a query

Use this when a frequent proxied query should be served from Readyset instead of the upstream database.

1. Open **Readyset** in the sidebar and select **Proxied** to see the uncached queries observed passing through Readyset.
2. Select a query and click **Create cache…** in the inspector.
3. In the **Configure Readyset cache** sheet the support check runs as soon as the sheet opens with the query pre-filled; the **Check support (EXPLAIN)** button re-runs it after you edit the query. Read the **Shallow** and **Deep** verdicts with their reasons.
4. Choose the **Cache type**: **Shallow cache (TTL & refresh)** or **Deep cache (streaming dataflow)**. For a shallow cache, set **TTL (seconds)** and optional refresh and coalesce values.
5. Click **Review CREATE CACHE…**, check the exact statement and confirm.

What you see: the cache is created and appears on the **Caches** tab with its type and status; matching queries stop being proxied to the upstream for reads the cache serves.

Good to know:

- The support check runs EXPLAIN CREATE CACHE as a dry run; when one cache type is unsupported the sheet says why and pre-selects the other.
- A new cache whose query collides with an existing cache replaces it; the sheet names the collision before you confirm. The **Caches** tab's **Export SQL** exports the cache definitions as DDL.
