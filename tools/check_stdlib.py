"""Fails if engine code imports anything outside the Python standard library (the engine must run on a bare
server with nothing but python3)."""
import ast
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if sys.version_info < (3, 10):
    print("stdlib check needs Python 3.10+ (sys.stdlib_module_names); skipped on", sys.version.split()[0])
    sys.exit(0)
local = {f[:-3] for d in ("bin", "tests", "tools", "research") for f in os.listdir(os.path.join(ROOT, d)) if f.endswith(".py")} | {"tests"}
bad = []
for d in ("bin", "tests"):
    for dirpath, _, files in os.walk(os.path.join(ROOT, d)):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            for node in ast.walk(ast.parse(open(path, encoding="utf-8").read(), path)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else []
                for n in names:
                    top = n.split(".")[0]
                    if top not in sys.stdlib_module_names and top not in local:
                        bad.append(f"{os.path.relpath(path, ROOT)}: {n}")
if bad:
    print("Third-party imports are not allowed in the engine:\n  " + "\n  ".join(bad))
    sys.exit(1)
print("ok: standard library only")
