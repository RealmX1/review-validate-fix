# Performance Subset

RVF 的性能标准遵循 Measure before optimizing。没有需求、证据、明确 anti-pattern 或可定位 regression 时，不输出性能 issue。

## 吸收标准

- before / after measurement。
- N+1 query。
- unbounded data fetching。
- blocking main thread。
- expensive recomputation。
- excessive re-render。
- large bundle / asset regression。
- layout shift / Core Web Vitals 风险。
- performance budget。

## RVF 报告门槛

可以报告：

- 当前改动引入 N+1。
- 当前改动新增无界查询、无分页列表或无上限读取。
- 当前改动引入明显主线程阻塞或同步重计算。
- 当前改动明显增加 bundle、LCP asset 或 layout shift 风险。
- 已有测量或需求显示本次改动违反性能预算。

不要报告：

- 没有证据的微优化。
- “可能更快”的重写建议。
- 与当前 scope 无关的性能债。

如果需要测量才能判断，reviewer 通过 `$RVF_WRITE_REVIEW_RESULT measurement-request --out "$RVF_REVIEW_RESULT" ...` 写 request artifact，或在 validate/fix 阶段 `ELEVATE`。
