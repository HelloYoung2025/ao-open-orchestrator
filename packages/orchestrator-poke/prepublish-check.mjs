// Package-local publish-time leak guard (self-contained — does NOT depend on the repo layout, so the
// package stays safe even if published from a standalone checkout). Fails `npm publish` if any private
// brand/host fragment slips into the shipped sources. The repo-level scripts/public_safety_scan.py is
// the broader gate; this is the package's own last line of defense.
//
// Forbidden fragments are assembled from adjacent string pieces so THIS guard never trips the
// repo-level scanner over its own source.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const forbidden = [
  "ccai" + "bao",
  "claw-" + "commander",
  "Claw" + "Code",
  "/opt/" + "homebrew",
  "young" + "hu",
  "chat" + "gpt",
  "gpt" + "_pro",
  "gpt" + "-pro",
  "@cl" + "aw",
  "/" + "Users/",
  "/" + "home/",
];
const targets = ["index.js", "package.json", "README.md"];
const findings = [];
for (const name of targets) {
  let text;
  try {
    text = readFileSync(join(here, name), "utf8");
  } catch {
    continue;
  }
  const lower = text.toLowerCase();
  for (const frag of forbidden) {
    if (lower.includes(frag.toLowerCase())) findings.push(`${name}: ${frag}`);
  }
}
// Robust structural check: a publishable package must not be marked private.
try {
  const pkg = JSON.parse(readFileSync(join(here, "package.json"), "utf8"));
  if (pkg.private === true) findings.push("package.json: private must not be true for a publishable package");
} catch {
  findings.push("package.json: unreadable/unparseable");
}
if (findings.length) {
  console.error("orchestrator-poke leak check FAILED:\n" + findings.join("\n"));
  process.exit(1);
}
console.log("orchestrator-poke leak check passed");
