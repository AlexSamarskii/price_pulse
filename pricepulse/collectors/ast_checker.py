from __future__ import annotations

import ast


BLOCKED_CALLS = {"system", "popen", "rmtree", "eval", "exec"}
BLOCKED_BUILTINS = {"eval", "exec", "__import__"}
ALLOWED_IMPORTS = frozenset(
    {
        "asyncio",
        "base64",
        "csv",
        "datetime",
        "hashlib",
        "html",
        "httpx",
        "json",
        "logging",
        "math",
        "pathlib",
        "re",
        "string",
        "time",
        "typing",
        "urllib",
    }
)


def check_script_safety(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax error in candidate code: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in BLOCKED_CALLS:
                return False, f"blocked dangerous function call: {node.func.attr}"
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_BUILTINS:
                return False, f"blocked builtin call: {node.func.id}"
    return True, "candidate code passed AST safety check"


def check_script_imports(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax error in candidate code: {exc}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots = (node.module,)
        else:
            continue
        for root in roots:
            package = root.split(".", 1)[0]
            if package not in ALLOWED_IMPORTS:
                return False, f"module '{package}' is not available in the offline sandbox"
    return True, "candidate imports passed the sandbox allowlist"
