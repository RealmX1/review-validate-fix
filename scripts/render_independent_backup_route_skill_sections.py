#!/usr/bin/env python3
"""更新时生成自包含备用指引；业务运行时无需调用。"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins/review-validate-fix/skills"
SOURCE = SKILLS / "review-validate-fix/references/independent-backup-route-instructions.md"
BEGIN = "<!-- rvf-independent-backup-route:begin -->"
END = "<!-- rvf-independent-backup-route:end -->"


def render(entry):
    text = SOURCE.read_text()
    parts = {}
    for segment in text.split("<!-- entry:")[1:]:
        name, body = segment.split(" -->", 1)
        parts[name] = body.strip()
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    return f"{BEGIN}\n<!-- source-sha256: {digest} -->\n{parts['common']}\n\n{parts[entry]}\n{END}"


def update(path, entry, check):
    text = path.read_text()
    block = render(entry)
    if BEGIN in text:
        prefix, rest = text.split(BEGIN, 1)
        _, suffix = rest.split(END, 1)
        expected = prefix + block + suffix
    else:
        # 放在正常规则之前，避免 Agent 在看到例外前先按失败指令停止。
        frontmatter_end = text.index("\n---", 4) + 4 if text.startswith("---\n") else 0
        heading_end = text.index("\n", text.index("# ", frontmatter_end))
        expected = text[:heading_end] + "\n\n" + block + text[heading_end:]
    if check and expected != text:
        raise ValueError(f"备用指引未同步：{path}")
    if not check:
        path.write_text(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--base-branch-sync-skill", type=Path)
    args = parser.parse_args()
    for path, entry in [(SKILLS / "review-validate-fix/SKILL.md", "review"),
                        (SKILLS / "rvf-land/SKILL.md", "land"),
                        (ROOT / "plugins/review-validate-fix/commands/rvf-handoff-commit.md", "land")]:
        update(path, entry, args.check)
    if args.base_branch_sync_skill:
        update(args.base_branch_sync_skill, "sync", args.check)
        helper = SKILLS / "review-validate-fix/scripts/independent_backup_route.py"
        destination = args.base_branch_sync_skill.parent / "scripts/independent_backup_route.py"
        manifest = {"source_repository": "review-validate-fix", "instructions_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                    "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
        manifest_path = args.base_branch_sync_skill.parent / "references/independent-backup-route-source-manifest.json"
        if args.check:
            if helper.read_bytes() != destination.read_bytes() or json.loads(manifest_path.read_text()) != manifest:
                raise ValueError("同步 Skill 的备用路线交付副本未同步")
        else:
            destination.write_bytes(helper.read_bytes())
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
