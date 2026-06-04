# 无人值守 Loop 原则

“无人值守”指的是自动推进到下一个安全边界，而不是绕过未知业务逻辑或 owner-only gate。

每次 tick 必须落入三类之一：

1. 继续执行受支持的 action；
2. 进入 typed repair 或 convergence；
3. 停在明确的 owner-only gate。

静默停住是设计失败。一个 fail-closed 状态只有在明确写出 blocked action、forbidden actions、allowed repair actions、repair obligation 和 evidence anchor 时才是有效状态。
