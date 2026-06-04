# 兼容边界

当前版本只支持一个 state schema 和一个 contract version：

- state schema：`schema_version = 1`；
- contract version：`version = 1`。

未来版本可以增加显式 migration 或 shim。但在这些支持被实现并测试之前，如果 runtime 改了 transport、环境变量、state shape 或 action vocabulary，正确结果是明确的 compatibility failure，而不是让 gate 悄悄失效。

compat/preflight 当前覆盖 root identity、contract version/action vocabulary、state schema 和 unsupported live obligations。caller identity 是在 receipt submission 和 authorization 路径上单独校验的，不是 preflight 本身完成的。

当前 caller identity 环境变量：

- `AO_CALLER_TYPE`；
- `AO_SESSION_ID`。

profile 可以把未来 runtime 的变量显式 shim 到这些名字，但 shim 必须是显式代码，并且必须有测试覆盖。

这些环境变量只是本地绑定提示，不是通用认证系统。不要在没有额外认证/授权层的情况下，把 `ao-state-writer` 或 adapter 命令暴露成网络服务。
