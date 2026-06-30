"""host-中性的跨簇共享常量内核包（去-codex 重构 S10d，R-NEW-1）。

承载被引擎 ≥2 个功能簇共同引用的模块级常量；消费方直接
``from core.rvf_shared_runtime_constants.rvf_shared_runtime_constants import ...``，
本包不做 re-export（保持单一来源、避免符号双暴露，与 core/run_ledger 同口径）。
"""
