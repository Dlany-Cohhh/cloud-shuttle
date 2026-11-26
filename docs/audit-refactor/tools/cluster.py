"""依赖簇分析：api() 分支与顶层函数的并查集。

用法（仓库任意位置均可）：
    python docs/audit-refactor/tools/cluster.py

回答两个问题：
1. api() 有多少分支、每个分支多大（重构第 1 步的工作量底稿）；
2. 按业务域切文件是否可行（若绝大多数分支同簇，则不可行）。
"""
import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "download-console.py"

src = SRC.read_text(encoding="utf-8")
tree = ast.parse(src)
total = src.count('\n') + 1

top = {}
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        top[n.name] = (n.lineno, 'def')
    elif isinstance(n, ast.ClassDef):
        top[n.name] = (n.lineno, 'class')
    elif isinstance(n, ast.Assign):
        for t_ in n.targets:
            if isinstance(t_, ast.Name):
                top[t_.id] = (n.lineno, 'var')


def local_names(node):
    out = set()
    for a in ast.walk(node):
        if isinstance(a, ast.arg):
            out.add(a.arg)
        elif isinstance(a, ast.Name) and isinstance(a.ctx, (ast.Store, ast.Del)):
            out.add(a.id)
        elif isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(a.name)
        elif isinstance(a, (ast.Import, ast.ImportFrom)):
            for al in a.names:
                out.add((al.asname or al.name).split('.')[0])
        elif isinstance(a, ast.ExceptHandler) and a.name:
            out.add(a.name)
        elif isinstance(a, ast.Global):
            for nm in a.names:
                out.discard(nm)
    return out


def refs_of(node):
    loc = local_names(node)
    return {a.id for a in ast.walk(node)
            if isinstance(a, ast.Name) and isinstance(a.ctx, ast.Load)
            and a.id in top and a.id not in loc}


api_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api')
branches = []
for stmt in api_fn.body:
    if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare):
        left = stmt.test.left
        if isinstance(left, ast.Name) and left.id == 'action':
            act = stmt.test.comparators[0]
            if isinstance(act, ast.Constant):
                branches.append((act.value, stmt.lineno, stmt.end_lineno,
                                 stmt.end_lineno - stmt.lineno + 1, refs_of(stmt)))

api_lines = api_fn.end_lineno - api_fn.lineno + 1
print('文件总行数               : %d' % total)
print('顶层函数/类/常量         : %d' % len(top))
print('api() 行数               : %d  (%.1f%%)' % (api_lines, 100 * api_lines / total))
print('api() 动作分支           : %d' % len(branches))
if branches:
    bl = [b[3] for b in branches]
    print('分支行数 min/中位/max    : %d / %d / %d' % (min(bl), sorted(bl)[len(bl) // 2], max(bl)))
    print('分支合计行数             : %d  (占 api() 的 %.1f%%)' % (sum(bl), 100 * sum(bl) / api_lines))

parent = {}


def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb


handlers = {}
for act, s, e, ln, refs in branches:
    nid = 'H:' + act
    handlers[nid] = (act, s, e, ln)
    find(nid)
    for r in refs:
        if top.get(r, (0, ''))[1] == 'def':
            union(nid, 'F:' + r)

fn_nodes = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        find('F:' + n.name)
        for r in refs_of(n):
            if top.get(r, (0, ''))[1] == 'def':
                union('F:' + n.name, 'F:' + r)

groups = defaultdict(set)
for k in parent:
    groups[find(k)].add(k)

clusters = []
for root_, members in groups.items():
    hs = [handlers[m] for m in members if m in handlers]
    if not hs:
        continue
    fs = [m[2:] for m in members if not m.startswith('H:')]
    flines = sum(fn_nodes[f].end_lineno - fn_nodes[f].lineno + 1 for f in fs if f in fn_nodes)
    clusters.append((sum(h[3] for h in hs), len(hs), flines, fs, hs))

print('\n=== 含 handler 的依赖簇（按分支行数排序）===')
for bl_, hn, fl, fs, hs in sorted(clusters, reverse=True)[:12]:
    names = ', '.join(h[0] for h in sorted(hs, key=lambda x: -x[3])[:6])
    print('%-6d %-7d %-8d %s' % (bl_, hn, len(fs), names[:88]))
print('\n其中最大的 3 个簇：')
for bl_, hn, fl, fs, hs in sorted(clusters, reverse=True)[:3]:
    print('  [%d handler / %d 函数 / %d 行] %s' % (hn, len(fs), fl, ', '.join(sorted(fs)[:14])))
