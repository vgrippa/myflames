# Replication, clusters and proxies

Task guides for the Topology, Replication, Cluster, ProxySQL and Readyset places. Reads in these places never change a server; the changes below show the exact statements, ask first and are recorded in the Audit log.

- [Create a connection group from Home](howto-topology.md#create-a-connection-group-from-home)
- [Read a whole group in Topology](howto-topology.md#read-a-whole-group-in-topology)
- [Change a parameter on every server](howto-topology.md#change-a-parameter-on-every-server)
- [Skip a failing replication transaction](howto-topology.md#skip-a-failing-replication-transaction)
- [Switch the primary or mode of a cluster](howto-topology.md#switch-the-primary-or-mode-of-a-cluster)
- [Run a cluster action on another member](howto-topology.md#run-a-cluster-action-on-another-member)
- [Give an account a missing privilege for a cluster action](howto-topology.md#give-an-account-a-missing-privilege-for-a-cluster-action)
- [Take a ProxySQL backend out of traffic](howto-topology.md#take-a-proxysql-backend-out-of-traffic)
- [Create a Readyset cache for a query](howto-topology.md#create-a-readyset-cache-for-a-query)

## Create a connection group from Home

Use this when you already have saved connections for the servers in a cluster or replication topology and want to group them together on **Home**.

1. Open **Home** in the sidebar.
2. Click **New group…** in the **Connections** header (or in the **Group connections** empty state if no groups exist yet). Alternatively, tick the saved connections you want in the **Saved connections** table below and click **Group (N)…** in the table toolbar.
3. In **New group**, enter a **Group name** (up to 80 characters, distinct from any existing group).
4. Check or uncheck saved connections in the list (1 to 16 connections; a connection already in another group notes `Moves it from group <other>`).
5. Click **Create group**.

What you see: the new group appears under **Group connections** above **Saved connections** on **Home** (and in the **Connection in use** picker), showing its server count, member names and strictest write-safety tags.

Good to know:

- **New group…** pre-ticks the saved connection in use when there is one; **Group (N)…** pre-ticks the saved connections you selected in the table (unsaved connections cannot join a group).
- If creating or saving a group moves a member out of the **Group connection** currently in use, that active Group connection refreshes automatically with its remaining members.
- Click **Open** on the group card under **Group connections** to open it as a **Group connection** and go straight to the **Topology** place.

Related: [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server), [Edit a connection group](howto-connections.md#edit-a-connection-group) and [Read a whole group in Topology](howto-topology.md#read-a-whole-group-in-topology).

## Read a whole group in Topology

Use this to inspect every member of a connection group together (map, member states, lag and configuration drift) without background polling.

1. Open a **Group connection**: click **Open** on a group card on **Home**, pick a group under **Group connections** in **Connection in use**, or switch to **Topology** while a grouped saved connection is in use and click **Open group connection `<group>`**.
2. On **Map** (the default tab), review **Servers** (saved members with role, state and read outcome, plus outside peers under **Not in this group** showing who reported them), **Links** (`from → to`, kind, state, and whether each link is `verified` by server identity or `address match, not verified`), **Clusters** and **Notes**.
3. Switch to **Members** for the summary line (which highlights version drift when members run different versions and counts read, failed and not-reached servers) and the table showing **Server**, **Role**, **State**, **Engine and version**, **Read only**, **Lag**, **Behind** and **Read**. Select any row to inspect channels and jump with **Use in Server views**, **Open in Cluster** or **Open in Replication**.
4. Switch to **Parameters**, **Accounts** or **Plugins** to compare global settings, user accounts and roles, or plugins and MySQL components across the read members. Use **Show** (**Differences** or **All**) and the search box to filter rows. Select a row in **Accounts** to inspect per-member differences and jump to **Users and roles** on any compared server.
5. Click **Refresh** in the toolbar whenever you want a fresh read of the tab on screen.

What you see: each tab sends one bounded read across the saved members only when it first comes on screen for that **Group connection** and when you click **Refresh** (at most 4 logins at once, 30 s per query and 120 s per tab request). Returning to a tab that already read keeps its report without logging in again.

Good to know:

- On **Parameters**, **Differences** hides settings that match across members and hides expected per-node settings (`server_id`, `server_uuid`, addresses, paths, `wsrep_provider_pc_weight`, `wsrep_provider_gmcast_segment`, role-driven `read_only` / `super_read_only`, and multi-primary or Galera `auto_increment_offset`). Choose **All** to show every row, with **Per server** pills marking those per-node settings.
- **Not compared** beneath **Parameters**, **Accounts** and **Plugins** lists saved members left out of that comparison along with the reason: for example ProxySQL Admin or Readyset members (read for identity only), failed logins, members not reached before the 120 s request budget closed, or incomplete inventories (such as more than 1,000 accounts). Truncated or redacted parameter values remain uncomparable with a reason rather than reading as equal.

Related: [Create a connection group from Home](howto-topology.md#create-a-connection-group-from-home), [Open a Group connection and work on any server](howto-connections.md#open-a-group-connection-and-work-on-any-server) and [Connection groups and topology consent](safety.md#connection-groups-and-topology-consent).

## Change a parameter on every server

Use this when you want to roll a global server variable change across 1 to 16 members of a connection group in a safe order under one review. *(Note: the rolling change engine is implemented on the backend; the workspace UI control in **Topology** is scheduled for a later update. Until it lands, inspect drift in **Topology** > **Parameters** and update individual members in **Server** > **Parameters** with the **Server:** picker.)*

1. **The review**: a rolling variable plan selects 1 to 16 members of the current **Group connection** and a mode (`global`, `persist` or `persist_only`; on MariaDB, `persist` and `persist_only` plan `SET GLOBAL` plus the `[mysqld]` option-file line to copy, never `SET PERSIST`). Any **Read-only** member in the group (even an unselected one) blocks planning before any login, and ProxySQL Admin or Readyset members are refused; members already at the requested value are marked unchanged and receive no statement.
2. **The rolling order**: myflames orders members from live topology facts: Group Replication and InnoDB Cluster secondaries run before the primary; Galera and Percona XtraDB Cluster nodes run in `wsrep_node_name` order; asynchronous replication topologies run deepest replicas first and the root source last (and refuse the change if an unselected relay's links cannot be verified); standalone servers run in saved connection name order.
3. **Production confirmation**: if any member in the group is **Production**, the whole **Group connection** is **Production**, so confirming requires typing the exact connection group name.
4. **Execution, Stop and partial results**: servers run one at a time. Before sending to each server, myflames re-checks group membership, group generation, endpoints, server identity, live role or replication parent and the current variable value, stopping before later servers on the first mismatch, refusal or server error. **Stop** prevents the next server from starting without interrupting a statement already sent. A stop or mid-roll failure leaves earlier servers applied and later ones **not sent** (`partial` or `stopped` outcome), and an interrupted write marks that server `unknown` so you can check it before reviewing another change.
5. **The Audit log**: each member in the roll writes its own record in **Settings** > **Audit log** under the shared change set ID, without passwords or raw server SQL errors.

Related: [Read a whole group in Topology](howto-topology.md#read-a-whole-group-in-topology), [Change server parameters and compare two servers](howto-server.md#change-server-parameters-and-compare-two-servers) and [Reviewed changes](safety.md#reviewed-changes).

## Skip a failing replication transaction

Use this when a replica's applier stopped on one transaction you can safely skip, for example a row that already exists.

1. Open **Replication** in the sidebar. **Topology** shows the sources and this replica; **Channels** (**Connections** on MariaDB) lists the thread states and the last error; **Lag** and **GTID** give the positions.
2. Switch to **Recovery** and select the stopped channel to inspect the failing GTID or event and its error message.
3. Click **Skip this transaction…** to skip the one failing transaction, or **Skip until stopped…** to keep skipping each failure until you stop the loop.
4. Check the exact statements in the sheet, then click **Skip transaction** (or **Start skipping**).

![Open Replication, switch to Recovery, select the stopped channel showing error 1062, click Skip this transaction…, review the skip statement in the sheet, and click Skip transaction.](../screenshots/howto-replication-skip.gif)

What you see: the sheet re-reads the failure before it opens and is planned again when the server or the statements changed, so what you confirm is what runs. Afterwards the channel's applier starts again and the view shows it catching up.

Good to know:

- A skipped transaction is missing on the replica: skip only when the data is already there or no longer wanted.
- On MySQL 8.0 the skip uses SET GTID_NEXT and needs SUPER, SYSTEM_VARIABLES_ADMIN, SESSION_VARIABLES_ADMIN or REPLICATION_APPLIER; on MySQL 5.7 and on the counter method, SET GLOBAL sql_slave_skip_counter needs SUPER; on MariaDB, START SLAVE needs REPLICATION SLAVE ADMIN.

## Switch the primary or mode of a cluster

Use this for a planned switchover before maintenance, or to change between single-primary and multi-primary.

1. Open **Cluster** in the sidebar. **Topology** shows the members and their states; **Members** (**Nodes** on Galera and Percona XtraDB Cluster) lists each one; **Routers** appears for InnoDB Cluster. Below the summary line, a sentence says which server this connection reaches (`You are connected to <member>…`), and that server is marked **This connection** on its card, in the table and in its inspector.
2. To move the primary to another server, select an `ONLINE` secondary member on **Topology** or in **Members** to open its inspector, click **Make primary…**, check the statement or MySQL Shell call in the sheet, and click **Make `<member>` primary**. The current primary becomes a read-only secondary once its running transactions finish.
3. To change the mode of the cluster or run an InnoDB ClusterSet change, scroll to **Changes · the whole cluster** on the **Cluster** overview below the tab content, click **Switch to multi-primary…** or **Switch to single-primary…** (or in a ClusterSet, **Make `<cluster>` the primary cluster…**, **Fail over to this cluster…** or **Rejoin `<cluster>` to the ClusterSet…**), type the cluster or member name to confirm, and click **Switch to multi-primary** or **Switch to single-primary**.

![Open Cluster, switch to Members, select an ONLINE secondary member, click Make primary… in the inspector, review the group_replication_set_as_primary statement, confirm, and close the result sheet.](../screenshots/howto-cluster-primary.gif)

![In Cluster, view cluster-wide actions under Changes · the whole cluster on the overview and member actions in a member's inspector, showing disabled actions with their reason and fix in plain words beneath them.](../screenshots/howto-cluster-reasons.gif)

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

![In Cluster, find a disabled action under Changes · the whole cluster or in a member's inspector whose reason names a missing privilege, and click Copy beside its GRANT statement to copy the statement to run as an administrator before clicking Refresh.](../screenshots/howto-cluster-grant.gif)

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

![Open ProxySQL, switch to Servers, select a backend, click Set OFFLINE_SOFT… in the inspector, review the ProxySQL statements, confirm, and close the outcome sheet.](../screenshots/howto-proxysql-offline.gif)

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

![Open Readyset, switch to Proxied, select a proxied query, click Create cache…, wait for Check support (EXPLAIN) to show Shallow: Supported, click Review CREATE CACHE…, confirm Create shallow cache, and close the outcome sheet.](../screenshots/howto-readyset-cache.gif)

What you see: the cache is created and appears on the **Caches** tab with its type and status; matching queries stop being proxied to the upstream for reads the cache serves.

Good to know:

- The support check runs EXPLAIN CREATE CACHE as a dry run; when one cache type is unsupported the sheet says why and pre-selects the other.
- A new cache whose query collides with an existing cache replaces it; the sheet names the collision before you confirm. The **Caches** tab's **Export SQL** exports the cache definitions as DDL.
