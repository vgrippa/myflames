// The Files place end to end against a real OpenSSH server, with no mocks:
// the real WorkspaceServer (throwaway HOME, vault secrets), the built
// workspace, and the lab from scripts/ssh-lab.sh. Skipped unless
// MYFLAMES_SSH_LAB is set (source local/ssh-lab/lab.env first). Covers the
// sidebar place, New SSH connection with a pasted user@host:port, the key
// pick list from ~/.ssh, Test connection through the host-key prompt (the
// fingerprint checked against ssh-keygen), Save, both panes' first listings,
// opening a remote folder, Download and Upload through the Transfers panel
// (bytes checked on both sides), Delete with its confirmation, and light and
// dark screenshots at 1440 and 720 px (FILES_SHOTS=<dir>, default
// test/shots-files-live).
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
if (!process.env.MYFLAMES_SSH_LAB) {
  console.log("files-live: skipped (set MYFLAMES_SSH_LAB: scripts/ssh-lab.sh up, then source local/ssh-lab/lab.env)");
  process.exit(0);
}
const [labHost, labPort] = process.env.MYFLAMES_SSH_LAB.split(":");
const labUser = process.env.MYFLAMES_SSH_LAB_USER;
const container = process.env.MYFLAMES_SSH_LAB_CONTAINER || "myflames-sshlab";
const shots = process.env.FILES_SHOTS || path.join(here, "shots-files-live");
const lab = (command) => execFileSync("docker", ["exec", container, "sh", "-c", command]).toString();

// The local pane's home and ~/.ssh: a throwaway folder with the lab key in it.
const home = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), "myflames-files-live-")));
await fs.mkdir(path.join(home, ".ssh"), { mode: 0o700 });
const key = path.join(home, ".ssh", "id_lab");
await fs.copyFile(process.env.MYFLAMES_SSH_LAB_KEY, key);
await fs.chmod(key, 0o600);
await fs.copyFile(`${process.env.MYFLAMES_SSH_LAB_KEY}.pub`, `${key}.pub`);
lab("rm -f /home/lab/data/local-upload.txt");

const proc = spawn(process.env.PYTHON || "python3",
  ["-c", "import sys, myflames.ui as u; s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()",
    path.join(home, "workspace.sqlite3")],
  { cwd: root, env: { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault", MYFLAMES_SSH_HOME: home, SSH_AUTH_SOCK: "", MYFLAMES_SSH_AUTH_SOCK: "" } });
const url = await new Promise((resolve, reject) => {
  let text = "";
  const timer = setTimeout(() => reject(Error(text)), 15000);
  proc.stderr.on("data", (chunk) => {
    text += chunk;
    const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
    if (match) { clearTimeout(timer); resolve(match[0]); }
  });
});

const browser = await puppeteer.launch({ headless: "shell" });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url);
  const pane = (side) => `.files-pane[data-side="${side}"]`;
  const row = (side, name) => `${pane(side)} [role="row"][data-name="${name}"]`;
  const pathOf = (side) => page.$eval(`${pane(side)} .files-crumbs`, (e) => e.dataset.path);
  const typeInto = async (selector, text) => { await page.click(selector, { clickCount: 3 }); await page.type(selector, text); };
  const waitText = (text, timeout = 30000) => page.waitForFunction((t) => document.body.innerText.includes(t), { timeout }, text);

  // The place is in the sidebar and opens on its empty state.
  await page.waitForSelector('.main-nav button[data-place="files"]', { timeout: 30000 });
  await page.click('.main-nav button[data-place="files"]');
  await waitText("Save an SSH connection to move files between this computer and a server.");
  await fs.mkdir(shots, { recursive: true });
  // The empty state fits its narrow column: nothing overflows sideways.
  const overflow = await page.$eval(".ssh-connections", (e) => e.scrollWidth - e.clientWidth);
  assert.ok(overflow <= 1, `the SSH connection list overflows by ${overflow} px`);
  await page.screenshot({ path: path.join(shots, "empty-light-1440.png") });

  // New SSH connection: a pasted user@host:port fills User and Port; Name follows.
  await page.click(".ssh-connections-head .button");
  await page.waitForSelector("#ssh-conn-host");
  await typeInto("#ssh-conn-host", `${labUser}@${labHost}:${labPort}`);
  await page.$eval("#ssh-conn-host", (e) => e.blur());
  assert.equal(await page.$eval("#ssh-conn-host", (e) => e.value), labHost);
  assert.equal(await page.$eval("#ssh-conn-user", (e) => e.value), labUser);
  assert.equal(await page.$eval("#ssh-conn-port", (e) => e.value), labPort);
  assert.equal(await page.$eval("#ssh-conn-name", (e) => e.value), `${labUser}@${labHost}`);
  await typeInto("#ssh-conn-name", "lab");
  // Key file: the pick list reads ~/.ssh (keys with a .pub beside them).
  await page.evaluate(() => [...document.querySelectorAll(".ssh-auth button")].find((b) => b.textContent === "Key file").click());
  await page.waitForFunction((k) => [...document.querySelectorAll("#ssh-conn-key option")].some((o) => o.value === k), {}, key);
  await page.select("#ssh-conn-key", key);

  // Test connection: an unknown host opens the trust prompt with the real fingerprint.
  await page.click(".ssh-test-button");
  await page.waitForSelector("code.hostkey-fingerprint", { timeout: 30000 });
  const shown = await page.$$eval("code.hostkey-fingerprint", (els) => els.map((e) => e.textContent));
  const real = execFileSync("sh", ["-c", `ssh-keyscan -p ${labPort} ${labHost} 2>/dev/null | ssh-keygen -l -E sha256 -f -`]).toString();
  for (const fingerprint of shown) assert.ok(real.includes(fingerprint), `fingerprint ${fingerprint} is the server's`);
  assert.match(await page.$eval(".hostkey-sheet h2", (e) => e.textContent), new RegExp(`Trust \\[${labHost.replace(/\./g, "\\.")}\\]:${labPort}\\?`));
  await page.screenshot({ path: path.join(shots, "hostkey-light-1440.png") });
  await page.click(".hostkey-trust-button");
  await waitText(`Home folder: /home/${labUser}`);
  await page.click(".ssh-save-button");
  await page.waitForSelector(".ssh-sheet", { hidden: true });

  // The browser opens: the server at its home, this computer at its home.
  await page.waitForFunction((s) => document.querySelector(`${s} .files-crumbs`)?.dataset.path === "/home/lab", { timeout: 30000 }, pane("remote"));
  assert.equal(await pathOf("local"), home);
  await page.waitForSelector(row("remote", "data"));
  await page.click(row("remote", "data"), { clickCount: 2 });
  await page.waitForFunction((s) => document.querySelector(`${s} .files-crumbs`)?.dataset.path === "/home/lab/data", {}, pane("remote"));
  await page.waitForSelector(row("remote", "big.bin"));

  // Download hello.txt: the Transfers panel reports it and the bytes land here.
  await page.click(row("remote", "hello.txt"));
  await page.click(`${pane("remote")} .files-transfer-button`);
  await page.waitForSelector('li.files-job[data-state="finished"]', { timeout: 60000 });
  assert.equal(await fs.readFile(path.join(home, "hello.txt"), "utf8"), "hello\n");
  await page.waitForSelector(row("local", "hello.txt"), { timeout: 15000 });

  // Upload a file of this computer into the folder on screen.
  await fs.writeFile(path.join(home, "local-upload.txt"), "from myflames\n");
  await page.click(`${pane("local")} button[aria-label^="Refresh"]`);
  await page.waitForSelector(row("local", "local-upload.txt"));
  await page.click(row("local", "local-upload.txt"));
  await page.click(`${pane("local")} .files-transfer-button`);
  await page.waitForFunction(() => document.querySelectorAll('li.files-job[data-state="finished"]').length >= 2, { timeout: 60000 });
  assert.equal(lab("cat /home/lab/data/local-upload.txt"), "from myflames\n");
  await page.waitForSelector(row("remote", "local-upload.txt"), { timeout: 15000 });

  // Screenshots: light and dark at the real window sizes.
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const [width, height] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width, height });
      await new Promise((r) => setTimeout(r, 300));
      await page.screenshot({ path: path.join(shots, `files-${theme}-${width}.png`) });
    }
  }
  await page.setViewport({ width: 1440, height: 900 });

  // Delete the uploaded file from the server, after its confirmation.
  await page.click(row("remote", "local-upload.txt"));
  await page.keyboard.press("Delete");
  await page.waitForFunction(() => /Delete 1 item/.test(document.body.innerText), { timeout: 10000 });
  await page.evaluate(() => [...document.querySelectorAll("dialog button")].find((b) => b.textContent.trim() === "Delete").click());
  await page.waitForSelector(row("remote", "local-upload.txt"), { hidden: true, timeout: 15000 });
  assert.equal(lab("ls /home/lab/data | grep -c local-upload || true").trim(), "0");

  assert.deepEqual(errors, []);
  console.log(`files-live: ok (screenshots in ${path.relative(process.cwd(), shots)})`);
} finally {
  await browser.close();
  proc.kill();
  await fs.rm(home, { recursive: true, force: true });
}
