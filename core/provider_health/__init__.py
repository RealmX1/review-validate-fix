"""``core.provider_health`` —— Provider 健康探测的 host-中性内核包（S10f）。

承载「跑一条 provider 健康/认证探测命令、判定 ok/failed、整形输出」的可复用中性机制，
完全 host-agnostic（不命名任何 host、不 import adapter）。codex 专属的二进制发现 /
登录 / auth-marker 判定住 ``adapters/codex/codex_provider_login.py``，经 requirement 的
``command`` 元组与 ``output_failure_predicate`` 注入。

消费方按全 dotted 路径 ``from core.provider_health.provider_health_guard import ...``，
本包不做 re-export（保持单一来源、避免符号双暴露）。
"""
