# 文档索引

本目录中文为主、英文为辅。建议先读中文版本；英文版本用于对外沟通和二次确认术语。

| 主题 | 中文 | English |
| --- | --- | --- |
| 快速开始 | [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md) | [QUICKSTART.md](QUICKSTART.md) |
| 使用说明（日常操作） | [USAGE.zh-CN.md](USAGE.zh-CN.md) | [USAGE.md](USAGE.md) |
| 多项目并行 | [MULTI_PROJECT.zh-CN.md](MULTI_PROJECT.zh-CN.md) | [MULTI_PROJECT.md](MULTI_PROJECT.md) |
| 架构 | [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 兼容边界 | [COMPATIBILITY.zh-CN.md](COMPATIBILITY.zh-CN.md) | [COMPATIBILITY.md](COMPATIBILITY.md) |
| 外部审查 receipt | [RECEIPT_CONTRACT.zh-CN.md](RECEIPT_CONTRACT.zh-CN.md) | [RECEIPT_CONTRACT.md](RECEIPT_CONTRACT.md) |
| 无人值守 loop | [UNATTENDED_LOOP.zh-CN.md](UNATTENDED_LOOP.zh-CN.md) | [UNATTENDED_LOOP.md](UNATTENDED_LOOP.md) |

## 阅读顺序

1. 先读 [README.md](../README.md)，理解这个项目是什么和不是什么。
2. 读 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)，确认 runtime、state writer、profile adapter 的分层。
3. 读 [RECEIPT_CONTRACT.zh-CN.md](RECEIPT_CONTRACT.zh-CN.md)，确认外部审查如何成为 canonical evidence。
4. 读 [COMPATIBILITY.zh-CN.md](COMPATIBILITY.zh-CN.md)，确认版本升级时为什么必须 fail-closed。
5. 读 [UNATTENDED_LOOP.zh-CN.md](UNATTENDED_LOOP.zh-CN.md)，确认无人值守推进的边界。
6. 想直接上手：读 [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md)，用 bootstrap 在新目录开展一个项目。
7. 项目建好之后的日常：读 [USAGE.zh-CN.md](USAGE.zh-CN.md)——命令手册、踢醒方法、排障表。
8. 同机多个项目：读 [MULTI_PROJECT.zh-CN.md](MULTI_PROJECT.zh-CN.md)。
