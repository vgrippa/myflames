# Query editor

Use these guides to edit SQL, run queries and work with results in the desktop app; shortcuts show macOS keys first, then Windows and Linux keys.

- [Multiple cursors](howto.md#multiple-cursors)
- [Suggestions](howto.md#suggestions)
- [Find and replace](howto.md#find-and-replace)
- [Settings and themes](howto.md#settings-and-themes)
- [Layout](howto.md#layout)
- [Run and explain](howto.md#run-and-explain)
- [Connect to a server and run a query](howto.md#connect-to-a-server-and-run-a-query)
- [Edit table rows after reviewing SQL](howto.md#edit-table-rows-after-reviewing-sql)
- [Commit or roll back a transaction](howto.md#commit-or-roll-back-a-transaction)
- [Explain a query and open it in Plans](howto.md#explain-a-query-and-open-it-in-plans)
- [Filter, sort and export query results](howto.md#filter-sort-and-export-query-results)
- [Explain a query with named parameters](howto.md#explain-a-query-with-named-parameters)
- [Compare Query digest with a baseline](howto.md#compare-query-digest-with-a-baseline)

## Multiple cursors

Use multiple cursors to make the same edit in several places at once.

1. Open **Query editor** and paste this practice SQL:

   ```sql
   SELECT id FROM orders WHERE status = 'new';
   SELECT id FROM orders WHERE status = 'paid';
   SELECT id FROM orders WHERE status = 'refunded';
   ```

2. Click just after `id` on the first line. Hold **Option** / **Alt** and click just after `id` on the other two lines to add cursors. If no cursors appear, turn on **Settings > Query editor > Multiple cursors**.
3. Type `, customer_id` once to add it on all three lines. Press **Esc** to keep one cursor; if suggestions are open, press **Esc** once to close them first.
4. Click inside the first `status`. Press **⌘D** / **Ctrl+D** to select the word, then again to add its next occurrence. Type `state` to replace both selections, then press **Esc** until only one cursor remains.
5. Click inside `orders` and press **⇧⌘L** / **Ctrl+Shift+L** to select every occurrence. Press **Esc** to keep one cursor.
6. Hold **⇧Option** / **Shift+Alt** and drag across `id` from the first line to the third for a rectangular selection.

![Open Query editor and paste three SELECT id FROM orders statements with status values new, paid and refunded. Click after the first id, then Option-click on macOS or Alt-click on Windows and Linux after the other two. If cursors do not appear, turn on Settings > Query editor > Multiple cursors. Type comma, space and customer_id to edit all three. Press Esc, closing suggestions first if needed, to keep one cursor. Click status, press Command+D or Ctrl+D twice and type state to replace two occurrences, then press Esc until one cursor remains. Click orders and press Shift+Command+L or Ctrl+Shift+L to select all occurrences, then Esc. Hold Shift+Option or Shift+Alt and drag across id on all three lines to select a rectangle.](../screenshots/howto-multi-cursor.gif)

Related: [Edit SQL, suggestions and checks](queries.md#edit-sql-suggestions-and-checks).

## Suggestions

Use suggestions to complete SQL keywords, table names and columns while you type.

1. Open **Query editor**, choose a connection and **Database**, and start with an empty tab. The recording uses an `orders` table with a `status` column; use names from your database.
2. Type `SEL` and press **Tab** to accept `SELECT`. If the list is closed, press **Ctrl+Space** on either platform to request it.
3. Type ` FROM o` and choose `orders` from the list. Type ` o` after the table name to give it an alias.
4. Click after `SELECT ` and type `o.`. Choose `status` from the list, then type a space before `FROM` to finish `SELECT o.status FROM orders o`.
5. Press **Ctrl+Space** to request suggestions again, then **Esc** to close them. Manual suggestions also work when **Settings > Query editor > Suggestions while typing** is off.

![Open an empty Query editor tab and choose a connection and database containing orders with a status column, or use your own names. Type SEL and press Tab to accept SELECT. Type space FROM o, choose orders, then type space o for its alias. Click after SELECT and its following space, type o. and choose status, then type a space before FROM to form SELECT o.status FROM orders o. Ctrl+Space requests suggestions on macOS, Windows and Linux even when Suggestions while typing is off; Esc closes the list.](../screenshots/howto-suggestions.gif)

Related: [Edit SQL, suggestions and checks](queries.md#edit-sql-suggestions-and-checks).

## Find and replace

Use find and replace to locate repeated text and change it throughout a query tab.

1. Open a **Query editor** tab containing repeated text, such as the `status` names in the [multiple-cursor example](#multiple-cursors).
2. Click **Find** in the editor toolbar, or press **⌘F** / **Ctrl+F**, and type `status` in **Find**. Press **⌘G** / **Ctrl+G** to move to the next match while Find is open.
3. Click **Find and replace** in the editor toolbar, or press **⌥⌘F** / **Ctrl+H**, then type `state` in **Replace with**.
4. Check the highlighted matches. Choose **Replace** to change one match or **Replace all** to change all matches in the tab.
5. Click back in the editor and press **⌘Z** / **Ctrl+Z** to undo the replacement in one step.

The toolbar's **Editing actions** menu lists editing commands with their shortcuts, including **Go to line**, line comments, cursor actions and **Toggle Tab moving focus** (**⇧⌥M** / **Ctrl+M**). **Find**, **Find and replace** and **Go to line** are also in the command palette (**⌘K** / **Ctrl+K**).

![In a Query editor tab with repeated status names, press Command+F on macOS or Ctrl+F on Windows and Linux and search for status. While Find is open, Command+G or Ctrl+G advances to the next match. Press Option+Command+F or Ctrl+H, enter state in Replace with and check the highlighted matches. Choose Replace for one match or Replace all for the whole tab. Click in the editor and press Command+Z or Ctrl+Z to undo the replacement in one step.](../screenshots/howto-find-replace.gif)

Related: [Work with SQL and results](queries.md#work-with-sql-and-results).

## Settings and themes

Use Settings to choose a theme, fonts and text sizes, and change how the Query editor behaves.

1. Click the gear at the bottom of the sidebar, or press **⌘,** / **Ctrl+,**, to open **Settings**.
2. Choose **Appearance**. Select **Dark** to try it across the app, then **System** to follow your operating system's appearance again. There are 17 choices.
3. Scroll to **Text** in **Appearance**. Open **Interface font** and choose **Inter (included)**, **System default** or an installed font from your computer, then choose **Smaller**, **Default**, **Large**, **Larger** or **Largest** in **Interface text size** (12 to 16 px) to scale workspace text.
4. Open **Code font** and choose **JetBrains Mono (included)**, **System default** or an installed monospace font, then pick **11 px** to **20 px** in **Code text size** for the Query editor and SQL or code blocks. The preview lines and the workspace update right away; choose **Restore default text settings** to return to **System default**, **Default** and **13 px**.
5. Choose **Query editor**. Switch **Multiple cursors** off, then on again, to try the control. Leave it on to use the multiple-cursor guide.
6. Press **Esc** or choose **Back** to return to your previous view. Your theme, text and editor settings are kept on this computer across app restarts.

![Click the sidebar gear or press Command+comma on macOS or Ctrl+comma on Windows and Linux to open Settings. Choose Appearance and select Dark to try it, then System to follow the operating system again; there are 17 choices. Choose Query editor and switch Multiple cursors off and back on. Press Esc or choose Back to return to your previous view. Theme and editor settings are saved on this computer across restarts.](../screenshots/howto-settings-theme.gif)

Related: [Appearance](quickstart.md#appearance) and [Query editor settings](queries.md#edit-sql-suggestions-and-checks).

## Layout

Use the layout controls to give navigation, SQL or results more room.

1. Open **Query editor** and run a SELECT from your database with **⌘Return** / **Ctrl+Enter** to show results below the editor.
2. Drag the sidebar's right edge to make it wider. For the keyboard alternative, press **Tab** until the edge has focus and use **Left** or **Right**; hold **Shift** for a larger step. Narrow windows use a compact navigation rail instead.
3. Drag the divider between the SQL editor and its results down to make the editor taller, or up to give the results more room.
4. Double-click the sidebar's right edge to restore its default width. With the edge focused, **Home** or **End** also restores it. Your chosen sidebar width survives app restarts.
5. The installed version sits beside the myflames name at the top of the sidebar. Choose it to open **Settings > Updates and news**, with the update status, **Check for updates** and **View releases**.

![Open Query editor and run a SELECT with Command+Return on macOS or Ctrl+Enter on Windows and Linux to show results. Drag the sidebar's right edge wider, or focus it with Tab and use Left or Right, holding Shift for larger steps. Narrow windows keep a compact rail. Drag the divider between SQL and results down for more editor space or up for more result space. Double-click the sidebar edge, or focus it and press Home or End, to restore its default width. Sidebar width is kept across restarts. The installed version sits beside the myflames name at the top of the sidebar.](../screenshots/howto-layout.gif)

Related: [Sidebar and status bar](quickstart.md#sidebar-and-status-bar) and [Work with SQL and results](queries.md#work-with-sql-and-results).

## Run and explain

Use Run to see a SELECT's results and Explain to inspect how the server plans to find them.

1. Open **Query editor**, choose a connection and **Database**, and enter a SELECT for a table there. The recording uses `SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24;`.
2. Click inside the statement and press **⌘Return** / **Ctrl+Enter**, or choose **Run**. Read the result rows below the editor.
3. Choose **Table** beside **Explain**, then choose **Explain** (**⌘E** / **Ctrl+E**). Its result shows the server's estimated plan; **Run analysis** executes the SELECT to measure it.
4. In the Explain result, choose **Explore** to open the visual plan in **Plans**. On MySQL, first commit or roll back any open transaction and turn autocommit on. Inspect the operators in **Visual Explain**, **Flame graph**, **Execution tree** or **Bar chart**.

![Open Query editor, choose a connection and database, and enter SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24, using a table and columns from your database. Click in the statement and press Command+Return on macOS or Ctrl+Enter on Windows and Linux, or choose Run, to see results. Choose Table beside Explain, then Explain or Command+E or Ctrl+E for the estimated plan; Run analysis executes the SELECT for measurements. Choose Explore in the Explain result to open Plans and inspect Visual Explain, Flame graph, Execution tree or Bar chart. On MySQL, first finish any open transaction and turn autocommit on.](../screenshots/howto-run-explain.gif)

Related: [Work with SQL and results](queries.md#work-with-sql-and-results) and [Explain a statement](queries.md#explain-a-statement).

## Connect to a server and run a query

Use this when you have a saved connection and want to read rows from its database.

1. Open the connection picker in the top bar.
2. Choose the saved server in **Connection**.
3. Open **Query editor** in the sidebar.
4. Choose **Database** above the SQL editor.
5. Enter a SELECT using your table and column names, for example `SELECT id FROM orders LIMIT 10;`.
6. Click inside that statement to leave a single cursor there.
7. Choose **Run**.

What you see: the returned rows appear below the editor; **Messages** shows the statement's outcome.

Good to know:

- If you need to save a connection first, use **New connection…** in the picker and follow [Connect to a server](quickstart.md#connect-to-a-server).
- **Run** sends the selected text when a selection exists, otherwise the statement at the cursor. **Run all** sends every statement in the tab.

## Edit table rows after reviewing SQL

Use this when you need to change a row in a supported base table and inspect the generated SQL first.

1. In **Query editor** > **Schemas**, right-click the table you want to change.
2. Choose **Edit table data…**.
3. Change one cell's value in the table panel.
4. Choose **Review 1 change**.
5. Inspect the statements in **Apply table changes?**.
6. Choose **Apply changes**.
7. After the operation completes, choose **Reload rows** to read the saved values.

What you see: the panel reports the operation's outcome and reloads the table page with the applied value.

Good to know:

- Query result grids are for reading. Double-clicking a name in **Schemas** inserts that name into SQL; use **Edit table data…** to edit rows.
- Editing requires an InnoDB base table with a primary key or a full-column UNIQUE key whose columns are NOT NULL. The app also checks privileges and table features; a notice explains why a table cannot be edited.
- Review is required on **Development** connections too, and the attempt is recorded in **Audit log**. A changed row or table definition refuses the batch. If the outcome is unknown, use **Check status** before making another attempt.

## Commit or roll back a transaction

Use this when you want to inspect an InnoDB row change before keeping or discarding it.

1. In a **Query editor** tab with no open transaction, run `START TRANSACTION;` with **Run**.
2. In the same tab, run one UPDATE or INSERT for your table with **Run**.
3. If a review opens, check the exact SQL before choosing **Run statement**.
4. Run a SELECT in the same tab to inspect the affected rows.
5. Open the **Transaction open** status pill above the editor; it can also show age and changed-row counts.
6. Choose **Roll back** to discard the changes, or **Commit** to keep them.
7. If **Commit** opens a review, confirm the reviewed COMMIT with **Commit**.

What you see: the editor reports **Rolled back** or **Committed** while keeping the query results available.

Good to know:

- Each tab has its own session. Keep all statements of this transaction in the same tab.
- **Production** connections ask before changes and Commit; writes are recorded in **Audit log**. **Roll back** remains available to release a transaction.
- Rollback applies to transactional row changes. DDL such as ALTER TABLE can commit implicitly; see the [MySQL transaction rules](https://dev.mysql.com/doc/refman/8.4/en/commit.html) and [MariaDB transaction rules](https://mariadb.com/docs/server/reference/sql-statements/transactions/start-transaction).

## Explain a query and open it in Plans

Use this when you want to inspect a SELECT's estimated operators without running it for measurements.

1. In **Query editor**, place the cursor inside the SELECT you want to inspect.
2. Choose **JSON** in **Explain format** beside **Explain**.
3. Choose **Explain**.
4. Choose **Explore** above the Explain result.
5. In **Plans** > **Explore**, choose **Visual Explain** or **Execution tree** to inspect the operators.

What you see: the query opens as an estimated visual plan in **Plans**. **Stats** shows the available counts and estimates.

Good to know:

- **Explore** captures a visual plan for SELECT statements only. It reads the plan again in the tab's session.
- On MySQL, this capture requires a known closed transaction and autocommit on. Finish the tab's transaction before opening the visual plan.
- For measured timings, choose **Run analysis** in **Query editor** and approve its review. It executes the SELECT and uses server resources.

## Filter, sort and export query results

Use this when a loaded result contains rows you want to narrow down and save to a file.

1. In **Query editor**, run a SELECT that returns rows.
2. Type a value in **Filter loaded rows…** above the first result grid.
3. Click a column heading to sort the displayed rows.
4. Open **Export result 1** above that result; the button shows the filtered row count.
5. Choose **CSV**, **JSON** or **Markdown**.

What you see: the app downloads a file containing the rows that match the filter.

Good to know:

- Filtering and sorting apply to loaded rows only. **Load more** runs the statement again with a larger row limit, so the rows can differ.
- Sorting changes the grid's display order. Export keeps the loaded row order; use ORDER BY in SQL when the exported order matters.
- CSV writes NULL as an empty field. Before choosing **CSV**, turn on **Protect spreadsheet formulas (CSV)** if values could be interpreted as spreadsheet formulas.

## Explain a query with named parameters

Use this when you want to supply literal values for `:name` placeholders while keeping the SQL text reusable.

1. In **Query editor**, enter a SELECT using your table and column names, for example `SELECT id FROM orders WHERE id = :id;`.
2. Open **Run analysis options**, the arrow beside **Run analysis**.
3. Expand **Parameters and execution conditions**.
4. Enter `{"id": 42}` in **Named parameters · JSON object**.
5. Choose **Explain**.

What you see: Explain uses the supplied value for `:id` and leaves the placeholder in the editor text.

Good to know:

- **Run** sends SQL as written, so replace placeholders with values before using it. **Explain** and **Run analysis** substitute the Parameters values.
- **Run analysis** executes the SELECT after review; choose it only when you need measurements.

## Compare Query digest with a baseline

Use this when you want to see which statement digests are new or have grown since a saved reading.

1. Open **Query digest** in the sidebar.
2. Choose **Statement digests** in **Source**.
3. Choose **Refresh** to read the server.
4. Open **Refresh options** > **Save baseline…**.
5. Enter a **Name** for the reading.
6. Choose **Save baseline**.
7. After the workload or deployment you want to compare, choose **Refresh**.
8. Open **Refresh options** > **Compare with baseline…**.
9. Select the saved reading in **Choose a baseline**.
10. Choose **Compare**.

What you see: **Since the baseline** lists new or grown digests with before and after measurements.

Good to know:

- Baselines work with **Statement digests** only. They require a successful Performance Schema reading with the server's identity, and stay on this computer for that server.
- A server restart or digest reset limits what can be compared; read the notice above the comparison before interpreting changes.
