# File transfer and SSH

**File transfer** in the sidebar is a two-pane SFTP workspace for moving dumps, slow query logs, configuration files and scripts between **This computer** and an SSH server.

Unlike database connections on **Home**, **File transfer** operates on host files over SFTP using your system `ssh`, `sftp` and `ssh-keygen` tools. Its connections are separate from your MySQL and MariaDB profiles, and file transfers do not run SQL or write to the database **Audit** log.

For step-by-step walkthroughs, see the [Connections and files how-to guides](howto-connections.md).

## Saved SSH connections

The **SSH connection** bar at the top of **File transfer** lets you pick a saved SSH connection, click **Disconnect** when a session is open, or click **New** (or **New SSH connection** in the empty state) to add one. When no SSH connection is active, the saved SSH connections table lists each connection's **Name**, **Host**, **User**, **Authentication** and **Default remote folder**.

- **Starting from an existing host**: in the **New SSH connection** (or **Edit SSH connection**) dialog, **Start from** offers **Enter details** plus any SSH hosts already configured on your database connections on **Home** and literal host entries discovered from `~/.ssh/config` (wildcard patterns such as `*` or `?` are skipped). Choosing one pre-fills the host, port, user and key path so you do not have to retype them.
- **Connection fields**:
  - **Name**: a label for the list (defaults to `user@host` when left blank).
  - **Host**, **Port** (1 to 65535, default `22`) and **User** (required).
  - **Authentication**:
    - **SSH agent or keys**: uses your running SSH agent and default key files (`~/.ssh/id_ed25519`, `~/.ssh/id_rsa` and others) in `BatchMode=yes` without storing a secret in myflames.
    - **Key file**: uses a specific private key file (**Key file path**, with **Browse…** to pick a file under `~/.ssh` or another local folder) and an optional **Key passphrase**.
    - **Password**: uses an **SSH password** stored in your local vault.
  - **Default remote folder**: the initial path opened on the server (such as `/var/lib/mysql` or `/var/log/mysql`).
  - **Default local folder**: the initial path opened on **This computer** (with **Browse…** to pick a local directory).
- **Testing and managing**: click **Test connection** in the dialog to verify the SSH handshake and remote home directory before clicking **Save**. In the saved connections table, each row's **Open** button connects immediately, and the `⋯` or right-click menu offers **Open**, **Edit…**, **Duplicate**, **Test connection**, **Copy address** (`user@host` or `user@host:port`) and **Delete…**.

## Host-key trust review

myflames never disables SSH host-key verification and never uses `StrictHostKeyChecking=no` or `AcceptNew`. Every SFTP connection checks the server's host keys against myflames' own known-hosts file (`~/.myflames/ssh_known_hosts`, created with mode `0600`) while also reading your standard `~/.ssh/known_hosts` file so hosts you already trust in OpenSSH work without a second prompt.

- **First connection to an unknown host**: if the server's host key is not in `~/.myflames/ssh_known_hosts` or `~/.ssh/known_hosts`, myflames scans the server's public keys with `ssh-keyscan`, computes their `SHA256:...` fingerprints with `ssh-keygen`, and opens the `Trust <host>?` (or `Trust this server?`) review sheet before sending any password, key passphrase or file data.
- **What the review sheet shows**: the host and port (`<host>:<port>`), each key algorithm (`ED25519`, `ECDSA`, `RSA`) with its `SHA256:...` fingerprint and a **Copy** button, and where the key will be recorded (`~/.myflames/ssh_known_hosts`). Clicking **Trust and connect** writes the verified keys to `~/.myflames/ssh_known_hosts` and completes the connection or test.
- **Changed host keys**: if a server presents a host key that differs from the key already trusted in `~/.myflames/ssh_known_hosts` or `~/.ssh/known_hosts`, myflames stops the connection and reports the host-key mismatch so you can verify with the server administrator whether the host was rebuilt or the connection is being intercepted. myflames never edits or overwrites your `~/.ssh/known_hosts` file.

## Two-pane browser

**File transfer** displays **This computer** on the left and the connected SSH server on the right. You can browse local folders on **This computer** even before opening an SSH connection.

- **Navigation controls**: each pane has **Back**, **Forward**, **Up one folder** (`Backspace` or `Alt+Up`), **Home folder**, and per-pane **Refresh** (`F5`). The path bar shows clickable breadcrumb segments; clicking the path background or **Edit path** switches to a text input where you can paste or type a directory path and press `Enter`. On **This computer**, **Browse…** opens the native OS folder picker.
- **Toolbar controls**: **Show hidden files** toggles dotfiles (names starting with `.`), **Transfers** toggles the bottom transfer queue panel (showing an active count badge while transfers run), and **Refresh** reloads both panes. Each pane also has its own **Filter** field to narrow the visible entries by name and a table header to sort by **Name**, **Size** or **Modified** (folders stay grouped at the top).
- **Transferring files and folders**:
  - Select one or more files or folders in **This computer** and click **Upload** (or press `Enter` on a selection, or drag the rows into the server pane) to upload them to the folder currently open on the server.
  - Select one or more files or folders on the server and click **Download** (or drag them into **This computer**) to download them to the folder currently open on **This computer**.
  - If any destination file or folder already exists, a conflict review sheet (`Replace existing file?` or `Replace <N> existing items?`) lists the colliding names before starting the transfer and lets you choose **Overwrite** or **Skip existing** (`Cancel` aborts the transfer).
- **Folder and file actions**:
  - **New folder** (the folder-plus icon in the pane header, or **New folder…** in the right-click menu) creates a directory in that pane.
  - **Rename…** (`F2` or the right-click menu) renames the selected file or folder in place.
  - **Copy path** copies the full local or remote path to the clipboard.
  - On **This computer**, **Reveal in Finder** (macOS) or **Show in File Explorer** (Windows) highlights the item in your system file manager.
  - On the server pane, **Delete…** (`Delete` / `Backspace` or the right-click menu) opens a confirmation sheet (`Delete <name>?` or `Delete <N> items?`) showing the server host and remote path before removing the remote file or directory tree. Local file deletion is intentionally not offered in **This computer**; use Finder or File Explorer to delete local files.

## Transfer queue

Clicking **Transfers** in the toolbar opens or closes the transfer queue panel at the bottom of **File transfer** (it also opens automatically when you start an upload or download).

- **What you see**: each queued, running, completed, failed or cancelled transfer row shows its direction (`↑` upload or `↓` download), file or folder name, source and destination endpoints, status pill (`Running`, `Queued`, `Completed`, `Failed`, `Cancelled`), byte progress (`transferred / total` and percentage progress bar), transfer rate (`B/s`, `KiB/s`, `MiB/s`) and error detail when a transfer fails.
- **Controls**: switch between **This connection** (transfers belonging to the active SSH connection) and **All connections** (transfers across every SSH connection in this session), click **Cancel** on a running or queued transfer to stop its SFTP subprocess, click **Retry** on a failed or cancelled transfer to re-queue it, or click **Clear finished** to remove completed, failed and cancelled rows from the list.
- **Atomic staging**: downloads write to a temporary `.myflames-part` sibling file in the destination folder and rename it into place only after the transfer succeeds; cancelling or failing a download removes the partial `.myflames-part` file so an interrupted download never leaves a truncated file under the final name.

## Safety and credentials

- **Where secrets live**: SSH passwords and private-key passphrases are stored in the local myflames secret vault (`~/.myflames/vault`) keyed by the connection ID and bound to the exact `(host, port, user, auth_type, key_path)` tuple. Changing the host, port, user, authentication mode or key path on a saved connection requires re-entering the secret unless you explicitly keep it for the same endpoint, preventing a saved password or passphrase from being sent to a different host.
- **Single-use askpass helper**: when authenticating with a password or key passphrase, myflames supplies the secret to OpenSSH through a temporary `0700` `SSH_ASKPASS` helper script and a single-use `0600` secret file that verifies its parent process PID, deletes the secret file on first read, and is cleaned up in `finally`. Passwords and passphrases are never passed on the command line or written to logs.
- **No agent or keychain side effects**: myflames runs `ssh` and `sftp` with `-o AddKeysToAgent=no` (and `-o UseKeychain=no` on macOS) so inspecting or testing a connection never silently adds keys to your system SSH agent or macOS Keychain.
- **Symbolic links and special files**: local uploads and folder walks use `O_NOFOLLOW` / `lstat` and refuse symbolic links, FIFOs, sockets and device nodes so a symlink inside a transfer tree cannot cause myflames to read or overwrite files outside the selected directory. Remote symbolic links are displayed with a `link` badge in the browser and are not traversed recursively during folder transfers.
- **Platform notes**: on Windows, password and passphrase askpass authentication requires OpenSSH 8.4 or later (`SSH_ASKPASS_REQUIRE=force`); for older Windows OpenSSH builds, use **SSH agent or keys** or an unencrypted **Key file**.
