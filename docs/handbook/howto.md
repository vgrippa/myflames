# How-to guides

Follow these steps to try common tasks in the desktop app. Shortcuts show macOS keys first, then Windows and Linux keys.

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
2. Press **⌘F** / **Ctrl+F** and type `status` in **Find**. Press **⌘G** / **Ctrl+G** to move to the next match while Find is open.
3. Press **⌥⌘F** / **Ctrl+H** to open Replace, then type `state` in **Replace with**.
4. Check the highlighted matches. Choose **Replace** to change one match or **Replace all** to change all matches in the tab.
5. Click back in the editor and press **⌘Z** / **Ctrl+Z** to undo the replacement in one step.

![In a Query editor tab with repeated status names, press Command+F on macOS or Ctrl+F on Windows and Linux and search for status. While Find is open, Command+G or Ctrl+G advances to the next match. Press Option+Command+F or Ctrl+H, enter state in Replace with and check the highlighted matches. Choose Replace for one match or Replace all for the whole tab. Click in the editor and press Command+Z or Ctrl+Z to undo the replacement in one step.](../screenshots/howto-find-replace.gif)

Related: [Work with SQL and results](queries.md#work-with-sql-and-results).

## Settings and themes

Use Settings to choose a theme and change how the Query editor behaves.

1. Click the gear at the bottom of the sidebar, or press **⌘,** / **Ctrl+,**, to open **Settings**.
2. Choose **Appearance**. Select **Dark** to try it across the app, then **System** to follow your operating system's appearance again. There are 17 choices.
3. Choose **Query editor**. Switch **Multiple cursors** off, then on again, to try the control. Leave it on to use the multiple-cursor guide.
4. Press **Esc** or choose **Back** to return to your previous view. Your theme and editor settings are kept on this computer across app restarts.

![Click the sidebar gear or press Command+comma on macOS or Ctrl+comma on Windows and Linux to open Settings. Choose Appearance and select Dark to try it, then System to follow the operating system again; there are 17 choices. Choose Query editor and switch Multiple cursors off and back on. Press Esc or choose Back to return to your previous view. Theme and editor settings are saved on this computer across restarts.](../screenshots/howto-settings-theme.gif)

Related: [Appearance](quickstart.md#appearance) and [Query editor settings](queries.md#edit-sql-suggestions-and-checks).

## Layout

Use the layout controls to give navigation, SQL or results more room.

1. Open **Query editor** and run a SELECT from your database with **⌘Return** / **Ctrl+Enter** to show results below the editor.
2. Drag the sidebar's right edge to make it wider. For the keyboard alternative, press **Tab** until the edge has focus and use **Left** or **Right**; hold **Shift** for a larger step. Narrow windows use a compact navigation rail instead.
3. Drag the divider between the SQL editor and its results down to make the editor taller, or up to give the results more room.
4. Double-click the sidebar's right edge to restore its default width. With the edge focused, **Home** or **End** also restores it. Your chosen sidebar width survives app restarts.
5. Look at the window status bar's right end for the installed version, **Check for updates** and **View releases**. The same bar appears on every screen.

![Open Query editor and run a SELECT with Command+Return on macOS or Ctrl+Enter on Windows and Linux to show results. Drag the sidebar's right edge wider, or focus it with Tab and use Left or Right, holding Shift for larger steps. Narrow windows keep a compact rail. Drag the divider between SQL and results down for more editor space or up for more result space. Double-click the sidebar edge, or focus it and press Home or End, to restore its default width. Sidebar width is kept across restarts. Every screen's bottom status bar shows the installed version, Check for updates and View releases on the right.](../screenshots/howto-layout.gif)

Related: [Sidebar and status bar](quickstart.md#sidebar-and-status-bar) and [Query workspace layout](../QUERY_WORKSPACE.md).

## Run and explain

Use Run to see a SELECT's results and Explain to inspect how the server plans to find them.

1. Open **Query editor**, choose a connection and **Database**, and enter a SELECT for a table there. The recording uses `SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24;`.
2. Click inside the statement and press **⌘Return** / **Ctrl+Enter**, or choose **Run**. Read the result rows below the editor.
3. Choose **Table** beside **Explain**, then choose **Explain** (**⌘E** / **Ctrl+E**). Its result shows the server's estimated plan; **Run analysis** executes the SELECT to measure it.
4. In the Explain result, choose **Explore** to open the visual plan in **Plans**. On MySQL, first commit or roll back any open transaction and turn autocommit on. Inspect the operators in Visual Explain, Flame graph, Tree or Bar chart.

![Open Query editor, choose a connection and database, and enter SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24, using a table and columns from your database. Click in the statement and press Command+Return on macOS or Ctrl+Enter on Windows and Linux, or choose Run, to see results. Choose Table beside Explain, then Explain or Command+E or Ctrl+E for the estimated plan; Run analysis executes the SELECT for measurements. Choose Explore in the Explain result to open Plans and inspect Visual Explain, Flame graph, Tree or Bar chart. On MySQL, first finish any open transaction and turn autocommit on.](../screenshots/howto-run-explain.gif)

Related: [Work with SQL and results](queries.md#work-with-sql-and-results) and [Explain a statement](queries.md#explain-a-statement).
