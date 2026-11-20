# Schemas & version comparison

Database diagram helps you explore the relationships in a schema. Schema versions lets you save definitions locally and compare them with another version, a file, or a live database. Step-by-step guides live in [Plans and schemas](howto-plans.md).

## Browse a schema

The Query editor's **Schemas** panel browses databases, tables, views, columns, indexes, routines, triggers and events of the connection in use; see [Browse and edit table data](queries.md#browse-and-edit-table-data). **Server > Tables** lists the tables of a database with their size and health, and its row menu opens **Table structure**, **Show CREATE TABLE (DDL)** and **Import CSV…** for one table.

## Table structure

**Table structure** (from **Server > Tables**, or the inspector's action bar) opens a sheet with **Columns**, **Indexes** and **Foreign keys** tabs for one table. It reads once when it opens; **Try again** after a failure reads again.

## Database diagram

**Database diagram** reads the tables, columns, indexes and foreign keys of a database through the connection in use; nothing on the server changes. Choose a **Database** and **Load** it, and **Refresh** reads the diagram on screen again; **Reload database list** re-reads the database names. **Import** opens a SQL, Mermaid or diagram JSON file instead; imported SQL is never run.

The **Diagram** and **Mermaid** views show the same diagram. Drag tables to arrange them (arrow keys move a focused table), **Find a table or column** searches, **Fit** fits the diagram to the window, and **Auto layout** arranges the tables. Use the mouse wheel or trackpad to zoom around the pointer, and adjust **Diagram wheel zoom speed** in **Settings > Workspace** to make each step slower or faster.

**Export** saves the diagram as **Editable JSON**, an **SVG image**, the **SQL schema** or **Mermaid** code.

## Schema versions, lint checks and migration scripts

In **Schema versions**, **Save version…** keeps a database's schema on this computer under **Saved versions**, and **Import…** reads a schema version file (JSON) back in. A saved version's actions are **Compare with the database now**, **Compare with…** (another saved version, a file or a database read now, on the same server or on different ones), **Export** (the version as a JSON file), **Export as SQL…**, **Rename…** and **Delete…**.

The **source** is the schema to match. The **target** is the schema that would change. The **Compare** tab lists differing objects and objects found only on one side, with definitions side by side, under **Differences**; **Show identical** adds the objects that already match, and **Ignore definers** leaves out differences that are only the DEFINER of a view, routine, trigger or event.

The **Sync script** tab holds the generated SQL that changes the target to match the source. myflames does not run the generated script. Objects found only in the target are left out until you choose them with **Include in sync script**. Changes within a selected object can still drop columns or replace objects, and carry warnings. The script can be copied (**Copy script**), saved (**Download .sql**) or opened in the Query editor (**Open in Query editor**) to review; myflames never runs it. **Export comparison** saves the comparison as **Markdown (.md)**, **JSON (.json)** or the **Sync script (.sql)**.

Read the **Drops data**, **May fail** and **Rebuilds table** warnings before using a script. ALTER algorithm predictions are comments that describe the expected behavior, not a guarantee that the operation will be inexpensive. Engine and version limits apply; MySQL 5.7 ALTER algorithm prediction is not available.

**Checks** can flag definitions such as FLOAT money columns, utf8mb3 and missing foreign-key indexes. **Not judged** means the check could not establish a verdict.

## Export as SQL

**Export as SQL…**, from the **Schema versions** toolbar for a database read now, or from a saved version's actions, builds a script that recreates the schema on another server. The sheet chooses the **Database to read**, the **Name in the script**, whether to **Create the database** first, and whether to **Keep definers** (off by default: the account that runs the script becomes the definer of views, routines, triggers and events).

The export orders tables by foreign-key dependencies and lists its warnings under **Review before running**. It is a script to review and run through your chosen process: **Copy script**, **Download .sql** or **Open in Query editor**. Exporting it does not apply it to a server.
