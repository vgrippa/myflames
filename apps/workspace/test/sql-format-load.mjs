// Loads src/sqlFormatCheck.ts and src/sqlFormat.ts (with sql-formatter) in
// Node for the unit tests: each module is compiled with Vite's esbuild
// transform into a temporary directory, its relative imports pointing at the
// compiled siblings and "sql-formatter" at the installed package.
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { transformWithEsbuild } from "vite";

const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-sql-format-"));
const formatter = import.meta.resolve("sql-formatter");
async function compile(name) {
  const source = await fs.readFile(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
  let { code } = await transformWithEsbuild(source, `${name}.ts`, { loader: "ts", format: "esm", target: "es2020" });
  code = code
    .replace(/from "sql-formatter"/g, `from ${JSON.stringify(formatter)}`)
    .replace(/from "\.\/([A-Za-z]+)"/g, (_, module) => `from ${JSON.stringify(pathToFileURL(path.join(directory, `${module}.mjs`)).href)}`);
  const file = path.join(directory, `${name}.mjs`);
  await fs.writeFile(file, code);
  return pathToFileURL(file).href;
}
await compile("sqlSelection");
export const check = await import(await compile("sqlFormatCheck"));
export const format = await import(await compile("sqlFormat"));
export async function cleanup() {
  await fs.rm(directory, { recursive: true, force: true });
}
