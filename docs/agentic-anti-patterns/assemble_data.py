#!/usr/bin/env python3
"""把 _cards/*.json 汇编为 antipatterns-data.json（含 groups 元数据，按 order 排序）。

这是「内容真相源」的组装步骤：每张卡片由 workflow 的 verify agent 各自写到
_cards/<id>.json，本脚本只做确定性聚合 + 排序 + 完整性校验，零模型参与。
"""
import json
import glob
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.path.join(HERE, "_cards")
OUT = os.path.join(HERE, "antipatterns-data.json")

# 组元数据：必须与 workflow 脚本中的 GROUPS 一致
GROUPS = [
    {"id": "g1", "title_cn": "架构与分解", "title_en": "Architecture & Decomposition"},
    {"id": "g2", "title_cn": "Prompt 与上下文", "title_en": "Prompts & Context"},
    {"id": "g3", "title_cn": "工具", "title_en": "Tools"},
    {"id": "g4", "title_cn": "控制流与可靠性", "title_en": "Control Flow & Reliability"},
    {"id": "g5", "title_cn": "自治与监督", "title_en": "Autonomy & Oversight"},
    {"id": "g6", "title_cn": "验证与评估", "title_en": "Verification & Evaluation"},
    {"id": "g7", "title_cn": "多智能体协调", "title_en": "Multi-Agent Coordination (MAST)"},
    {"id": "g8", "title_cn": "安全（附录）", "title_en": "Security (Appendix)"},
]

REQUIRED = [
    "id", "name_cn", "name_en", "group_id", "order",
    "definition", "why_harmful", "symptoms", "mitigation",
    "sources", "evidence_strength",
]


def main():
    files = sorted(glob.glob(os.path.join(CARDS_DIR, "*.json")))
    cards = []
    problems = []
    seen = set()
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                c = json.load(fh)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{os.path.basename(f)}: JSON 解析失败 {e}")
            continue
        miss = [k for k in REQUIRED if k not in c]
        if miss:
            problems.append(f"{os.path.basename(f)}: 缺字段 {miss}")
        if c.get("id") in seen:
            problems.append(f"{os.path.basename(f)}: id 重复 {c.get('id')}")
        seen.add(c.get("id"))
        cards.append(c)

    cards.sort(key=lambda c: c.get("order", 9999))
    data = {"groups": GROUPS, "cards": cards}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    print(f"汇编 {len(cards)} 张卡片 → {OUT}")
    cnt = Counter(c.get("group_id") for c in cards)
    for g in GROUPS:
        print(f"  {g['id']} {g['title_cn']}: {cnt.get(g['id'], 0)}")
    unknown = [gid for gid in cnt if gid not in {g['id'] for g in GROUPS}]
    if unknown:
        problems.append(f"出现未知 group_id: {unknown}")
    if problems:
        print("⚠️ 问题：")
        for p in problems:
            print("  - " + p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
