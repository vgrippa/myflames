# Installation & first steps

myflames is a desktop performance workspace for MySQL, MariaDB and Percona Server. You can start with a saved JSON execution plan without connecting to a server.

## Choose your installation

### macOS, Apple Silicon

Download the signed, notarized `.dmg` from [official downloads](https://github.com/vgrippa/myflames/releases/latest), open it, and drag `myflames.app` to Applications. Open the app from Applications.

The current downloadable release checked for this site is **3.4.0**. A macOS Intel binary is not advertised in this release.

### Windows x64, preview

Download the Windows setup `.exe` from [official downloads](https://github.com/vgrippa/myflames/releases/latest). The installer is unsigned; Windows SmartScreen may require **More info → Run anyway**. It installs for the current user.

The Windows build is a preview tested on Windows 11 x64. A crash can leave a statement that was already running on the server until it finishes. Read the [desktop reference](../DESKTOP.md) before using maintenance operations.

## Updates

myflames checks for a newer release when it starts and once a day while **Settings > Check for updates automatically** is on (the default); **Check for updates** in the window status bar on every screen and About myflames checks on demand. When an update is available, a badge beside the logo at the top of the sidebar follows the download until the update is ready. Choose **Update**, then **Restart to update**, or let the update install when you next quit the app. On macOS, keep `myflames.app` in your Applications folder so it can update in place. On Windows the update installs silently after the app quits.

## Appearance

Choose a theme in **Settings > Appearance**, where a card per theme previews its colors, or open **Search…** (⌘K on macOS, Ctrl+K on Windows) and type **Theme:**. Available themes are System, Light, Dark, Fire, Digital Rain, Neon Grid, Dracula, Nord, Solarized Light, Solarized Dark, Tokyo Night, Gruvbox Dark, Catppuccin Mocha, Catppuccin Latte, GitHub Light, GitHub Dark and One Dark. The choice is kept on this computer and applies to navigation, views, tables, dialogs, the Query editor and charts. System follows your operating system's light or dark appearance.

Editor palettes are adapted for readable text and selections. Chart operator colors, status meanings and Production (purple) and Development (teal) connection colors keep their meaning in every theme. Palette credits and licenses are in [NOTICE](../../NOTICE).

## Sidebar and status bar

Drag the sidebar's right edge to choose its width. You can also focus the edge and use Left or Right (hold Shift for a larger step). Double-click, Home or End restores the default width. Narrow windows keep their compact navigation rail. Sidebar and Server inspector widths, the Server view navigation choice, Health auto-refresh choices for Production and Development, Query editor Explain format, and File transfer hidden-file and Transfers-panel choices survive app restarts alongside your theme.

Every screen has one window status bar. Its right end shows the installed version, update status, **Check for updates** and **View releases**. The current view's safety, connection, session and refresh facts remain on the left; narrow windows shorten these facts before the update actions.

## Start with an exported plan

Open Plans in the desktop app and import your exported JSON plan. Select Visual Explain, Flame graph, Tree or Bar chart, then select an operator to inspect its measurements and findings.

To share it, choose **Export** in Plans for a standalone HTML report or JSON. **Hide literal values** reduces what is shared, but object names and measurements remain; inspect the export before distributing it.

## Connect to a server

The app includes the MySQL 8.4 LTS client (`mysql` and `mysqldump`) and finds installed client tools. For each server it prefers tools of the same engine and major.minor series, then the nearest newer series, then the newest older series with a warning. Within that series, authentication plugins come first, then the newest patch, then installed before bundled. MariaDB accounts using `ed25519` or `parsec` need MariaDB tools, and Maintenance may also need `mysqlsh` or `mariadb-dump`.

Open **Settings > Client tools…** to see the programs for the connection in use and every installation found. MySQL and MariaDB each have a choice: **Automatic (best match for each server)** or an installation you choose. A choice that stops working stays selected with its error until you change it. If a view cannot use a client, **Choose client tools…** opens the same dialog; an authentication-plugin error names the plugin the client could not load.

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
