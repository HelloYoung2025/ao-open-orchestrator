#!/bin/bash
# Headless-browser (CDP) GPT Pro review actuator wrapper.
# Invoked by gpt_pro_desktop_bridge.py as: chatgpt_browser_review.sh run <pkg> <prompt> <raw_out> <timeout>
# Runs the Playwright-based driver under a python that has playwright installed.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PYBIN="${AO_GPT_PRO_BROWSER_PYTHON:-/tmp/cgpw/bin/python}"
if [ ! -x "$PYBIN" ]; then
  # fall back to any python3 with playwright on PATH
  PYBIN="$(command -v python3)"
fi
exec "$PYBIN" "$DIR/chatgpt_browser_review.py" "$@"
