#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghost_check.py · 前端一致性一次性审计脚本（阶段 1.2，提示词 C）

检查 static/index.html 模板引用的标识符（插值 {{ }} / 指令值 / @click / :prop）
是否都能在 static/app.js 的 data / computed / methods 中找到定义。
JS 内置（Math、JSON…）、循环变量（v-for 局部）、$event 等自动排除。
"""
import re
import sys

ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
HTML = ROOT + "/static/index.html"
JS = ROOT + "/static/app.js"

# v-for 引入的局部变量：v-for="x in list" / "(x, i) in list"
FOR_RE = re.compile(r'v-for="\s*\(?\s*([\w$]+)\s*(?:,\s*([\w$]+))?\)?\s+(?:in|of)\s')
# 模板表达式：{{ ... }} 与指令值（v-if / v-show / v-else-if / :xxx / @xxx / v-bind）
MUSTACHE_RE = re.compile(r"\{\{(.+?)\}\}", re.S)
DIRECTIVE_RE = re.compile(r'(?:^|\s)(?:v-if|v-else-if|v-show|:[\w:.-]+|@[\w:.-]+|v-model(?:\.[\w.]+)?|v-for)="([^"]*)"', re.M)

JS_BUILTINS = {
    "Math", "JSON", "Array", "Object", "String", "Number", "Boolean", "Date",
    "console", "window", "document", "localStorage", "navigator", "true",
    "false", "null", "undefined", "this", "$event", "in", "of", "typeof",
    "instanceof", "new", "length", "console", "Infinity", "NaN", "Date",
    "encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat",
    "isNaN", "alert", "confirm", "prompt", "setTimeout", "setInterval",
    "clearTimeout", "clearInterval", "fetch", "URL", "RegExp", "Map", "Set",
}


def main():
    html = open(HTML, encoding="utf-8").read()
    js = open(JS, encoding="utf-8").read()

    # 1. app.js 里全部定义的符号（宽松：出现 identifier 作为属性名或函数名即算）
    defined = set(re.findall(r"(?m)^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*[:(=]", js))
    defined |= set(re.findall(r"(?:this\.|vm\.)?([A-Za-z_$][\w$]*)\s*\(", js))  # 调用过的名字必然可解析
    # Vue data 顶层：直接抓 app 对象的属性赋值（含注释同行的）
    defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*:", js))
    # 2. v-for 局部变量（含 v-for 在同一标签内引入的名字，逐标签扫描）
    locals_ = set()
    for m in re.finditer(r"<[a-zA-Z][^>]*>", html):
        tag = m.group(0)
        for f in FOR_RE.finditer(tag):
            locals_.add(x for x in f.groups() if x)
        for f in FOR_RE.finditer(tag):
            for g in f.groups():
                if g:
                    locals_.add(g)
    # v-slot / slot props
    for m in re.finditer(r'(?:#|v-slot:)[\w-]+="([\w$, ]+)"', html):
        for g in re.findall(r"[\w$]+", m.group(1)):
            locals_.add(g)

    # 3. 收集模板表达式
    exprs = []
    for m in MUSTACHE_RE.finditer(html):
        exprs.append(m.group(1))
    for m in DIRECTIVE_RE.finditer(html):
        exprs.append(m.group(1))

    # 4. 提取表达式里的标识符（排除属性访问 .x 的右部、字符串、数字）
    used = {}
    for e in exprs:
        # 去掉字符串
        e2 = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "", e)
        # 标识符：不在 . 右侧（属性）、不在引号里
        for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)", e2):
            name = m.group(1)
            # 排除 . 左侧对象的属性访问：如果前面紧跟 '.' 已被 lookbehind 排除
            used.setdefault(name, 0)
            used[name] += 1

    ghosts = []
    for name in sorted(used):
        if name in JS_BUILTINS or name in locals_:
            continue
        if name in defined:
            continue
        # 全词搜索兜底（大小写敏感）
        if re.search(r"(?<![\w$.@'\"])" + re.escape(name) + r"(?![\w$])", js):
            continue
        ghosts.append((name, used[name]))

    if ghosts:
        print("疑似幽灵标识符（模板用到、app.js 找不到定义）：")
        for n, c in ghosts:
            print("  %s  (%d 处)" % (n, c))
    else:
        print("✅ 模板引用的标识符全部有定义，无幽灵字段/方法。")
    print("（检查了 %d 个表达式，%d 个不同标识符，%d 个 v-for 局部变量）"
          % (len(exprs), len(used), len(locals_)))


if __name__ == "__main__":
    sys.exit(main())
