#!/usr/bin/env python3
"""代码地图(Graphify AST思想的最小版): tree-sitter本地解析python文件→函数/类+调用关系
零LLM成本, coder器官任务前先查地图少读文件。输出: {file: {classes:[], funcs:[], calls:[]}}
"""
import os
from tree_sitter import Language, Parser
import tree_sitter_python

PY_LANG = Language(tree_sitter_python.language())
_parser = Parser(PY_LANG)

def parse_file(path: str) -> dict:
    """单文件 → {classes, funcs, imports} (函数名+行号)"""
    try:
        src = open(path, "rb").read()
    except Exception:
        return {}
    tree = _parser.parse(src)
    out = {"classes": [], "funcs": [], "imports": []}
    def walk(node):
        if node.type == "class_definition":
            name = node.child_by_field_name("name")
            if name: out["classes"].append({"name": name.text.decode(), "line": node.start_point[0]+1})
        elif node.type == "function_definition":
            name = node.child_by_field_name("name")
            if name: out["funcs"].append({"name": name.text.decode(), "line": node.start_point[0]+1})
        elif node.type in ("import_statement", "import_from_statement"):
            out["imports"].append(node.text.decode()[:60])
        for ch in node.children:
            walk(ch)
    walk(tree.root_node)
    return out

def build_map(root: str, exts=( ".py",)) -> dict:
    """目录递归 → 代码地图"""
    result = {}
    for dirpath, _, files in os.walk(root):
        if any(s in dirpath for s in ("/.git", "__pycache__", "node_modules", "/.venv")):
            continue
        for f in files:
            if os.path.splitext(f)[1] in exts:
                p = os.path.join(dirpath, f)
                info = parse_file(p)
                if info and (info["classes"] or info["funcs"]):
                    result[os.path.relpath(p, root)] = info
    return result

def to_digest(codemap: dict, max_lines: int = 40) -> str:
    """地图→给LLM的紧凑摘要(它不用再读全库文件)"""
    lines = []
    for path, info in sorted(codemap.items()):
        classes = ", ".join(c["name"] for c in info["classes"])
        funcs = ", ".join(f["name"] for f in info["funcs"])
        entry = f"{path}: "
        if classes: entry += f"[{classes}] "
        if funcs: entry += funcs
        lines.append(entry)
    return "\n".join(lines[:max_lines])

if __name__ == "__main__":
    import sys, json
    root = sys.argv[1] if len(sys.argv) > 1 else "/root/nono-mind"
    m = build_map(root)
    print(f"文件数: {len(m)}")
    print(to_digest(m))
