# Replication, clusters and proxies

Task guides for the Replication, Cluster, ProxySQL and Readyset places. Reads in these places never change a server; the changes below show the exact statements, ask first and are recorded in the Audit log.

- [Skip a failing replication transaction](howto-topology.md#skip-a-failing-replication-transaction)
- [Switch the primary or mode of a cluster](howto-topology.md#switch-the-primary-or-mode-of-a-cluster)
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

1. Open **Cluster** in the sidebar. **Topology** shows the members and their states; **Members** (**Nodes** on Galera and Percona XtraDB Cluster) lists each one; **Routers** appears for InnoDB Cluster.
2. Select an ONLINE secondary member and click **Make primary…** in the inspector; check the statement and confirm. The current primary becomes a read-only secondary once its running transactions finish.
3. To change the mode on the connected member, use **Switch the group to multi-primary…** or **Switch the group to single-primary…** on Group Replication, or **Switch the cluster to multi-primary…** / **Switch the cluster to single-primary…** on InnoDB Cluster, and type the cluster name to confirm.

What you see: the sheet shows the exact statement or MySQL Shell call and checks the cluster again before sending; the topology then shows the new primary or mode.

Good to know:

- InnoDB Cluster changes run through MySQL Shell installed on this computer (use **Get MySQL Shell** in **Settings** > **Client tools** > **Client tools…** if needed; see [Get MySQL Shell](howto-server.md#get-mysql-shell)) and are not offered over an SSH connection, because MySQL Shell connects to the other members directly.
- On MariaDB Galera there is no primary to switch; a node's inspector offers **Desync this node…** and **Resync this node…** to take it out of flow control for a backup and back in, and a member listed as MISSING in an InnoDB Cluster offers **Rejoin to the cluster…**.

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
