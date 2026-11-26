"""结构分析：函数规模排行。

用法（仓库任意位置均可，脚本自动定位仓库根）：
    python docs/audit-refactor/tools/analyze.py

仅用标准库 ast，输出：总行数、顶层函数/类统计、20 大函数排行、类清单。
本文档体系的全部实测数据出处。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "download-console.py"

src = SRC.read_text(encoding="utf-8")
t = ast.parse(src)
lines = src.count('\n') + 1
funcs = [(n.end_lineno - n.lineno + 1, n.name, n.lineno, n.end_lineno)
          for n in t.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
classes = [(n.end_lineno - n.lineno + 1, n.name, n.lineno, n.end_lineno)
           for n in t.body if isinstance(n, ast.ClassDef)]
print('total lines', lines)
print('top-level funcs', len(funcs), 'classes', len(classes))
print('--- 20 largest top-level defs ---')
for L, name, s, e in sorted(funcs, reverse=True)[:20]:
    print('%5d  %-38s %d-%d  (%.1f%%)' % (L, name, s, e, 100.0 * L / lines))
print('--- classes ---')
for L, name, s, e in sorted(classes, reverse=True):
    print('%5d  %-38s %d-%d' % (L, name, s, e))
