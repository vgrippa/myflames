# Connection policies & write safety

Reading a server in myflames never changes server state. When you run SQL or trigger an operational action that changes a server, the connection's **Type** and **Read-only** setting decide whether myflames asks first, runs the statement, or refuses it before connecting.

## Connection types and Read-only

Every saved connection in **Home** > **Connections** has a **Type** (**Production** or **Development**) and an independent **Read-only** checkbox:

| Setting | Behavior |
| --- | --- |
| **Production** (default **Type**, purple) | Asks for confirmation before every change myflames sends |
| **Development** (**Type**, teal) | Runs session changes and keyed `INSERT`, `UPDATE` and `DELETE` writes in **Query editor**, and asks before risky changes such as DDL, `TRUNCATE`, `KILL`, `SET GLOBAL`, `SET PERSIST`, `UPDATE` or `DELETE` without a key `WHERE` clause, account or grant changes, replication skips, `LOAD DATA` and `CLONE` |
| **Read-only** (checkbox on either **Type**) | Refuses every change before connecting, and starts query sessions with `SET SESSION TRANSACTION READ ONLY` so stored functions cannot modify tables |

**Development** is not a read-only mode. Every unsaved connection is judged as **Production**, and editing a saved **Development** connection's host, port, user, TLS or SSH settings switches it back to **Production** until you choose **Development** again.

The active policy stays visible in a colored strip along the top edge of the window, in words on the left side of the window status bar, in the **Connection in use** pill, and in the header band of every write-review sheet (including `via <ssh_host>` when connected through SSH). While saved connection settings are still loading or cannot be read, the status bar reports **Production assumed** and keeps write actions disabled.

## Reviewed changes

Every protected change in **Server**, **Workload**, **Replication**, **Cluster**, **ProxySQL**, **Readyset**, **Maintenance** and **Import CSV**, as well as every confirmed statement in **Query editor**, opens a review sheet before anything is sent. The sheet shows the exact SQL or MySQL Shell AdminAPI statements (along with any identity check sent first), the target server and object identity, the connection **Type**, operational warnings and any undo step. Destructive actions require typing the target name to confirm.

Reviewed SQL is executed SQL: confirming binds a review token to the exact statements, connection policy and target identity. Right before sending, myflames re-reads the saved connection policy and checks the live target again; if the policy or the target changed after you opened the sheet, nothing is sent. For Readyset, that live re-read runs in a separate login from the send, so a target switch between the re-read and the send is not caught. If the connection drops or times out after statements were sent, myflames marks the result **Outcome unknown** and never retries the write automatically.

Cluster SQL changes check the server identity in the session that sends the SQL. Galera checks the node's UUID as well as its server details. MySQL Shell first checks its global session's server UUID, then checks the group UUID of the Cluster object it obtains through AdminAPI. For ClusterSet actions it compares the set's name and every member cluster's group UUID with the reviewed metadata. A mismatch refuses the action. The action uses that checked object; AdminAPI manages its own connections to members. ProxySQL changes (on ProxySQL 2.4.0 and later) check the instance's persisted UUID from `disk.global_settings` in the same Admin login before the reviewed statements run; connect directly to ProxySQL Admin (or over SSH), not through a statement-routing proxy, because Admin runs the identity check and the change as separate statements in one session.

MySQL Shell Cluster actions require MySQL Shell 8.0.32 or later; rejoining a member needs 8.1.0 or later and cluster metadata 2.0.0 or later. **Rejoin to the cluster…** stays disabled when the cluster's ClusterSet status cannot be verified as exactly `OK`. Check the status and perform the rejoin in MySQL Shell when the app cannot establish it.

Privilege checks read `SHOW GRANTS` as a hint. When myflames cannot prove from `SHOW GRANTS` that an action is permitted, it reports the privilege state as unverified with the reason rather than assuming permission.

## Audit log

Every change myflames sends, refuses, or that you cancel in a review sheet is recorded on this computer in `workspace.sqlite3` before any statement reaches the server, and updated with how it finished (**Done**, **Failed**, **Outcome unknown**, **Not finished**, **Stopped**, **Refused** or **Declined**). Audit entries record the timestamp, connection name and **Type**, SSH tunnel host when used, verified target checks, duration, affected rows and stored statements with passwords redacted. Passwords are never stored, and **Import CSV** audit records omit imported row data.

Open **Settings** > **Audit log** (from the sidebar gear, `⌘,` on macOS or `Ctrl+,` on Windows), choose **Audit log** in **Search…** (`⌘K` / `Ctrl+K`), or follow **Open the Audit log** from a reviewed change's result or from the inspectors in **Parameters** and **Users and roles**. You can filter records by **Connection**, **Status** and **Since**, set **Retention** from 1 to 3,650 days (90 days by default), and choose **Export** to download matching records as **CSV** or **JSON**.

## Keep the cost of reads in mind

Reads can still consume CPU, buffer pool pages, disk I/O and metadata or InnoDB locks. **Run analysis** executes the `SELECT` inside a read-only transaction to measure actual operator timings. **Read-only** blocks state changes, not expensive reads: start with **Explain** for estimated plans when investigating a busy server.

## Connection groups and topology consent

When you open **Cluster**, **Replication**, **ProxySQL** or **Readyset**, myflames can read related servers (such as replication sources or linked ProxySQL and Readyset instances) to build the multi-tier topology and check source versions. A saved connection's **Group** is your consent for those peer logins:

- myflames logs in with saved passwords only to the connection in use or to other saved connections in the same non-empty **Group** as the connection in use.
- If the connection in use is unsaved, has an empty **Group**, or its host, port, user, TLS or SSH fields were edited away from the saved connection, myflames reads only the connection you opened. Imported connections always start with an empty **Group**.
- Before every statement of a peer read, myflames verifies again that both connections remain in the same **Group** and that the peer's endpoint and saved password have not changed. Notes about unreached or failed group peers use fixed plain phrases and never expose SQL or raw client errors.

When other saved connections match the open server's topology (matched without logging in, from server identities remembered when you opened those connections directly or from addresses reported by the open server), the connection-group bar above the topology shows which group members were read and offers **Add to** (for example **Add to prod-cluster**), **Review…** or **Create a group…**.

## Local data, credentials and sharing

Workspace state, saved investigations and the **Audit log** stay on your computer (`~/Library/Application Support/myflames-desktop` on macOS, `%APPDATA%\myflames-desktop` on Windows). Saved database passwords are stored in the local encrypted vault (`credentials.vault` and `vault.key` under `backend-home/.myflames`), never in `workspace.sqlite3`, captures or exports. Database SSH tunnels run your system `ssh` with your `~/.ssh/config`, keys and agent; in **File transfer**, host keys you trust are stored in myflames' own `ssh_known_hosts` file without modifying `~/.ssh/known_hosts`.

myflames makes outbound network requests only to your database and SSH servers, to GitHub release pages during update checks (controlled by **Settings** > **Updates and news** > **Check for updates automatically**), to Planet MySQL and MariaDB Foundation feeds while **Home** is open (controlled by **Settings** > **Updates and news** > **Show MySQL and MariaDB news on Home**), to vendor release listings when you check upgrade targets or tool updates in **Maintenance**, and to Oracle's download CDN when you choose **Get MySQL Shell** in **Settings** > **Client tools** > **Client tools…**.

In **Plans** > **Export**, **Hide literal values** replaces SQL and filter literals with `?` while keeping schema and table names, data types, row counts, timings and server versions; review the exported file before sharing it outside your team. Schema sync scripts in **Schema versions**, **Export as SQL…** scripts and exported account `GRANT` scripts are generated for review only and are never executed automatically.
