#!/usr/bin/env python3
"""presentation.html 的 JSON 数据岛 → anti-patterns.annotated.md（可批注；重跑保留 annot 区块内容）。

设计要点：
- 唯一真相源是 presentation.html 内的数据岛；本脚本只读它、不读 data.json，符合「md 从 html 自动生成」的约定。
- 每张卡片尾部有一个以反模式 id 为锚的 annot 区块；重跑本脚本会**保留**你在区块内写的批注（按 id merge，不整文件覆盖）。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "presentation.html")
OUT = os.path.join(HERE, "anti-patterns.annotated.md")

DEFAULT_ANNOT = (
    "> **我的评估**：☐ 同意　☐ 存疑　☐ 补充\n"
    "> **既有认知 / 反驳**：\n"
    "> **RVF 相关度预判（可选）**：\n"
)


def extract_data(html):
    m = re.search(
        r'<script type="application/json" id="antipatterns-data">(.*?)</script>',
        html, re.S,
    )
    if not m:
        raise SystemExit("未在 presentation.html 找到数据岛")
    return json.loads(m.group(1))


def load_existing_annots(path):
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    out = {}
    for m in re.finditer(
        r"<!-- annot:start:(.*?) -->\n(.*?)<!-- annot:end:\1 -->", txt, re.S
    ):
        out[m.group(1)] = m.group(2)
    return out


def main():
    if not os.path.exists(HTML):
        print(f"缺少 {HTML}；请先运行 build_html.py", file=sys.stderr)
        return 1
    data = extract_data(open(HTML, encoding="utf-8").read())
    groups = data["groups"]
    cards = data["cards"]
    annots = load_existing_annots(OUT)
    gmap = {g["id"]: g for g in groups}

    L = []
    L.append("# AI Agentic 工作流设计反模式 — 可批注评估清单\n")
    L.append(
        "> 本文件由 `generate_markdown.py` 从 `presentation.html` 的数据岛**自动生成**。\n"
        "> **卡片正文请勿手改**（要改内容：改 `antipatterns-data.json` → 重跑 `build_html.py` → 重跑本脚本）。\n"
        "> 只在每张卡片的 `annot` 区块（HTML 注释锚之间）批注；重跑本脚本会**保留**你写在区块内的内容。\n"
    )

    L.append("\n## 速览与判定表\n")
    L.append("| # | 反模式 | 组 | 证据强度 | 我的判定（同意/存疑/补充） |")
    L.append("|---|---|---|---|---|")
    for c in sorted(cards, key=lambda x: x["order"]):
        gt = gmap.get(c["group_id"], {}).get("title_cn", c["group_id"])
        L.append(
            f"| {c['order']} | {c['name_cn']} <sub>{c['name_en']}</sub> "
            f"| {gt} | {c['evidence_strength']} | |"
        )

    for g in groups:
        gc = [c for c in cards if c["group_id"] == g["id"]]
        if not gc:
            continue
        L.append(f"\n---\n\n## {g['title_cn']} · {g['title_en']}\n")
        for c in sorted(gc, key=lambda x: x["order"]):
            L.append(
                f"### {c['order']}. {c['name_cn']} — {c['name_en']}　·　`{c['evidence_strength']}`\n"
            )
            L.append(f"**定义**：{c['definition']}\n")
            L.append(f"**为何有害**：{c['why_harmful']}\n")
            L.append("**症状（如何识别）**：")
            for s in c["symptoms"]:
                L.append(f"- {s}")
            L.append(f"\n**缓解（正确做法）**：{c['mitigation']}\n")
            L.append("**来源**：")
            for s in c["sources"]:
                L.append(f"- [{s['label']}]({s['url']})")
            inner = annots.get(c["id"], DEFAULT_ANNOT)
            if not inner.endswith("\n"):
                inner += "\n"
            L.append(f"\n<!-- annot:start:{c['id']} -->\n{inner}<!-- annot:end:{c['id']} -->\n")

    open(OUT, "w", encoding="utf-8").write("\n".join(L).rstrip("\n") + "\n")
    print(f"生成 {OUT}（{len(cards)} 卡片；保留既有批注 {len(annots)} 处）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
