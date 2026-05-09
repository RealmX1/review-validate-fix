# Moved: Reviewer Prompt

Reviewer 子代理 prompt 的事实源已迁移到 `prompts/reviewer.md`。

正常 RVF 运行应由脚本读取 `prompts/reviewer.md` 并注入 self-contained 上下文；主会话不要把本兼容文件当作 reviewer prompt 使用。
