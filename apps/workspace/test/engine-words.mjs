// Unit suite for apps/workspace/src/editor/engineWords.ts.
// Bundles engineWords.ts with esbuild (no browser or server needed) and verifies:
// - major.minor fallback rules (exact release, uncataloged patch, Percona suffix,
//   MariaDB suffix, earlier-than-base release, unknown series)
// - case-insensitive isKeyword lookups
// - the coordinator's verified sample of MySQL and MariaDB keyword facts,
//   add/remove transitions, internal-token exclusion (PARSE_GCOL_EXPR), and
//   cross-engine differences.
// Run: cd apps/workspace && node test/engine-words.mjs
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const result = await build({
  entryPoints: [
    fileURLToPath(new URL("../src/editor/engineWords.ts", import.meta.url)),
  ],
  bundle: true,
  format: "esm",
  platform: "neutral",
  target: "es2020",
  write: false,
  logLevel: "silent",
});

const mod = await import(
  "data:text/javascript;base64," +
    Buffer.from(result.outputFiles[0].text).toString("base64")
);
const { wordsFor, isKeyword, functionsFor } = mod;

function findWord(engine, version, name) {
  const { keywords } = wordsFor(engine, version);
  return keywords.find((w) => w.name === name.toUpperCase()) ?? null;
}

// 1. Version resolution and fallback rules.
{
  // Exact cataloged releases.
  const m8036 = wordsFor("mysql", "8.0.36");
  assert.equal(m8036.release, "8.0.36");
  assert.equal(m8036.exact, true);
  assert.ok(m8036.keywords.length > 600);
  assert.ok(m8036.grammarFunctions.includes("COUNT"));

  const ma10116 = wordsFor("mariadb", "10.11.6");
  assert.equal(ma10116.release, "10.11.6");
  assert.equal(ma10116.exact, true);
  assert.ok(ma10116.keywords.length > 500);
  assert.ok(ma10116.grammarFunctions.includes("COUNT"));

  // Cached decoded release arrays are reused across calls.
  assert.equal(wordsFor("mysql", "8.0.36").keywords, m8036.keywords);

  // Uncataloged patch release falls back to the nearest earlier release in the same major.minor series.
  const m8412 = wordsFor("mysql", "8.4.12");
  assert.equal(m8412.release, "8.4.11");
  assert.equal(m8412.exact, false);
  assert.deepEqual(m8412.keywords, wordsFor("mysql", "8.4.11").keywords);

  const ma1066 = wordsFor("mariadb", "10.6.6");
  assert.equal(ma1066.release, "10.6.5");
  assert.equal(ma1066.exact, false);
  assert.deepEqual(ma1066.keywords, wordsFor("mariadb", "10.6.5").keywords);

  // Percona build suffix ("8.0.36-28") and MariaDB log suffix ("10.11.6-MariaDB-log")
  // resolve to the nearest earlier cataloged release in that major.minor series with exact: false.
  const percona = wordsFor("mysql", "8.0.36-28");
  assert.equal(percona.release, "8.0.36");
  assert.equal(percona.exact, false);
  assert.deepEqual(percona.keywords, m8036.keywords);

  const perconaUncatalogedPatch = wordsFor("mysql", "8.4.12-1");
  assert.equal(perconaUncatalogedPatch.release, "8.4.11");
  assert.equal(perconaUncatalogedPatch.exact, false);

  const mariaLog = wordsFor("mariadb", "10.11.6-MariaDB-log");
  assert.equal(mariaLog.release, "10.11.6");
  assert.equal(mariaLog.exact, false);
  assert.deepEqual(mariaLog.keywords, ma10116.keywords);

  // Version earlier than the first cataloged release in the series returns null and empty lists.
  for (const [engine, ver] of [
    ["mysql", "5.7.8"],
    ["mysql", "8.0.10"],
    ["mariadb", "10.6.2"],
    ["mariadb", "11.4.1"],
  ]) {
    const res = wordsFor(engine, ver);
    assert.deepEqual(res, {
      release: null,
      exact: false,
      keywords: [],
      grammarFunctions: [],
    });
  }

  // Uncataloged series or unparseable version string returns null and empty lists.
  for (const [engine, ver] of [
    ["mysql", "5.6.51"],
    ["mysql", "8.5.0"],
    ["mariadb", "10.5.20"],
    ["mysql", ""],
    ["mysql", "invalid"],
  ]) {
    const res = wordsFor(engine, ver);
    assert.deepEqual(res, {
      release: null,
      exact: false,
      keywords: [],
      grammarFunctions: [],
    });
  }
}

// 2. isKeyword is case-insensitive and respects fallback / unknown versions.
{
  assert.equal(isKeyword("mysql", "8.0.14", "LATERAL"), true);
  assert.equal(isKeyword("mysql", "8.0.14", "lateral"), true);
  assert.equal(isKeyword("mysql", "8.0.14", "Lateral"), true);
  assert.equal(isKeyword("mariadb", "11.7.2", "VECTOR"), true);
  assert.equal(isKeyword("mariadb", "11.7.2", "vector"), true);
  assert.equal(isKeyword("mariadb", "11.7.2", "VeCtOr"), true);
  assert.equal(isKeyword("mysql", "8.0.36-28", "select"), true);
  assert.equal(isKeyword("mariadb", "10.11.6-MariaDB-log", "sequence"), true);
  assert.equal(isKeyword("mysql", "8.0.14", "NOT_A_SQL_KEYWORD"), false);
  assert.equal(isKeyword("mysql", "5.6.51", "SELECT"), false);
  assert.equal(isKeyword("mysql", "8.0.14", ""), false);
}

// 3. Verified facts from the coordinator's sample:
// - MySQL LATERAL first appears in 8.0.14 and is reserved
// - MySQL INTERSECT first appears in 8.0.31
// - MySQL BULK first appears in 8.0.32
// - MySQL QUALIFY first appears in 8.3.0
// - MySQL SQL_CACHE present in 5.7.44 and absent in 8.0.11
// - MySQL MASTER_HOST present in 8.3.0 and absent in 8.4.0
// - PARSE_GCOL_EXPR absent everywhere (internal MySQL 5.7 .frm parser token)
// - MariaDB VECTOR first appears in 11.7.2
// - MariaDB RECORD first appears in 11.8.2
// - MariaDB AUTO present in 10.6.16 and absent in 10.6.17
// - MariaDB HOST present in 11.8.8 and absent in 11.8.9
// - HISTOGRAM is a MySQL keyword and not a MariaDB one
// - SEQUENCE is a MariaDB keyword and not a MySQL one
{
  // MySQL LATERAL first appears in 8.0.14 and is reserved.
  assert.equal(isKeyword("mysql", "5.7.44", "LATERAL"), false);
  assert.equal(isKeyword("mysql", "8.0.13", "LATERAL"), false);
  assert.deepEqual(findWord("mysql", "8.0.14", "LATERAL"), {
    name: "LATERAL",
    reserved: true,
  });

  // MySQL INTERSECT first appears in 8.0.31.
  assert.equal(isKeyword("mysql", "8.0.30", "INTERSECT"), false);
  assert.deepEqual(findWord("mysql", "8.0.31", "INTERSECT"), {
    name: "INTERSECT",
    reserved: true,
  });

  // MySQL BULK first appears in 8.0.32.
  assert.equal(isKeyword("mysql", "8.0.31", "BULK"), false);
  assert.deepEqual(findWord("mysql", "8.0.32", "BULK"), {
    name: "BULK",
    reserved: false,
  });

  // MySQL QUALIFY first appears in 8.3.0.
  assert.equal(isKeyword("mysql", "8.0.46", "QUALIFY"), false);
  assert.equal(isKeyword("mysql", "8.2.0", "QUALIFY"), false);
  assert.equal(isKeyword("mysql", "8.3.0", "QUALIFY"), true);

  // MySQL SQL_CACHE present in 5.7.44 and absent in 8.0.11.
  assert.equal(isKeyword("mysql", "5.7.44", "SQL_CACHE"), true);
  assert.equal(isKeyword("mysql", "8.0.11", "SQL_CACHE"), false);

  // MySQL MASTER_HOST present in 8.3.0 and absent in 8.4.0.
  assert.equal(isKeyword("mysql", "8.3.0", "MASTER_HOST"), true);
  assert.equal(isKeyword("mysql", "8.4.0", "MASTER_HOST"), false);

  // PARSE_GCOL_EXPR absent everywhere across MySQL and MariaDB releases.
  const sampleReleases = [
    ["mysql", "5.7.9"],
    ["mysql", "5.7.44"],
    ["mysql", "8.0.11"],
    ["mysql", "8.0.46"],
    ["mysql", "8.4.0"],
    ["mysql", "9.7.4"],
    ["mysql", "26.10.0"],
    ["mariadb", "10.6.3"],
    ["mariadb", "10.11.19"],
    ["mariadb", "11.4.13"],
    ["mariadb", "11.8.9"],
    ["mariadb", "13.0.2"],
  ];
  for (const [engine, ver] of sampleReleases) {
    assert.equal(
      isKeyword(engine, ver, "PARSE_GCOL_EXPR"),
      false,
      `PARSE_GCOL_EXPR must be absent in ${engine} ${ver}`,
    );
  }

  // MariaDB VECTOR first appears in 11.7.2.
  assert.equal(isKeyword("mariadb", "11.6.2", "VECTOR"), false);
  assert.equal(isKeyword("mariadb", "11.7.2", "VECTOR"), true);

  // MariaDB RECORD first appears in 11.8.2.
  assert.equal(isKeyword("mariadb", "11.7.2", "RECORD"), false);
  assert.equal(isKeyword("mariadb", "11.8.2", "RECORD"), true);

  // MariaDB AUTO present in 10.6.16 and absent in 10.6.17.
  assert.equal(isKeyword("mariadb", "10.6.16", "AUTO"), true);
  assert.equal(isKeyword("mariadb", "10.6.17", "AUTO"), false);

  // MariaDB HOST present in 11.8.8 and absent in 11.8.9.
  assert.equal(isKeyword("mariadb", "11.8.8", "HOST"), true);
  assert.equal(isKeyword("mariadb", "11.8.9", "HOST"), false);

  // HISTOGRAM is a MySQL keyword and not a MariaDB one.
  assert.equal(isKeyword("mysql", "8.0.11", "HISTOGRAM"), true);
  assert.equal(isKeyword("mysql", "8.4.0", "HISTOGRAM"), true);
  assert.equal(isKeyword("mariadb", "10.6.3", "HISTOGRAM"), false);
  assert.equal(isKeyword("mariadb", "11.4.2", "HISTOGRAM"), false);
  assert.equal(isKeyword("mariadb", "13.0.2", "HISTOGRAM"), false);

  // SEQUENCE is a MariaDB keyword and not a MySQL one.
  assert.equal(isKeyword("mariadb", "10.6.3", "SEQUENCE"), true);
  assert.equal(isKeyword("mariadb", "11.4.2", "SEQUENCE"), true);
  assert.equal(isKeyword("mysql", "5.7.44", "SEQUENCE"), false);
  assert.equal(isKeyword("mysql", "8.0.11", "SEQUENCE"), false);
  assert.equal(isKeyword("mysql", "8.4.0", "SEQUENCE"), false);
  assert.equal(isKeyword("mysql", "26.10.0", "SEQUENCE"), false);
}

// 4. Native function arity independently verified with git cat-file against
// the listed source tags. Bounds are envelopes, so ODD/EVEN do not claim parity.
{
  const findFunction = (engine, version, name) =>
    functionsFor(engine, version).functions.find(fn => fn.name === name) ?? null;
  const expectBounds = (engine, version, name, min, max) =>
    assert.deepEqual(findFunction(engine, version, name), { name, min, max });

  // mysql-8.0.36:sql/item_create.cc:1519-1520, both names use Item_func_pow.
  expectBounds("mysql", "8.0.36", "POW", 2, 2);
  expectBounds("mysql", "8.0.36", "POWER", 2, 2);
  // mysql-8.0.36:sql/item_create.cc:1392; MAX_ARGLIST_SIZE defined at 129-130.
  expectBounds("mysql", "8.0.36", "CONCAT", 1, "unbounded");
  // mysql-8.0.36:sql/item_create.cc:1453-1454, SQL_FN_EVEN starts at zero.
  expectBounds("mysql", "8.0.36", "JSON_OBJECT", 0, "unbounded");
  // mysql-8.0.36:sql/item_create.cc:1457, SQL_FN_ODD starts at three.
  expectBounds("mysql", "8.0.36", "JSON_SET", 3, "unbounded");
  // mysql-8.0.36:sql/item_create.cc:1721-1722, internal registry entry.
  assert.equal(findFunction("mysql", "8.0.36", "INTERNAL_AUTO_INCREMENT"), null);
  // mysql-5.7.44:sql/item_create.cc:3440 (arg2), 7749-7750 (both aliases).
  expectBounds("mysql", "5.7.44", "POW", 2, 2);
  expectBounds("mysql", "5.7.44", "POWER", 2, 2);
  // mariadb-13.0.2:sql/item_create.cc:130 (arg2), 6330 (registration).
  expectBounds("mariadb", "13.0.2", "ADD_MONTHS", 2, 2);
  // mysql-8.0.36:sql/item_create.cc:1371-1788 has no ADD_MONTHS entry.
  assert.equal(findFunction("mysql", "8.0.36", "ADD_MONTHS"), null);
  // mariadb-13.0.2:sql/item_create.cc:3496-3502 rejects counts below one.
  expectBounds("mariadb", "13.0.2", "CONCAT", 1, "unbounded");
  // mariadb-13.0.2:sql/item_geofunc.cc:3716 (arg2), 4913 (registration).
  expectBounds("mariadb", "13.0.2", "ST_DISTANCE", 2, 2);
  // mariadb-13.0.2:sql/item_create.cc:5212-5233, guard before constructor switch.
  expectBounds("mariadb", "13.0.2", "MASTER_GTID_WAIT", 1, 2);

  // mysql-8.0.22:sql/item_create.cc has neither entry; mysql-8.0.23 adds
  // ST_FRECHETDISTANCE at 1523 and ST_HAUSDORFFDISTANCE at 1539-1540.
  for (const name of ["ST_FRECHETDISTANCE", "ST_HAUSDORFFDISTANCE"]) {
    assert.equal(findFunction("mysql", "8.0.22", name), null);
    expectBounds("mysql", "8.0.23", name, 2, 3);
  }

  // Every grammar hint remains present, but no arity is invented for COUNT.
  for (const [engine, version] of [["mysql", "8.0.36"], ["mariadb", "13.0.2"]]) {
    const result = functionsFor(engine, version);
    expectBounds(engine, version, "COUNT", null, null);
    for (const name of wordsFor(engine, version).grammarFunctions) {
      assert.ok(result.functions.some(fn => fn.name === name), `${engine} missing ${name}`);
    }
    const names = result.functions.map(fn => fn.name);
    assert.deepEqual(names, [...new Set(names)].sort(), "hints are sorted and deduplicated");
    assert.equal(functionsFor(engine, version).functions, result.functions);
  }

  // Function hints and keywords share exact, suffix, and nearest-earlier policy.
  for (const [engine, version] of [
    ["mysql", "8.0.36"], ["mysql", "8.0.36-28"], ["mysql", "8.4.12"],
    ["mariadb", "10.11.6-MariaDB-log"], ["mariadb", "10.6.6"],
  ]) {
    const words = wordsFor(engine, version);
    const functions = functionsFor(engine, version);
    assert.equal(functions.release, words.release);
    assert.equal(functions.exact, words.exact);
    assert.deepEqual(functions.functions, functionsFor(engine, words.release).functions);
  }
  for (const [engine, version] of [
    ["mysql", "5.7.8"], ["mysql", "8.0.10"], ["mariadb", "11.4.1"],
    ["mysql", "5.6.51"], ["mariadb", "10.5.20"], ["mysql", "invalid"], ["mysql", ""],
  ]) {
    assert.deepEqual(functionsFor(engine, version), { release: null, exact: false, functions: [] });
  }
}

console.log(
  "engine-words: fallback rules, case-insensitive lookup, and verified MySQL/MariaDB keyword and function facts passed.",
);
