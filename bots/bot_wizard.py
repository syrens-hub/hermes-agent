#!/usr/bin/env python3
"""bot_wizard.py — Hermes bot 人格创建向导 (Phase 2, 复制 Cumora 6 字段模式)

6 字段 → profile 目录 + profile.yaml + SOUL.md (+ 可选 mmx 头像)
设计: ~/.hermes/plans/bot-team-chat-design.md Phase 2
用法: python3 bot_wizard.py            交互问答
      python3 bot_wizard.py --avatar   额外调 mmx 生成头像 (耗 mmx 配额)

Phase 0 实证的两个坑在这里被结构性消灭:
  - api_mode 配错 → 静默 404: 向导默认写死验证过的 provider 配置, 用户不碰
  - profile 无推理配置: 向导必生成 config.yaml (沿用 legal-advisor 验证过的模板)
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

PROFILES_DIR = Path.home() / ".hermes" / "profiles"

# legal-advisor Phase 0/09-03 实证可用的推理配置 (api.kimi.com coding, anthropic_messages)
DEFAULT_CONFIG = """model:
  default: kimi-for-coding
  provider: kikimikimi-3
  context_length: 200000
providers:
  kikimikimi-3:
    base_url: https://api.kimi.com/coding/
    api_key: ${env:KIMI_API_KEY}
    api_mode: anthropic_messages
    model: kimi-for-coding
"""

SOUL_TEMPLATE = """# {name} ({pid} profile)
# 由 bot_wizard.py 生成于 {date}

## 人格

- 角色: {role}
- 风格: {style}
{persona_extra}

## 工作边界

- 在自己领域内作答, 超出边界时明确说"这不是我的域"并建议找谁
- 团队线程中被 @ 时才发言; 没把握时明说, 不编造

## 工具调用约束

- 遵守 Hermes 通用工具纪律 (引用给来源, 不确定就标不确定)
"""


def ask(field: str, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar", action="store_true", help="调 mmx 生成头像 (耗配额)")
    args = ap.parse_args()

    print("=== Hermes bot 人格向导 (6 字段, 复制 Cumora 模式) ===\n")
    name = ask("名称", "1/6 名称 (bot 显示名)", "")
    role = ask("角色", "2/6 角色 (一句话职责)", "")
    style = ask("风格", "3/6 风格 (第二人称, 如'你写...')", "严谨、直接、给具体数字")
    bio = ask("简介", "4/6 简介 (可选, 回车跳过)", "")
    model = ask("主力模型", "5/6 主力模型", "kimi-for-coding")
    fast = ask("轻量模型", "6/6 轻量模型 (分诊预留, v1 未接线)", "kimi-for-coding")

    pid = slugify(name)
    if not pid:
        pid = slugify(ask("ID", "名称为纯中文, 给个 ASCII ID (如 negotiator)", ""))
    if not pid:
        raise SystemExit("ID 不能为空 (需要 ASCII 字母/数字, @mention 也只认 ASCII)")
    if (PROFILES_DIR / pid).exists():
        raise SystemExit(f"profile 已存在: {pid} (换名或先删)")
    for label, val in [("名称", name), ("角色", role)]:
        if not val:
            raise SystemExit(f"{label}不能为空")

    pdir = PROFILES_DIR / pid
    pdir.mkdir(parents=True)

    (pdir / "profile.yaml").write_text(
        f'description: "{bio or role} (bot-wizard 2026 生成)"\n'
        f'description_auto: false\n',
        encoding="utf-8",
    )
    persona_extra = f"- 简介: {bio}\n" if bio else ""
    (pdir / "SOUL.md").write_text(
        SOUL_TEMPLATE.format(
            name=name, pid=pid, date=time.strftime("%Y-%m-%d"),
            role=role, style=style, persona_extra=persona_extra,
        ),
        encoding="utf-8",
    )
    (pdir / "config.yaml").write_text(DEFAULT_CONFIG, encoding="utf-8")
    (pdir / "skills").mkdir()

    print(f"\n✅ profile '{pid}' 已创建: {pdir}")
    print(f"   SOUL.md / profile.yaml / config.yaml / skills/")

    if args.avatar:
        prompt = f"flat minimal avatar icon for an AI assistant named {name}, role: {role}, simple geometric, no text"
        print(f"\n🎨 生成头像: {prompt[:60]}...")
        r = subprocess.run(
            ["mmx", "img", prompt, "--output", str(pdir / "avatar.png")],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0:
            print("✅ 头像已生成")
        else:
            print(f"⚠️ 头像生成失败 (不影响 profile): {r.stderr[-200:]}")
    else:
        print("   (头像: 加 --avatar 可调 mmx 生成)")

    print(f"\n试用: hermes -p {pid} -z \"你好, 介绍你自己\"")
    print(f"入群: python3 ~/dev/bot-thread/bot_thread.py create <线程> --members default,{pid}")


if __name__ == "__main__":
    main()
