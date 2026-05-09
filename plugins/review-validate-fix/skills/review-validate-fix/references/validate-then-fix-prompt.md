# Moved: Validate/Fix Prompt

Validate/fix 子代理 prompt 的事实源已迁移到 `prompts/validate-fix.md`。

正常 RVF 运行应由脚本读取 `prompts/validate-fix.md` 并注入 self-contained issue package；主会话不要把本兼容文件当作 validate/fix prompt 使用。
