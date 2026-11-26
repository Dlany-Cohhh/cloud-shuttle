#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api_audit.py · 前端 / 后端 / docs/API.md 三方接口对账（阶段 1 与阶段 4 共用）

用法（在仓库任意位置）：
    python scripts/api_audit.py            # 输出差异表
    python scripts/api_audit.py --all      # 额外打印三个集合全量清单

规则：
    F = static/app.js 里 api("...") 调用的 action 字面量（含 this.api / await api）
    B = download-console.py 的 api() 分发分支键（含 if action == "x" 与 if action in (...)）
    D = docs/API.md 声明的 action（「全部 action」清单 + 常用操作表）

    F 有 B 无 → 前端孤儿调用；B 有 F 无 → 后端死接口；
    B 有 D 无 → 未文档化；D 有 B 无 → 文档承诺未实现（最严重）。
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "download-console.py")
JS = os.path.join(ROOT, "static", "app.js")
DOC = os.path.join(ROOT, "docs", "API.md")


def backend_actions():
    """返回 {action: 行号}。

    v4.21 起优先读 @action("x") 注册表（HANDLERS）；为兼容历史基线，
    无注册表时回退解析 api() 内 if action == "x" 分支。
    """
    src = open(PY, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}

    # ── 路径 1：@action("x") / @action("a", "b", ...) 装饰器注册表 ──
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                    and dec.func.id == "action"):
                for a in dec.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        out[a.value] = node.lineno
    if out:
        return out

    # ── 路径 2：历史形态 api() 内 if action == "x" 分支 ──
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "api":
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                t = sub.test
                # if action == "x":
                if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                        and t.left.id == "action" and len(t.ops) == 1
                        and isinstance(t.ops[0], ast.Eq) and len(t.comparators) == 1
                        and isinstance(t.comparators[0], ast.Constant)
                        and isinstance(t.comparators[0].value, str)):
                    out[t.comparators[0].value] = sub.lineno
                # if action in ("a", "b", ...):
                if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                        and t.left.id == "action" and len(t.ops) == 1
                        and isinstance(t.ops[0], ast.In) and len(t.comparators) == 1
                        and isinstance(t.comparators[0], (ast.Tuple, ast.List, ast.Set))):
                    for elt in t.comparators[0].elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            out[elt.value] = sub.lineno
    return out


def frontend_actions():
    """返回 (dict action->首个行号, list 非字面量调用的行号清单)。"""
    lines = open(JS, encoding="utf-8").read().splitlines()
    found, nonlit = {}, []
    for i, line in enumerate(lines, 1):
        # 定义行（helper 本体）跳过
        if re.search(r"""(async\s+api\s*\(|function\s+api\s*\(|const\s+api\s*=)""", line):
            continue
        # 逐个匹配：先找字面量形式（this.api("x", ...) / api("x", ...)）
        for m in re.finditer(r"""(?<![\w$])api\s*\(\s*(['"`])([^'"`\n]+)\1""", line):
            name = m.group(2)
            # 模板字符串里带 ${} 的无法静态判定
            if "${" in name:
                nonlit.append((i, line.strip()[:80]))
            else:
                found.setdefault(name, i)
        # 找到 api( 但首个参数不是字面量
        for m in re.finditer(r"""(?<![\w$])api\s*\(""", line):
            window = line[m.end():m.end() + 120]
            if not re.match(r"""^\s*(['"`])""", window):
                nonlit.append((i, line.strip()[:80]))
    # 去重：同一行既命中字面量又误报非字面量的，移除
    nonlit = list(dict.fromkeys(nonlit))
    nonlit = [(i, t) for (i, t) in nonlit
              if not re.search(r"""(?<![\w$])api\s*\(\s*['"`]""", lines[i - 1])]
    return found, nonlit


def doc_actions():
    """返回 {action: 位置说明}。"""
    out = {}
    text = open(DOC, encoding="utf-8").read()
    # 「全部 action」清单项：- `xxx`
    for m in re.finditer(r"^\s*-\s*`([a-z0-9_]+)`", text, re.M):
        out.setdefault(m.group(1), "全部action清单")
    # 常用操作表：| `a` / `b` | 说明 |（单元格前后允许空白）
    for m in re.finditer(r"^\|\s*(`[^|]+`)\s*\|", text, re.M):
        cell = m.group(1)
        for t in re.finditer(r"`([a-z0-9_]+)`", cell):
            out.setdefault(t.group(1), "常用操作表")
    return out


def main():
    show_all = "--all" in sys.argv
    B = backend_actions()
    F, nonlit = frontend_actions()
    D = doc_actions()

    print("集合规模：前端 F=%d  后端 B=%d  文档 D=%d" % (len(F), len(B), len(D)))
    print()

    rows = []
    for name in sorted(set(F) - set(B)):
        rows.append(("前端孤儿调用", name, F[name], "-", D.get(name, "-")))
    for name in sorted(set(B) - set(F)):
        rows.append(("后端死接口", name, "-", B[name], D.get(name, "-")))
    for name in sorted(set(B) - set(D)):
        rows.append(("未文档化", name, F.get(name, "-"), B[name], "-"))
    for name in sorted(set(D) - set(B)):
        rows.append(("承诺未实现", name, F.get(name, "-"), "-", D[name]))

    if rows:
        print("| 类型 | action | 前端行 | 后端行 | 文档 |")
        print("|---|---|---|---|---|")
        for r in rows:
            print("| %s | %s | %s | %s | %s |" % r)
    else:
        print("✅ 三方完全对齐，无差异。")

    if nonlit:
        print("\n## 无法静态判定（非字面量 action）")
        for i, t in nonlit:
            print("- app.js:%d  %s" % (i, t))

    if show_all:
        for tag, s in (("F", F), ("B", B), ("D", D)):
            print("\n== 集合 %s（%d 个）==" % (tag, len(s)))
            print(sorted(s))


if __name__ == "__main__":
    main()
