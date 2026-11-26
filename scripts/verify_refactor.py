# -*- coding: utf-8 -*-
"""verify_refactor.py · 阶段 3 验收：AST 逐分支等价断言。

比较 download-console.py.refactor-bak（原 if action == "x" 分支体）
与新文件的 h_xxx(body) 函数体。要求 ast.dump 完全一致（只搬不改）。
"""
import ast

OLD = "download-console.py.refactor-bak"
NEW = "download-console.py"


def branch_bodies(tree):
    api_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "api")
    out = []
    for s in api_fn.body:
        if not isinstance(s, ast.If):
            continue
        t = s.test
        if isinstance(t, ast.Compare) and isinstance(t.ops[0], ast.Eq):
            keys = [t.comparators[0].value]
        else:
            keys = [e.value for e in t.comparators[0].elts]
        out.append((keys, s.body))
    return out


def handler_bodies(tree):
    out = {}
    names = {}
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        for dec in n.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "action":
                keys = [a.value for a in dec.args]
                for k in keys:
                    out[k] = n.body
                    names[k] = n.name
    return out, names


def main():
    old_tree = ast.parse(open(OLD, encoding="utf-8").read())
    new_tree = ast.parse(open(NEW, encoding="utf-8").read())
    branches = branch_bodies(old_tree)
    handlers, names = handler_bodies(new_tree)
    print("原分支组 %d 个（含 in 组），新 handler %d 个 action" % (len(branches), len(handlers)))

    bad = 0
    for keys, body in branches:
        for k in keys:
            if k not in handlers:
                print("❌ 缺 handler: %s" % k)
                bad += 1
                continue
            d_old = ast.dump(ast.Module(body=body, type_ignores=[]))
            d_new = ast.dump(ast.Module(body=handlers[k], type_ignores=[]))
            if d_old != d_new:
                print("❌ AST 不等价: %s (h_%s)" % (k, names[k]))
                bad += 1
    extra = set(handlers) - {k for keys, _ in branches for k in keys}
    if extra:
        print("❌ 多出未对应原分支的 action: %s" % sorted(extra))
        bad += 1

    if bad == 0:
        print("✅ %d 个 action 全部 AST 等价（只搬不改达成）" % len(handlers))
    else:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
