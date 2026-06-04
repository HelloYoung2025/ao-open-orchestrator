# 兼容边界

兼容层当前只支持一个 state schema 和一个 contract version：

- state schema：`schema_version = 1`；
- contract version：`version = 1`。

如果未来 runtime 改了 transport、环境变量、state shape 或 action vocabulary，正确结果是明确的 compatibility failure，而不是让 gate 悄悄失效。

当前 caller identity 环境变量：

- `AO_CALLER_TYPE`；
- `AO_SESSION_ID`。

profile 可以把未来 runtime 的变量显式 shim 到这些名字，但 shim 必须是显式代码，并且必须有测试覆盖。
