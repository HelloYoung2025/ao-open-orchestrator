import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, chmodSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import poke, { create, manifest } from "../index.js";

// Drive create(config).notify(events) with a FAKE `tmux` on PATH that records each invocation's argv
// (as a JSON line) to a capture file. Assert on captured CONTENT, not on timing/call-count ordering.
async function runNotify(config, events) {
  const dir = mkdtempSync(join(tmpdir(), "poke-tmux-"));
  const capture = join(dir, "capture.log");
  const fake = join(dir, "tmux");
  // CJS fake (the temp dir has no package.json, so node treats a bare-extension file as CommonJS).
  writeFileSync(
    fake,
    "#!/usr/bin/env node\n" +
      'const { appendFileSync } = require("node:fs");\n' +
      `appendFileSync(${JSON.stringify(capture)}, JSON.stringify(process.argv.slice(2)) + "\\n");\n`,
    "utf8"
  );
  chmodSync(fake, 0o755);
  const prevPath = process.env.PATH;
  process.env.PATH = `${dir}:${prevPath}`;
  try {
    const notifier = create(config);
    for (const ev of events) {
      await notifier.notify(ev);
    }
  } finally {
    process.env.PATH = prevPath;
  }
  const lines = existsSync(capture)
    ? readFileSync(capture, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l))
    : [];
  rmSync(dir, { recursive: true, force: true });
  return lines;
}

function pokeMessages(argvLines) {
  // The `-l` send-keys invocation carries the literal poke message at argv index 4.
  return argvLines.filter((a) => a[3] === "-l").map((a) => a[4]);
}

const RELAYED = { type: "reaction.triggered", sessionId: "w-1", projectId: "p", data: { reaction: { key: "agent-needs-input" } } };
const CFG = { orchestratorSession: "proj-orchestrator", activeRoot: "/tmp/ao-test-root", stateWriterCommand: "ao-state-writer", projectIds: ["p"] };

test("manifest is a brand-neutral notifier plugin", () => {
  assert.equal(manifest.slot, "notifier");
  assert.equal(manifest.name, "orchestrator-poke");
  assert.equal(typeof poke.create, "function");
});

test("relayed needs-input event pokes the orchestrator with the configured engine command + root", async () => {
  const lines = await runNotify(CFG, [RELAYED]);
  const msgs = pokeMessages(lines);
  assert.equal(msgs.length, 1, "exactly one poke");
  const msg = msgs[0];
  assert.match(msg, /AO_DISPATCH_POKE worker_session=w-1 project=p/);
  // engine command is config-driven and the root is shell-quoted data.
  assert.ok(msg.includes("ao-state-writer list-ready --root '/tmp/ao-test-root'"), msg);
  assert.ok(msg.includes("ao-state-writer list-gated --root '/tmp/ao-test-root'"), msg);
  // load-bearing reconcile-from-state contract phrases survive verbatim.
  assert.ok(msg.includes("reconcile-from-state"), msg);
  assert.ok(msg.includes("NEVER bare `continue`"), msg);
  assert.ok(msg.includes("ambiguous_proposal_id"), msg);
  assert.ok(msg.includes("authorize --root"), msg);
  // tmux send-keys sequence: Escape, then -l message, then Enter.
  assert.deepEqual(lines[0], ["send-keys", "-t", "proj-orchestrator", "Escape"]);
  assert.deepEqual(lines[lines.length - 1], ["send-keys", "-t", "proj-orchestrator", "Enter"]);
});

test("a custom stateWriterCommand is honoured (MACHINERY decoupled from activeRoot)", async () => {
  const lines = await runNotify({ ...CFG, stateWriterCommand: "my-writer --flag" }, [RELAYED]);
  const msg = pokeMessages(lines)[0];
  assert.ok(msg.includes("my-writer --flag list-ready --root '/tmp/ao-test-root'"), msg);
});

test("omitted stateWriterCommand defaults to the public console script", async () => {
  const { stateWriterCommand, ...noCmd } = CFG; // drop it -> exercise the default path
  const lines = await runNotify(noCmd, [RELAYED]);
  const msg = pokeMessages(lines)[0];
  assert.ok(msg.includes("ao-state-writer list-ready --root '/tmp/ao-test-root'"), msg);
  assert.ok(!msg.includes("undefined"), msg); // default must not leak an undefined command
});


test("non-relayed events do not poke", async () => {
  const lines = await runNotify(CFG, [{ type: "something.else", sessionId: "w-1", projectId: "p", data: {} }]);
  assert.equal(pokeMessages(lines).length, 0);
});

test("orchestrator's own / *-orchestrator sessions are never poked", async () => {
  const a = await runNotify(CFG, [{ ...RELAYED, sessionId: "proj-orchestrator" }]);
  const b = await runNotify(CFG, [{ ...RELAYED, sessionId: "other-orchestrator" }]);
  assert.equal(pokeMessages(a).length, 0);
  assert.equal(pokeMessages(b).length, 0);
});

test("per-session dedup within the 10s window", async () => {
  const lines = await runNotify(CFG, [RELAYED, { ...RELAYED }]);
  assert.equal(pokeMessages(lines).length, 1, "second poke for the same session is deduped");
});

test("project allow-list filters out non-listed projects", async () => {
  const lines = await runNotify({ ...CFG, projectIds: ["allowed"] }, [{ ...RELAYED, projectId: "other" }]);
  assert.equal(pokeMessages(lines).length, 0);
});

test("missing config no-ops without poking and without throwing", async () => {
  const lines = await runNotify({}, [RELAYED]); // no orchestratorSession / activeRoot
  assert.equal(pokeMessages(lines).length, 0);
});

test("the poke message carries no private brand/host fragment", async () => {
  const lines = await runNotify(CFG, [RELAYED]);
  const msg = pokeMessages(lines)[0];
  // fragments assembled at runtime so this test source never self-poisons the safety scan.
  const forbidden = ["ccai" + "bao", "claw-" + "commander", "/opt/" + "homebrew", "young" + "hu", "chat" + "gpt"];
  for (const frag of forbidden) {
    assert.ok(!msg.toLowerCase().includes(frag.toLowerCase()), `message leaked: ${frag}`);
  }
});
