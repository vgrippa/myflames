# Changelog

The release notes for every published version are in [docs/release-notes](docs/release-notes/index.md).

This file lists the changes merged since the last release. Add an entry under [Unreleased] in the same pull request as the change, under the heading of the section it belongs to: `### New features`, `### Improvements`, `### Bug fixes`, `### Security fixes`, `### Deprecations and removals`, `### Known issues` or `### Packaging notes`. Write it for people who use myflames, the way the release notes are written (docs/DEVELOPER_REFERENCE.md, "Release notes"). When a version ships, its entries move into `docs/release-notes/X.Y.Z.md` and this section starts empty again.

## [Unreleased]

### Improvements

- Improves workspace startup and shutdown by loading backend feature modules on first use and stopping the startup client-tool check immediately on exit without a two-second wait.
- Improves query execution speed by reusing the chosen `mysql` or `mariadb` client on a connection while nothing affecting that choice has changed, while still verifying the program file before each use. **ProxySQL** Admin connections also skip repeating a version probe before every statement.
- **Topology** > **Accounts** and **Compare servers** now read accounts in batches instead of one at a time, cutting an **Accounts** read on a five-member InnoDB Cluster group with 12 accounts from about 4.6 s to about 1 s while still checking that every row comes from the identified server.
- Abandoned read-only requests now stop when their caller disconnects so no further query is sent, and identical **Health**, **Waits**, **I/O**, **Memory**, **Locks**, **Indexes**, **Tables**, **Full analysis** or **Topology** reads of the same server at the same time (for example in two windows) run once and share the result.
- The **Query editor** now checks large multi-row `INSERT` statements and long scripts up to 10 times faster before running them: a 232 KB `INSERT` with JSON values drops from 11.2 s to 0.9 s, and a 4,000-statement script from 8.4 s to 1.7 s.
- The **Query editor** now splits scripts with many comments much faster, cutting a 106 KB script from 2.6 s to 0.07 s.
- Very long statements with unterminated comments or quotes no longer stall **Query digest**, the `SELECT` check before `EXPLAIN`, table extraction or **Readyset** cache creation, cutting a 64 KB check from over 4 s to under 20 ms.
- Improves saving **Query editor** history, audit records and saved queries by hiding secrets about ten times faster for ordinary scripts.
- Improves **Visual Explain** wheel zooming so large plans zoom smoothly, and reduces the code loaded on the first screen.
- Improves opening the workspace and reading saved passwords on Windows by no longer rewriting owner-only permissions for the same storage files every time.

### Bug fixes

- Fixed an issue where **Query editor** history, saved queries and the **Audit log** could keep a secret written in an unusual form, such as inside quoted dynamic SQL strings, encoded literals, custom `DELIMITER` scripts, MariaDB executable comments, translated server error messages, or database URLs whose user name contains `[` or whose scheme has a driver prefix.
- Fixed an issue where SQL after a hidden value in an unambiguous custom `DELIMITER` script within the scan limits, or the row alias of `INSERT ... AS new`, was dropped from stored **Query editor** copies. Ambiguous scripts and credential-bearing tails remain conservatively hidden.
- Fixed an issue where **Software update** stayed failed after two failed attempts to open it until the app restarted. Focus now returns to the control that opened it after **Dismiss**, stays on **Retry** after a failed retry, and the busy state shows on the 720 px sidebar rail too.
- Fixed an issue where the **Replication** place could link a local **ProxySQL** or **Readyset** instance to a server reached over SSH or to a remote replica reporting a loopback address when the addresses looked alike, and where hexadecimal or octal IPv4 addresses did not match their dotted form.
- Fixed an issue where **Topology** > **Map** drew a **ProxySQL** backend in a monitor's offline hostgroup, `OFFLINE_SOFT`, or `OFFLINE_HARD` as a working route. The diagram now draws it offline with a short `hostgroup N` label, while the **Links** list and link tooltip state the reason (including replication-lag shunning or no `ONLINE` member).
- Fixed an issue where **Readyset** cache export commented out cache names ending in a non-ASCII character, hid a word in a definition that happened to equal the connection password, and missed URL passwords containing a quote, a space or a backtick. A password inside a string literal or comment stays hidden, and exports start with a `charset utf8mb4` line for importing with the `mysql` or `mariadb` client when runnable SQL or an emitted `USE` statement contains a non-ASCII character immediately before a backtick or backslash.
- Fixed an issue where **Maintenance** could offer a `mysqlsh` link pointing to another program as MySQL Shell.
- Fixed an issue where one group member's unexpected error failed the whole **Topology** read or group connection. Only that member is now marked failed.
- Fixed an issue where **Import CSV** could put keyboard focus on **Choose file…** instead of the path field on a busy computer, the **Health** watch line's **Stop** button moved as the count changed, and **Back** in full-width detail views (**Top statements**, **Accounts**) could move under the pointer while the view slid in.
- Fixed an issue where the **Query editor** toolbar moved about 23 px to the right after a connection was chosen.
- Fixed an issue where opening or closing an inspector or a full-width detail view, or pressing **Back**, could scroll the page.
- Fixed an issue where popovers and row menus opened in a view's content could open behind the sticky compact toolbar at the 720 px window width. They now render above the toolbar in every place.
- Fixed an issue in **Server** > **Parameters** where a long staged value for a set-type parameter (for example `sql_mode`) could push the members button under **Discard**, which took the click and dropped the draft.
- Fixed an issue where running or explaining a parenthesized `SET` statement such as `(SET x=1)` in the **Query editor** failed with `Could not complete the request. Try again or use the CLI.` while preparing the run. It is now classified as a risky statement that asks for confirmation on **Production** and **Development** and is refused on **Read-only** connections.
- Fixed an issue where a **Database diagram** label containing `$$` could be drawn as a math formula in the Mermaid preview.
- Fixed an issue where the tool update check and the upgrade-target check could wait forever on a server that answers very slowly.
- Fixed an issue where an unexpected error during a write or **Query editor** action that can send SQL could report an error that sounded like nothing was sent. It now states that the request could not be completed, that its outcome is unknown, and to check the server state before trying again.
- Fixed an issue where **Readyset** cache export could let hiding one credential shift quote boundaries for another. Credentials are now found on the original definition under both quote modes: when quote-mode differences prevent safe redaction, or a definition's URL password holds a quote or backtick or breaks its token, the entire definition is replaced with `-- This definition was hidden because its credentials depend on the server's quote mode.`, while safely mergeable differences retain the redacted definition, commented out.
- Fixed an issue where saving a connection could create a duplicate after a part of the app failed to load. The saved connection is now kept and the list refreshes even when readiness details fail to load.
- Fixed an issue where saving a connection's settings again right after the connection list failed to refresh could erase its saved password. The retry now remembers the password state of the save it repeats.

### Security fixes

- Updates Electron to 44.11.5, which includes the mitigation for the compromised eMudhra intermediate certificates (GHSA-jr5w-w6x2-84p9).

### Known issues

- A credential written with a comment or no space between the keyword and the value (for example `IDENTIFIED BY'x'` or `IDENTIFIED BY /* c */ 'x'`), or MariaDB `IDENTIFIED VIA ... USING`/`AS` chains, can still be kept in **Query editor** history, saved queries and the **Audit log**, as in 3.5.1.
- A server error echo that starts inside a secret (for example `ERROR 1064 ... near '<tail of the secret>'`) cannot be recognized as a credential and can still be kept in **Query editor** history, saved queries and the **Audit log**, as in 3.5.1.
- Stored **Query editor** history, saved queries and the **Audit log** can still show a secret in rare malformed text that 3.5.1 also mishandled: a server `ERROR 1231` echo whose value is cut off before its closing quote, a custom `DELIMITER` script where a secret variable follows a comma-prefixed assignment after a `DELIMITER` change, prose with an unmatched apostrophe before credential statements in different quote styles (for example `It didn't work:` followed by `SET PASSWORD FOR app = "o'brien";` and a later credential statement), or a credential literal cut off before its closing quote that takes the next credential's opening quote so that next secret stays visible, as in 3.5.1.
- **Readyset** cache export of definitions with stray unbalanced quotes can show a literal that 3.5.1 hid.
