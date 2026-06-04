# 贡献指南

[English](CONTRIBUTING.en.md)

请保持改动小、可测试、可回滚。

提交 PR 前请运行：

```bash
python -m pytest -q
python scripts/public_safety_scan.py
git diff --check
```

贡献规则：

- 不要增加第二个 canonical state writer。
- 不要把 scheduler、daemon 或 queue 引入 core state logic。
- 本地 transport 放在 profile 或 adapter 层。
- 新增 action token、state schema 变化或外部 receipt 规则时，必须补 regression test。
- public/private hygiene 是测试面的一部分，不是发布后再补的附属检查。
