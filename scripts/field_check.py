#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""field_check.py · 阶段 1.2（提示词 C 第 1 类）：后端返回字段 vs 前端读取字段

方法：
1. 在 static/app.js 中定位 api("action") 调用，取其后 40 行窗口，
   收集 `响应变量.field` 形式的字段读取（响应变量 = 接 api() 返回值的 const/let 名）。
2. 在 download-console.py 的 api() 对应分支中收集返回 dict 的字符串键
   （含嵌套 dict 字面量键、dict(...) 调用键、"%s" % 装配除外）。
3. 输出：前端读取但后端该分支未出现的字段（候选断链，人工复核）。

注意：本脚本是候选生成器，报告条目必须人工复核（后端字段可能来自
helper 函数或 jobs 字典的共享构造），复核规则见 EXECUTION-PLAN 阶段 1。
"""
import ast
import re
import sys

ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
PY = ROOT + "/download-console.py"
JS = ROOT + "/static/app.js"

# 只查这些高频 action（提示词 C 要求 20 个）
WATCH = ["jobs", "version", "stats", "remotes", "dirs", "settings", "savesettings",
         "accounts", "accttest", "quota", "health", "transfers", "history",
         "jobdetail", "localfiles", "localdirs", "scanpath", "shareparse",
         "autotuneinfo", "updatecheck"]


def branch_returns():
    """返回 {action: set(该分支返回 dict 的全部字符串键)}。

    对 `return helper(...)` 形态的分支，追进对应顶层函数体一并收集（一层）。
    """
    src = open(PY, encoding="utf-8").read()
    tree = ast.parse(src)
    top_fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    api_fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "api")

    def collect_keys(nodes, lo, hi):
        keys = set()
        for node in nodes:
            if not (isinstance(node, (ast.Dict, ast.keyword))):
                continue
            ln = getattr(node, "lineno", 0)
            if not (lo <= ln <= hi):
                continue
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            else:
                keys.add(node.arg)
        return keys

    def helper_range(branch_lo, branch_hi):
        """若分支体内 return helper(...)，返回 helper 名列表。"""
        helpers = set()
        for node in ast.walk(api_fn):
            if isinstance(node, ast.Return) and branch_lo <= node.lineno <= branch_hi:
                v = node.value
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                    helpers.add(v.func.id)
        return helpers

    out = {}
    branches = []
    for sub in ast.walk(api_fn):
        if not isinstance(sub, ast.If):
            continue
        t = sub.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "action" and isinstance(t.ops[0], ast.Eq)
                and isinstance(t.comparators[0], ast.Constant)):
            branches.append((t.comparators[0].value, sub.lineno, sub.end_lineno))
    all_dict_nodes = list(ast.walk(api_fn))
    for act, lo, hi in branches:
        keys = collect_keys(all_dict_nodes, lo, hi)
        for h in helper_range(lo, hi):
            fn = top_fns.get(h)
            if fn:
                keys |= collect_keys(list(ast.walk(fn)), fn.lineno, fn.end_lineno)
        out[act] = keys
    return out


def frontend_reads():
    """返回 {action: set(前端读取字段)}。"""
    lines = open(JS, encoding="utf-8").read().splitlines()
    reads = {}
    for i, line in enumerate(lines):
        m = re.search(r"""(?<![\w$])api\s*\(\s*['"]([\w]+)['"]""", line)
        if not m:
            continue
        act = m.group(1)
        if act not in WATCH:
            continue
        # 找响应变量名：const d = await this.api(...); 或 let r = ...
        v = re.search(r"(?:const|let|var)\s+([\w$]+)\s*=\s*await\s+(?:this\.)?api\(", line)
        var = v.group(1) if v else None
        fields = set()
        if var:
            # 窗口截止到下一次 api( 调用（避免同名响应变量污染）
            win_lines = []
            for ln in lines[i + 1:i + 40]:
                if re.search(r"""(?<![\w$])api\s*\(""", ln):
                    break
                win_lines.append(ln)
            win = "\n".join(win_lines)
            for fm in re.finditer(re.escape(var) + r"\s*\.\s*([\w$]+)", win):
                fields.add(fm.group(1))
            # 解构：const { a, b } = await this.api(...)
            dm = re.search(r"const\s*\{([^}]*)\}\s*=\s*await\s+(?:this\.)?api\(", line)
            if dm:
                for part in dm.group(1).split(","):
                    for nm in re.findall(r"[\w$]+", part):
                        fields.add(nm)
        if fields:
            reads.setdefault(act, set()).update(fields)
    return reads


def main():
    B = branch_returns()
    F = frontend_reads()
    total = 0
    for act in WATCH:
        if act not in F or act not in B:
            continue
        missing = F[act] - B[act]
        if not missing:
            continue
        total += 1
        print("== %s ==" % act)
        print("  前端读取但后端分支内无对应字面量键: %s" % ", ".join(sorted(missing)))
    if total == 0:
        print("✅ 高频 action 字段比对：无可疑断链。")


if __name__ == "__main__":
    main()
