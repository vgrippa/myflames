# Installation & first steps

myflames is a desktop performance workspace for MySQL, MariaDB and Percona Server. You can start with a saved JSON execution plan without connecting to a server.

## Choose your installation

### macOS, Apple Silicon

Download the signed, notarized `.dmg` from [official downloads](https://github.com/vgrippa/myflames/releases/latest), open it, and drag `myflames.app` to Applications. Open the app from Applications.

The current downloadable release checked for this site is **3.3.1**. A macOS Intel binary is not advertised in this release.

### Windows x64, preview

Download the Windows setup `.exe` from [official downloads](https://github.com/vgrippa/myflames/releases/latest). The installer is unsigned; Windows SmartScreen may require **More info → Run anyway**. It installs for the current user.

The Windows build is a preview tested on Windows 11 x64. A crash can leave a statement that was already running on the server until it finishes. Read the [desktop reference](../DESKTOP.md) before using maintenance operations.

## Start with an exported plan

Open Plans in the desktop app and import your exported JSON plan. Select Visual Explain, Flame graph, Tree or Bar chart, then select an operator to inspect its measurements and findings.

To share it, choose **Export** in Plans for a standalone HTML report or JSON. **Hide literal values** reduces what is shared, but object names and measurements remain; inspect the export before distributing it.

## Connect to a server

Live features need a local `mysql` or `mariadb` client, reachable through PATH or standard vendor installation folders. Maintenance may also need `mysqlsh`, `mysqldump` or `mariadb-dump`.

Add a connection on Home, choose a connection policy, and test it. Start with **Read-only** for inspection. Use SSH tunneling when your connection requires a bastion. Credentials and SSH configuration remain on your computer.

The macOS app stores its data under `~/Library/Application Support/myflames-desktop`; Windows uses `%APPDATA%\myflames-desktop`.

## Choose a next step

- [Understand a plan](plans.md)
- [Work in the Query editor](queries.md)
- [Inspect server health](server.md)
- [Check engine compatibility](compatibility.md)
- [Understand connection policies](safety.md)
