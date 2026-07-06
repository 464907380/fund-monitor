#!/usr/bin/env python3
"""
基金监控管理 — 增减监控基金

用法：
    python fund_manage.py list                   # 查看当前监控列表
    python fund_manage.py add 001438             # 添加基金（支持多个）
    python fund_manage.py add 001438 180031
    python fund_manage.py remove 001438          # 移除基金
    python fund_manage.py remove 001438 180031

数据存储在 fund_list.json，所有脚本共享。
"""
import json
import os
import sys
import re

_FUND_LIST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "fund_list.json")


def _load() -> list[dict]:
    if os.path.exists(_FUND_LIST_PATH):
        try:
            with open(_FUND_LIST_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(data: list[dict]) -> None:
    with open(_FUND_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def cmd_list() -> None:
    funds = _load()
    if not funds:
        print("📭 当前没有监控任何基金")
        return
    print(f"📋 监控基金列表（共 {len(funds)} 只）")
    print("-" * 40)
    for f in funds:
        code = f["code"]
        name = f.get("name", "")
        if name:
            print(f"  {code}  {name}")
        else:
            print(f"  {code}")


def cmd_add(codes: list[str]) -> None:
    funds = _load()
    existing = {f["code"] for f in funds}
    added = 0
    for code in codes:
        if not re.fullmatch(r"\d{6}", code):
            print(f"  ⚠️  {code} 格式错误，跳过（需6位数字）")
            continue
        if code in existing:
            print(f"  ⏭️  {code} 已在监控列表中")
            continue
        funds.append({"code": code})
        existing.add(code)
        added += 1
        print(f"  ✅ {code} 已加入监控")
    if added:
        _save(funds)
        print(f"\n✔ 共添加 {added} 只，当前 {len(funds)} 只")


def cmd_remove(codes: list[str]) -> None:
    funds = _load()
    before = len(funds)
    removed_codes = set(codes)
    funds = [f for f in funds if f["code"] not in removed_codes]
    removed = before - len(funds)
    not_found = [c for c in codes if c not in {f["code"] for f in _load()}]
    for code in codes:
        if code in {f["code"] for f in _load()}:
            print(f"  ❌ {code} 已移除")
        elif code not in removed_codes:
            continue
    for code in not_found:
        print(f"  ⚠️  {code} 不在监控列表中")
    if removed:
        _save(funds)
        print(f"\n✔ 共移除 {removed} 只，当前 {len(funds)} 只")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return

    cmd = args[0]
    codes = args[1:]

    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        if not codes:
            print("❌ 请指定要添加的基金代码，例如：python fund_manage.py add 001438")
            return
        cmd_add(codes)
    elif cmd == "remove":
        if not codes:
            print("❌ 请指定要移除的基金代码，例如：python fund_manage.py remove 001438")
            return
        cmd_remove(codes)
    else:
        print(f"❌ 未知命令: {cmd}，支持: list, add, remove")
        print(f"\n{__doc__.strip()}")


if __name__ == "__main__":
    main()
