# -*- coding: utf-8 -*-
"""refactor_api.py · 阶段 3：把 api() 铺平成 HANDLERS 注册表 + 90+ 个 @action 函数。

用法：
    python scripts/refactor_api.py --check   # 只做侦察（分支体引用检查），不改文件
    python scripts/refactor_api.py           # 执行重构（原文件先备份为 .refactor-bak）

规则（STRUCTURE-OPTIMIZATION-GUIDE 4.1）：
- 逐分支机械搬移，只搬不改：分支体原样变成函数体，签名 def h_<action>(body):
- in-组分支（bt/yt 停用提示）：生成一个 handler，6 个 action 名都注册到它
- 注册顺序 = 原分支顺序（HANDLERS 字典插入序即 sorted 前的原序）
- 新 api() 只做查表 + 未知 action 报错
"""
import ast
import sys

PY = "download-console.py"


def get_src():
    return open(PY, encoding="utf-8").read()


def get_api(tree):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "api")


def branch_infos(api_fn):
    """[(kind, key(s), lineno, end_lineno, body_stmts)]，按原顺序。"""
    out = []
    for s in api_fn.body:
        if not isinstance(s, ast.If):
            continue
        t = s.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "action" and isinstance(t.ops[0], ast.Eq)
                and isinstance(t.comparators[0], ast.Constant)):
            out.append(("eq", [t.comparators[0].value], s))
        elif (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "action" and isinstance(t.ops[0], ast.In)
                and isinstance(t.comparators[0], ast.Tuple)):
            keys = [e.value for e in t.comparators[0].elts]
            out.append(("in", keys, s))
        else:
            raise SystemExit("意外的分支形态: 行%d" % s.lineno)
    return out


def check_bodies(infos, tree):
    """侦察：分支体（不含 test）是否引用 api() 作用域里 action/body 之外的局部变量。"""
    import builtins
    problems = []
    # 模块级可用名：顶层 def/class/assign + 全部 import 名
    globals_ = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            globals_.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                globals_.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                globals_.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        globals_.add(n.id)
        elif isinstance(node, ast.AnnAssign) and node.target:
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    globals_.add(n.id)
    builtins_ = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}

    for kind, keys, s in infos:
        # 分支体内的全部绑定名：赋值/函数/类/lambda参数/except名/推导式目标/with as
        bound = set()
        for sub in ast.walk(ast.Module(body=s.body, type_ignores=[])):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                bound.add(sub.id)
            elif isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                bound.add(sub.name)
            elif isinstance(sub, ast.Lambda):
                for a in sub.args.args + sub.args.kwonlyargs:
                    bound.add(a.arg)
                if sub.args.vararg:
                    bound.add(sub.args.vararg.arg)
                if sub.args.kwarg:
                    bound.add(sub.args.kwarg.arg)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                bound.add(sub.name)
            elif isinstance(sub, ast.arg):
                bound.add(sub.arg)   # 嵌套 def 的参数
        for sub in ast.walk(ast.Module(body=s.body, type_ignores=[])):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                nm = sub.id
                if nm == "action":
                    problems.append("行%d 分支体引用了 action 变量" % s.lineno)
                elif nm not in bound and nm not in globals_ and nm not in builtins_:
                    problems.append("行%d 分支体引用未知名 %s" % (s.lineno, nm))
    return problems


def extract_lines(src, node, dedent=1):
    """按行取出 node 的源码段并整体反缩进 dedent 层（4 空格/层）。"""
    lines = src.splitlines()
    seg = lines[node.lineno - 1: node.end_lineno]
    out = []
    for ln in seg:
        if ln.startswith(" " * (4 * dedent)):
            out.append(ln[4 * dedent:])
        elif ln.strip() == "":
            out.append("")
        else:
            # 行首缩进不足（跨行字符串等），保原样
            out.append(ln)
    return out


def main():
    src = get_src()
    tree = ast.parse(src)
    api_fn = get_api(tree)
    infos = branch_infos(api_fn)

    problems = check_bodies(infos, tree)
    if problems:
        print("侦察发现问题，中止：")
        for p in problems:
            print(" ", p)
        if "--check" in sys.argv:
            return
        sys.exit(1)
    print("侦察通过：%d 个 eq 分支 + 1 组 in 分支，无外部局部变量引用" % len(infos))
    if "--check" in sys.argv:
        return

    # 生成 handler 函数源码：每个 If 节点取分支体语句行段，反缩进一层
    handlers_src = []
    seen = set()
    reg_lines = []
    for kind, keys, s in infos:
        # s 是 If 节点；分支体语句从 s.body[0].lineno 到 s.body[-1].end_lineno
        first = s.body[0].lineno
        last = s.body[-1].end_lineno
        lines = src.splitlines()
        seg = []
        for i in range(first - 1, last):
            ln = lines[i]
            if ln.strip() == "":
                seg.append("")
            elif ln.startswith("    "):
                seg.append(ln[4:])
            else:
                seg.append(ln)   # 顶层罕见；保原样
        fn_name = "h_" + keys[0] if kind == "eq" else "h_disabled_legacy"
        if kind == "in":
            head = "@action(%s)" % ", ".join('"%s"' % k for k in keys)
        else:
            head = '@action("%s")' % keys[0]
        handlers_src.append("")
        handlers_src.append(head)
        handlers_src.append("def %s(body):" % fn_name)
        if seg:
            handlers_src.extend(seg)
        else:
            handlers_src.append("    pass")
        for k in keys:
            if k in seen:
                raise SystemExit("重复 action: %s" % k)
            seen.add(k)
        reg_lines.append((keys, fn_name))

    new_api = '''
# ────────────────────────────────────────────────────────────────────────────
# v4.21 结构重构：api() 铺平为注册表。原 2,114 行 if action == "..." 分支
# 机械搬移为下面的 @action 函数（只搬不改，AST 逐分支等价断言见
# docs/audit-refactor/）。注册顺序 = 原分支顺序。
# ────────────────────────────────────────────────────────────────────────────
HANDLERS = {}


def action(*names):
    """把函数注册为 /api 的一个（或多个）action。"""
    def deco(fn):
        for n in names:
            HANDLERS[n] = fn
        return fn
    return deco
'''

    new_api_end = '''

def api(action, body):
    fn = HANDLERS.get(action)
    if fn is None:
        raise RuntimeError("未知操作")
    return fn(body)
'''

    # 替换 api() 整段
    lines = src.splitlines(keepends=False)
    before = lines[: api_fn.lineno - 1]
    after = lines[api_fn.end_lineno:]
    out = list(before)
    out.extend(new_api.rstrip("\n").split("\n"))
    out.extend(handlers_src)
    out.extend(new_api_end.strip("\n").split("\n"))
    out.extend(after)

    open(PY + ".refactor-bak", "w", encoding="utf-8", newline="\n").write(src)
    open(PY, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("重构完成：备份 %s.refactor-bak，注册 %d 个 action" % (PY, len(seen)))


if __name__ == "__main__":
    main()
