# Installation & first steps

myflames is a desktop performance workspace for MySQL, MariaDB and Percona Server for MySQL. You can start with the built-in sample plan or an exported JSON execution plan before connecting to a server.

## Install and update

### macOS, Apple Silicon

Download `myflames-3.4.3-macos-arm64.dmg` from [official downloads](https://github.com/vgrippa/myflames/releases/latest), open it, and drag `myflames.app` to Applications. Open `myflames.app` from Applications; it is signed with a Developer ID and notarized by Apple.

The current downloadable release checked for this site is **3.4.3**. A macOS Intel binary is not advertised in this release. Application data lives under `~/Library/Application Support/myflames-desktop`.

### Windows x64, preview

Download `myflames-3.4.3-windows-x64-setup.exe` from [official downloads](https://github.com/vgrippa/myflames/releases/latest) and run it. The installer is not code signed yet, so when Windows SmartScreen shows "Windows protected your PC", choose **More info**, then **Run anyway**. It installs for the current user without an administrator prompt and keeps its data in `%APPDATA%\myflames-desktop`.

The Windows build is a preview tested on Windows 11 x64. SSH tunnels stop with the app, while an in-flight query or a running dump, restore or clone job can keep running on the server after an app crash until it finishes. Read [Server diagnostics & operations](server.md) and the [desktop reference](../DESKTOP.md) before running maintenance jobs on Windows.

### Updates and news

myflames checks GitHub for a newer release when it opens and once a day while **Settings** > **Updates and news** > **Check for updates automatically** is on (the default). Choose **Check for updates** in the window status bar on any screen or in **Settings** > **About myflames** to check on demand.

When a newer release is available, a badge beside the logo at the top of the sidebar (and **Update…** in the status bar, or **Update available…** in **Settings** > **Updates and news**) opens **Software update**. Choose **Update** to download and verify the release against its published SHA-256, then **Restart to update**, or let it install the next time you quit. On macOS, keep `myflames.app` in Applications so it can update in place; otherwise **Download update** saves the installer to your Downloads folder. On Windows, the staged installer runs silently after the app quits.

While **Home** is open, the **News** pane reads MySQL and MariaDB headlines from Planet MySQL and the MariaDB Foundation over HTTPS and caches them for six hours. Choose **Refresh news** to fetch them again, or turn off **Settings** > **Updates and news** > **Show MySQL and MariaDB news on Home** to stop news requests.

## Connect to a server

The packaged app bundles the official MySQL 8.4 LTS client (`mysql` and `mysqldump`) and discovers installed MySQL, MariaDB and MySQL Shell tools on your computer. For each server it prefers tools of the same engine and major.minor series, then the nearest newer series, then the newest older series with a warning; within a series, authentication-plugin support comes first, then the newest patch, then installed tools ahead of the bundled client. MariaDB accounts using `ed25519` or `parsec` authentication and MariaDB dumps (`mariadb-dump`) require an installed MariaDB client.

Open **Settings** > **Client tools** > **Client tools…** to inspect which programs handle queries, dumps, restores and MySQL Shell jobs for the connection in use. MySQL and MariaDB each default to **Automatic (best match for each server)** or let you pin an installation; a pinned path that stops working stays selected with its error instead of switching silently. When a read fails for a missing client or authentication plugin, **Choose client tools…** opens the same dialog.

On **Home** in **Connections**, choose **Add connection** (or open the **Connection in use** picker in the top bar and choose **New connection…**):

1. Enter **Host**, **Port**, **User**, **Password** and an optional **Default database** (pasting a `host:port` value or `mysql://` URL into **Host** fills the matching fields).
2. Choose a connection **Type**: **Production** (the default, which asks before every change) or **Development** (which asks before risky changes). Turn on **Read-only** to refuse every change on this connection.
3. Expand **TLS settings** to set **TLS mode** (`PREFERRED`, `REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY` or `DISABLED`) and an optional **CA file path**. Expand **Connect through SSH** when the database is reached through an SSH host; myflames uses your system `ssh`, `~/.ssh/config` aliases, keys and agent.
4. Set an optional **Group** name (up to 80 characters) when this server belongs to a replication pair, cluster, ProxySQL tier or Readyset tier that you want topology views to read together. myflames never logs in to a saved connection outside the **Group** of the connection in use (see [Connection policies & write safety](safety.md)).
5. Choose **Test connection** to verify connectivity and load the database list, then choose **Save connection** (which asks **Also save the password?**: **Save with password** stores it encrypted on this computer, and **Settings only** saves the connection without a password) or **Use without saving**.

**Home**'s **Connections** table shows each saved connection's **Type**, `user@host:port` target, **Database** (tooltip "Default database") and **Server** readiness facts (product, version, replication or cluster role, read-only state and Performance Schema state), kept across restarts and marked stale while offline. Choose **Check now** from a connection row's menu, run **Test connection**, or pick the connection in **Connection in use** to refresh those facts.

### Import saved connections

To bring existing connections over from MySQL Workbench or DBeaver, go to **Home** > **Connections**, open the **Connections menu** (`⋯` beside **Add connection**), and choose **Import from MySQL Workbench or DBeaver…**.

In **Import connections**, pick **MySQL Workbench** (`connections.xml`) or **DBeaver** (`data-sources.json`) in **From**. If your connection file is not in its default folder, choose **Choose file…** to select it. Review the preview rows, where duplicate names and endpoints are unchecked with the reason shown, then choose **Import** (for example **Import 3 connections**). Password stores and SSH keys are never read: imported connections are saved as **Production** with TLS mode `PREFERRED`, an empty **Group**, and no saved password, so review their TLS and SSH settings and run **Test connection** before using them.

## Inspect your first plan

To try myflames before connecting to a database, go to **Home** and choose **Explore sample plan** under **Recent work**, or choose **Import plan** in the top bar (you can also drag a `.json` file onto **Home** or **Plans**). In **Import an execution plan**, choose a file or paste `EXPLAIN ANALYZE FORMAT=JSON`, `EXPLAIN FORMAT=JSON`, MariaDB `ANALYZE FORMAT=JSON` or a myflames capture `.json`, then choose **Analyze plan**.

In **Plans**, switch between **Visual Explain**, **Flame graph**, **Execution tree**, **Bar chart** and **Stats**. Select an operator in a chart (or a table or operator row in **Stats**) to inspect its timings, row estimates and advisor findings, or switch from **Explore** to **Compare** in the top bar to compare two plans side by side.

To share a plan, open **Export** in **Plans** and choose **HTML report** or **JSON**. **Hide literal values** replaces SQL and condition literals with `?` while keeping object names, counts and timings; inspect the file before sharing it.

## Run your first query

1. Open **Query editor** in the sidebar (or choose **Write SQL** on **Home**).
2. Check the **Connection in use** pill in the top bar and pick a **Database** in the editor toolbar.
3. Enter a `SELECT` statement and choose **Run** (`⌘Return` on macOS, `Ctrl+Enter` on Windows) to inspect the result rows below the editor.
4. Choose **Explain** (`⌘E` on macOS, `Ctrl+E` on Windows) to run the server's `EXPLAIN` in **Table**, **Tree** (MySQL 8.0.16+) or **JSON** format, then choose **Explore** in the Explain result to open the visual plan in **Plans**.
5. On MySQL 8.4+ and MariaDB 10.11+, choose **Run analysis** (`⇧⌘E` on macOS, `Ctrl+Shift+E` on Windows) to execute the `SELECT` inside a read-only transaction and capture measured operator timings.

## Navigate the workspace

### Command palette

Choose **Search…** in the top bar or press `⌘K` on macOS (`Ctrl+K` on Windows) to open the **Command palette** (`Search or jump to…`).

- When the search field is empty, **Recent** lists the last places and views you visited.
- Typing filters every sidebar place (**Places**), **Server** and **Workload** view, **Compare** (**Plans**), **Full analysis** (**Tuning**), **Theme:** option (**Appearance**), workspace action (**Import plan**, **Audit log**, **Account and privileges**, **New connection**, **Settings**), and **Query editor** tab action (**Rename query tab**, **Format SQL**, **Save query…**).
- When a server is connected, typing two or more characters also searches databases, tables, views, sequences, procedures, functions, triggers and events on the connection in use. Choosing a database, table, view or sequence reveals it in the **Query editor**'s **Schemas** browser; choosing a routine, trigger or event opens its `SHOW CREATE` statement in a new **Query editor** tab without running it.

Use **Back** and **Forward** (`⌘[` and `⌘]` on macOS, `Alt+Left` and `Alt+Right` on Windows) to step through visited places, **Server** and **Workload** views, query tabs and open plans.

### Appearance

Open **Settings** from the gear at the bottom of the sidebar (`⌘,` on macOS, `Ctrl+,` on Windows) and choose **Appearance**, or open **Search…** and type `Theme:`. Available themes are System, Light, Dark, Fire, Digital Rain, Neon Grid, Dracula, Nord, Solarized Light, Solarized Dark, Tokyo Night, Gruvbox Dark, Catppuccin Mocha, Catppuccin Latte, GitHub Light, GitHub Dark and One Dark. System follows your operating system's light or dark appearance, and your choice is saved on this computer across navigation, tables, dialogs, the **Query editor** and charts.

Editor palettes keep readable text and selection contrast. Chart operator colors, status indicators, and **Production** (purple) and **Development** (teal) connection colors keep their meaning in every theme. Each theme card in **Settings** > **Appearance** names its palette credit; application license and inspiration credits appear in **Settings** > **About myflames**, and full third-party notices ship in the `NOTICE` file in the repository and app bundle.

### Sidebar and status bar

Drag the sidebar's right edge to resize it, or focus the edge and press Left or Right (hold Shift for larger steps). Double-clicking the edge or pressing Home or End restores the default width; narrow windows switch to a compact navigation rail. Sidebar and **Server** inspector widths, the **Server** view list mode, **Health** auto-refresh choices, the **Query editor** Explain format, and **File transfer** hidden-file and **Transfers**-panel choices persist across restarts.

Every screen shares one bottom status bar. Its left side shows the current connection's write-safety state, session facts and refresh status; its right side shows the installed version, update status, **Check for updates** and **View releases**.

## Where to go next

Step-by-step how-to guides:

- [Query editor](howto.md): multiple cursors, suggestions, find and replace, themes, layout, and running and explaining SQL
- [Plans and schemas](howto-plans.md): exploring and comparing execution plans, and comparing or exporting schemas
- [Server and workload](howto-server.md): inspecting live health, sessions, locks, parameters and query digests
- [Connections and files](howto-connections.md): managing saved connections, connection groups, and SSH/SFTP file transfer
- [Replication, clusters and proxies](howto-topology.md): inspecting Replication, Cluster, ProxySQL and Readyset topologies

Workflows and reference:

- [Execution plans & comparison](plans.md)
- [Query editor & slow queries](queries.md)
- [Server diagnostics & operations](server.md)
- [File transfer and SSH](files.md)
- [Connection policies & write safety](safety.md)
- [Engine & platform compatibility](compatibility.md)
