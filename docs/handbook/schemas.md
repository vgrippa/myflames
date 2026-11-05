# Schemas & version comparison

Database diagram helps you explore the relationships in a schema. Schema versions lets you save definitions locally and compare them with another version, a file, or a live database.

## Compare the intended source with the target

The **source** is the schema to match. The **target** is the schema that would change. The comparison lists differing objects and objects found only on one side, with definitions side by side.

Generated sync SQL changes the target to match the source. myflames does not run the generated script. Objects found only in the target are left out until you explicitly choose them. Changes within a selected object can still drop columns or replace objects, and carry warnings.

```bash
myflames schema save -p --profile prod -D shop --name "before release"
myflames schema compare latest live -p --profile prod -D shop
myflames schema compare profile:prod/shop profile:staging/shop \
  --sql -o sync-staging.sql
```

These examples assume saved `prod` and `staging` profiles and the `shop` database.

## Inspect the proposed change

Read Drops data, May fail and rebuild warnings. ALTER algorithm predictions are comments that describe the expected behavior, not a guarantee that the operation will be inexpensive. Engine and version limits apply; MySQL 5.7 ALTER algorithm prediction is not available.

Checks can flag definitions such as FLOAT money columns, utf8mb3 and missing foreign-key indexes. Not judged means the check could not establish a verdict.

## Export a schema as SQL

```bash
myflames schema export latest --sql --create-database -o shop.sql
```

The export orders tables by foreign-key dependencies and omits definers by default. It is a script to review and run through your chosen process. Exporting it does not apply it to a server.
