# Installation & first steps

myflames is a desktop performance workspace for MySQL, MariaDB and Percona Server. You can start with a saved JSON execution plan without connecting to a server.

## Choose your installation

### macOS, Apple Silicon

Download the signed, notarized `.dmg` from [official downloads](https://github.com/vgrippa/myflames/releases/latest), open it, and drag `myflames.app` to Applications. Open the app from Applications.

The current downloadable release checked for this site is **3.3.3**. A macOS Intel binary is not advertised in this release.

### Windows x64, preview

Download the Windows setup `.exe` from [official downloads](https://github.com/vgrippa/myflames/releases/latest). The installer is unsigned; Windows SmartScreen may require **More info → Run anyway**. It installs for the current user.

The Windows build is a preview tested on Windows 11 x64. A crash can leave a statement that was already running on the server until it finishes. Read the [desktop reference](../DESKTOP.md) before using maintenance operations.

## Updates

myflames checks for a newer release when it starts and once a day while **Settings > Check for updates automatically** is on (the default); **Check for updates** in Home's footer and About myflames checks on demand. When an update is available, a badge beside the logo at the top of the sidebar follows the download until the update is ready. Choose **Update**, then **Restart to update**, or let the update install when you next quit the app. On macOS, keep `myflames.app` in your Applications folder so it can update in place. On Windows the update installs silently after the app quits.

## Start with an exported plan

Open Plans in the desktop app and import your exported JSON plan. Select Visual Explain, Flame graph, Tree or Bar chart, then select an operator to inspect its measurements and findings.

To share it, choose **Export** in Plans for a standalone HTML report or JSON. **Hide literal values** reduces what is shared, but object names and measurements remain; inspect the export before distributing it.

## Connect to a server

Live features need a `mysql` or `mariadb` client. The desktop app includes the official MySQL 8.4 LTS client (`mysql` and `mysqldump`), so you can connect without installing one. A client you installed, reachable through PATH or standard vendor installation folders, is always preferred to the included one. MariaDB accounts that use `ed25519` or `parsec` authentication need an installed MariaDB client. Maintenance may also need `mysqlsh` or, for MariaDB servers, `mariadb-dump`. If myflames still cannot use a client, the view says so and offers **Choose client…**: pick `mysql` or `mariadb` (on Windows `mysql.exe` or `mariadb.exe` under Program Files or your profile), or open **Settings > Database client…** to see the client in use, the ones found, and to choose or clear one.

Add a connection on Home, choose a connection policy, and test it. Start with **Read-only** for inspection. Use SSH tunneling when your connection requires a bastion. Credentials and SSH configuration remain on your computer.

Home's Connections table calls its default-database column **Database** (tooltip "Default database"); the connection form keeps **Default database**. The table prioritizes product and version in **Server**, letting the stale-time hint truncate first, and its default columns are sized for a 1440 px window. It keeps the last facts collected for each server (product, version, replication or cluster role, read-only and Performance Schema state) and shows them even while the server is offline, marked as stale. Choose **Check now** from a connection's menu to refresh them; Test connection and an explicit choice in the connection picker also check the server. To bring connections over from another tool, open the Connections header menu and choose **Import from MySQL Workbench or DBeaver…**, pick **MySQL Workbench** or **DBeaver** in **From**, review the selected rows and the duplicates it found in the wider preview, then import. The sheet shows an error alert only when there is an error. Imported connections use the Production policy and carry no password, so review their TLS and SSH settings and test before use.

Use **Back** and **Forward** in the top bar (**⌘[** and **⌘]** on macOS, **Alt+Left** and **Alt+Right** elsewhere) to move across places, Server and Workload views, query tabs and open plans, or open **Search…** (**⌘K** / **Ctrl+K**) for recent destinations, commands, and database objects on the connection in use.

The macOS app stores its data under `~/Library/Application Support/myflames-desktop`; Windows uses `%APPDATA%\myflames-desktop`.

## Choose a next step

- [Understand a plan](plans.md)
- [Work in the Query editor](queries.md)
- [Inspect server health](server.md)
- [Check engine compatibility](compatibility.md)
- [Understand connection policies](safety.md)
