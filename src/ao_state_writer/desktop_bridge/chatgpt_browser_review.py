#!/usr/bin/env python3
"""Headless-browser (CDP) GPT Pro review actuator.

Drop-in replacement for the fragile GUI desktop_bridge script. Implements the same
contract the Python bridge (gpt_pro_desktop_bridge.py) invokes:

    chatgpt_browser_review.(sh|py) run <package_path> <prompt_path> <raw_out> <timeout_s>

It drives the REAL logged-in chatgpt.com web app over the Chrome DevTools Protocol
(no screen automation, no clipboard, no osascript), selects the GPT Pro model
("进阶专业" / "ChatGPT Pro"), attaches the review package, submits the prompt,
bounded-polls until the response completes, and writes the assistant transcript to
<raw_out>. The Python bridge then classifies the VERDICT line and records the receipt
through the sanctioned gpt-pro-actuate path (caller_type=gpt_pro_review_actuator set by
the CLI, never self-asserted by the orchestrator).

Env (all optional, sane defaults):
  AO_GPT_PRO_CDP_URL          CDP endpoint of the logged-in Chrome (default http://127.0.0.1:9222)
  AO_GPT_PRO_BROWSER_MAX_S    hard cap on the completion poll (default 2700s)
  AO_GPT_PRO_MODEL_LABELS     comma list of acceptable GPT Pro model labels
"""
from __future__ import annotations
import os, re, sys, time, hashlib, pathlib

CDP = os.environ.get("AO_GPT_PRO_CDP_URL", "http://127.0.0.1:9222")
# Resilience cache: the actuate->bridge chain can be SIGTERM'd before a slow Pro run
# (10-20min) completes, even though the standalone browser capture survives. We cache the
# genuine captured verdict for the exact package+gate+nonce tuple so a re-actuate of the
# same authorized gate records the receipt instantly, without allowing cross-gate replay.
CACHE_DIR = os.environ.get("AO_GPT_PRO_VERDICT_CACHE_DIR", "/tmp")
CACHE_MAX_AGE_S = int(os.environ.get("AO_GPT_PRO_VERDICT_CACHE_MAX_AGE_S", "10800"))
MAX_S_CAP = int(os.environ.get("AO_GPT_PRO_BROWSER_MAX_S", "2700"))
PRO_LABELS = tuple(
    s.strip() for s in os.environ.get("AO_GPT_PRO_MODEL_LABELS", "进阶专业,ChatGPT Pro,Pro,GPT-5.5 Pro").split(",") if s.strip()
)
LOGGED_OUT = ("Log in", "Sign up for free", "Welcome back")
# A capture is only "complete" once the model has emitted a parseable final verdict line
# (the bridge classifier requires exactly this). Pro models often emit a short preamble
# ("I'll review and give a verdict") and pause mid-stream — that must NOT be mistaken for
# completion, and must NOT be cached. Mirror the bridge's _VERDICT_RE enum.
VERDICT_RE = re.compile(
    r'(?:["“”]?\bverdict\b["“”]?|结论)\s*[:：]\s*["“”]?'
    r"(blocker|pass_with_advisory|pass_with_nits|advisory|pass)\b",
    re.IGNORECASE,
)
VERDICT_INSTR = (
    "\n\n---\nThe complete review package is attached as a .zip. "
    "Unzip it, review the included source and evidence, and treat the package "
    "contents as the review scope. End your response with a final line EXACTLY "
    "in this form: VERDICT: <one of: pass|pass_with_nits|advisory|blocker>"
)


def _log(m):  # to stderr; bridge captures it on failure
    print(f"[browser-actuator] {m}", file=sys.stderr, flush=True)


def _find_model_button(page):
    btn = page.query_selector("[data-testid='model-switcher-dropdown-button']")
    if btn:
        return btn
    for b in page.query_selector_all("button"):
        t = (b.inner_text() or "").strip()
        if any(lbl in t for lbl in PRO_LABELS) or t.startswith("ChatGPT"):
            return b
    return None


def _ensure_pro_model(page) -> bool:
    btn = _find_model_button(page)
    label = (btn.inner_text().strip() if btn else "")
    if any(lbl in label for lbl in PRO_LABELS):
        return True
    # try to open the menu and pick a Pro option
    if btn:
        try:
            btn.click(); time.sleep(1.5)
            for it in page.query_selector_all("[role='menuitem'],[role='option']"):
                t = (it.inner_text() or "").strip()
                if any(lbl in t for lbl in PRO_LABELS):
                    it.click(); time.sleep(1)
                    page.keyboard.press("Escape")
                    btn2 = _find_model_button(page)
                    return any(lbl in (btn2.inner_text() if btn2 else "") for lbl in PRO_LABELS)
            page.keyboard.press("Escape")
        except Exception as e:
            _log(f"model select error: {e}")
    return False


def _cache_path(pkg: str, proposal_id: str, nonce: str) -> pathlib.Path | None:
    if not proposal_id or not nonce:
        return None
    package_sha = hashlib.sha256(pathlib.Path(pkg).read_bytes()).hexdigest()
    gate_sha = hashlib.sha256(f"{package_sha}:{proposal_id}:{nonce}".encode("utf-8")).hexdigest()
    return pathlib.Path(CACHE_DIR) / f"cg_pro_verdict_{gate_sha}.txt"


def run(pkg: str, prompt_path: str, raw_out: str, timeout_s: int) -> int:
    timeout = min(int(timeout_s or MAX_S_CAP), MAX_S_CAP)
    proposal_id = os.environ.get("AO_GPT_PRO_PROPOSAL_ID", "")
    nonce = os.environ.get("AO_GPT_PRO_SUBMISSION_NONCE", "")
    # Fast path: reuse a recent genuine capture for THIS exact gate+nonce (survives bridge kill).
    cache = _cache_path(pkg, proposal_id, nonce)
    if cache and cache.exists() and (time.time() - cache.stat().st_mtime) < CACHE_MAX_AGE_S:
        cached = cache.read_text(encoding="utf-8", errors="replace")
        if cached and VERDICT_RE.search(cached):
            pathlib.Path(raw_out).write_text(cached, encoding="utf-8")
            _log(f"used cached verdict for authorized gate ({len(cached)} chars) -> {raw_out}")
            return 0
    prompt = pathlib.Path(prompt_path).read_text(encoding="utf-8") + VERDICT_INSTR
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.connect_over_cdp(CDP)
        except Exception as e:
            _log(f"cdp connect failed ({CDP}): {e}"); return 3
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.new_page()
        try:
            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            _log(f"goto error: {e}")
        page.bring_to_front(); time.sleep(5)
        body = page.inner_text("body")[:1500] if page.query_selector("body") else ""
        if any(m in body for m in LOGGED_OUT) and not page.query_selector("[data-message-author-role]"):
            _log("not logged in"); return 3
        if not _ensure_pro_model(page):
            _log("GPT Pro model not selectable — refusing to run on a non-Pro model"); return 3
        fi = page.query_selector("input[type='file']")
        if not fi:
            _log("no file input"); return 3
        fi.set_input_files(pkg); time.sleep(8)
        comp = page.query_selector("#prompt-textarea") or page.query_selector("div[contenteditable='true']")
        if not comp:
            _log("no composer"); return 3
        comp.click(); time.sleep(0.5); page.keyboard.insert_text(prompt); time.sleep(1)
        sb = page.query_selector("[data-testid='send-button']")
        if not sb:
            for bt in page.query_selector_all("button"):
                al = (bt.get_attribute("aria-label") or "")
                if any(k in al for k in ["发送", "Send"]) and bt.is_enabled():
                    sb = bt; break
        if not sb:
            _log("no send button"); return 3
        sb.click(); time.sleep(6)
        if not page.query_selector("[data-message-author-role='user']"):
            _log("send did not post user message"); return 3
        t0 = time.time(); last = ""; stable = 0
        while time.time() - t0 < timeout:
            gen = bool(
                page.query_selector("[data-testid='stop-button']")
                or page.query_selector("button[aria-label*='停止']")
                or page.query_selector("button[aria-label*='Stop']")
            )
            msgs = page.query_selector_all("[data-message-author-role='assistant']")
            txt = msgs[-1].inner_text() if msgs else ""
            # Complete only when NOT generating AND a parseable final verdict line is present
            # AND it has been stable for 2 consecutive polls (Pro pauses mid-stream after a
            # preamble; the preamble has no VERDICT line, so it can never end the loop here).
            if not gen and len(txt) > 40 and VERDICT_RE.search(txt):
                if txt == last:
                    stable += 1
                else:
                    stable = 0
                last = txt
                if stable >= 2:
                    break
            else:
                last = txt
                stable = 0
            time.sleep(30)
        pathlib.Path(raw_out).write_text(last or "", encoding="utf-8")
        if not last or not VERDICT_RE.search(last):
            _log(f"no parseable verdict captured (len={len(last)})"); return 3
        try:
            if cache is not None:
                cache.write_text(last, encoding="utf-8")
        except Exception as e:
            _log(f"cache write failed: {e}")
        _log(f"captured verdict transcript (len={len(last)}) -> {raw_out}")
        return 0


def main(argv) -> int:
    if len(argv) < 5 or argv[0] != "run":
        _log("usage: run <package_path> <prompt_path> <raw_out> <timeout_s>"); return 2
    return run(argv[1], argv[2], argv[3], int(argv[4] or MAX_S_CAP))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
