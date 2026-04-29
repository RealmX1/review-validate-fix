# Security Subset

RVF 把安全问题视为一等 bug，但 reviewer 输出仍必须是可定位的 issue，而不是完整 audit report。

## 吸收标准

- Three-tier boundary system：Always Do、Ask First、Never Do 的安全边界纪律。
- 边界输入校验。
- 参数化查询。
- 输出编码 / XSS 防护。
- auth / authorization。
- secrets management。
- session / cookie / CORS / security headers。
- file upload / webhook / external integration 风险。
- dependency audit triage。

## RVF 报告门槛

可以报告：

- 未校验外部输入进入数据库、shell、HTML、路径或业务权限判断。
- auth 或 authorization 缺失。
- secrets 出现在代码、日志、配置或 handoff。
- CORS、cookie、session 或 security headers 误配置造成当前 scope 内风险。
- 可达 high / critical dependency risk。

不要报告：

- 与当前 scope 无关的泛化 hardening plan。
- 不能定位到当前代码的安全建议。
- 需要产品或安全策略决策但无法独立验证的问题；这类应 `ELEVATE`。
