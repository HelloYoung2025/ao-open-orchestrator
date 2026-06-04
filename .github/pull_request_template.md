# PR 检查清单 / Pull Request Checklist

## 变更说明

请说明这次改动解决什么问题，以及是否改变了 state schema、contract version、action vocabulary、receipt contract 或 adapter 行为。

English: describe what this change fixes and whether it changes state schema, contract version, action vocabulary, receipt contract, or adapter behavior.

## 安全边界

- [ ] 没有增加第二个 canonical state writer。
- [ ] 没有把 scheduler、daemon 或 queue 引入 core state logic。
- [ ] 没有提交 raw `.omx` state、本地 transcript、账号绑定日志、机器路径或 secret。
- [ ] 如果新增外部 adapter，已说明它属于 profile/adapter 层，而不是 trust root。
- [ ] 如果新增 action token、schema 或 receipt rule，已补 regression test。

## 验证

```bash
python -m pytest -q
python scripts/public_safety_scan.py
git diff --check
```

请贴出验证结果。  
English: paste the verification output.
