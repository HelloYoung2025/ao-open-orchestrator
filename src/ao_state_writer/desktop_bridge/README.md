# desktop_bridge — GPT Pro 外审驱动（major-chapter external review）

`gpt_pro_desktop_bridge.py` 调用这里的脚本来驱动 **真正的 GPT Pro 模型** 完成 major-chapter
外审（提交审核包 + 收取 Pro verdict）。bridge 通过模块相对路径解析脚本（不依赖 `/tmp`），
`AO_GPT_PRO_DESKTOP_BRIDGE_SCRIPT` 可覆盖；`DEFAULT_BRIDGE_SCRIPT` 现指向 **browser actuator**。

外审三硬约束（Owner 决定 2026-06-03，缺一不可）：**非 GUI 屏幕自动化 / 非开发者 API /
必须真 GPT Pro 模型**。唯一同时满足三者的载体 = 程序化(CDP)驱动已登录的 chatgpt.com 产品。

---

## 主驱动 — chatgpt_browser_review.py / .sh（PROVEN 2026-06-03）

通过 **Chrome DevTools Protocol** 程序化驱动**已登录的真实 chatgpt.com**，选中 GPT Pro
模型（中文 UI 标签「进阶专业」），附上审核包、提交、轮询到完成、抓取 transcript。
**无 osascript、无像素点击、无剪贴板、无窗口焦点竞争** —— 规避了所有击垮 GUI 路线的失败点。

接口（被 bridge 调用）：`chatgpt_browser_review.sh run <package.zip> <prompt.md> <raw_out> <timeout_s>`
→ 把含 `VERDICT:<enum>` 的完整 transcript 写入 `<raw_out>` 并 exit 0；bridge 负责算 sha/nonce、
分类 verdict、cli 负责按契约设 `caller_type=gpt_pro_review_actuator` 记 receipt（**orchestrator
全程不自设 caller type**）。

### 前置条件（运行 actuator 前必须就绪）
1. **已登录的 Chrome，且开了 CDP 端口**。独立实例、与日常 Chrome 并存：
   ```
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 --user-data-dir=/tmp/cgauto \
     --no-first-run --no-default-browser-check --new-window https://chatgpt.com/
   ```
   然后**在该窗口里手动登录 ChatGPT Pro 账号一次**（复制加密 cookie 会被 macOS keychain 挡住，
   手动登录最干净；会话长期有效）。
2. **playwright**：`AO_GPT_PRO_BROWSER_PYTHON` 指向装了 playwright 的 python（默认 `/tmp/cgpw/bin/python`）。

### 关键环境变量
- `AO_GPT_PRO_CDP_URL`（默认 `http://127.0.0.1:9222`）
- `AO_GPT_PRO_BROWSER_PYTHON`（默认 `/tmp/cgpw/bin/python`）
- `AO_GPT_PRO_BROWSER_MAX_S`（完成轮询上限，默认 2700s）
- `AO_GPT_PRO_VERDICT_CACHE_DIR` / `AO_GPT_PRO_VERDICT_CACHE_MAX_AGE_S`（按 package-sha 的 verdict 缓存）

### 完成检测契约（关键正确性）
**只有出现可解析的 `VERDICT:(blocker|pass_with_nits|advisory|pass)` 行、且 NOT generating、
且连续 2 次稳定，才算完成。** Pro 常先发一句开场白（"我会解压审查…给出 verdict"）再长时间推理，
中途短暂停顿会让 stop 键消失 —— 若只看"停了"就截断,会把**开场白误当 verdict**（实测踩过，
196 字节开场白被缓存，bridge 报 `unrecognized_review_verdict`）。VERDICT 行硬门槛根除该误触发。

### 缓存韧性（为什么需要）
`gpt-pro-actuate → bridge → 脚本` 这条嵌套链曾在 ~8.7 分钟被外部 SIGTERM 杀掉（杀因未定位），
而 Pro 一轮约 14 分钟。对策：脚本按 `sha256(package)` 缓存**有效** verdict；即便 bridge 被杀，
**孤儿浏览器进程仍跑满并写出缓存**（独立浏览器捕获不受该杀窗影响），随后**重跑 `gpt-pro-actuate`
秒级命中缓存 → cli 正规记 receipt**。缓存只接受含可解析 VERDICT 行的内容（开场白不缓存）。

---

## 已退役 — chatgpt_desktop_review.sh（GUI，parked 为 `.PAUSED-mousegrab`）

原 GUI 路线（osascript/cliclick 像素点击 + 剪贴板 Cmd+A/Cmd+C + 窗口焦点）**本质脆弱、已弃用**：
`sdef`/Automation 工具链一坏即废、几何点击会抓错窗口、嵌套链被杀、verdict 落在加密会话取不出。
本变更不把 GUI helper 作为 canonical 自动路径入库；若本机仍有 `.PAUSED-mousegrab` 实验脚本，它只是
本地历史证据，不参与 `gpt-pro-actuate`。

## 运行时瞬态
- `reports/gpt-pro-raw/<proposal>-<nonce>.txt` — 本轮抓取的原始 transcript（bridge 读它分类 verdict）。
- `/tmp/cg_pro_verdict_<package_sha256>.txt` — 按包 sha 的 verdict 缓存（瞬态，不入库）。
