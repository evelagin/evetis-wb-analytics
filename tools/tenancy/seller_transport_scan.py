"""Offline AST gate for normal runtime credentialed Seller sinks (no runtime imports)."""
from __future__ import annotations

import ast
from pathlib import Path

HTTP_IMPORTS = ("urllib.request", "requests", "httpx", "aiohttp", "http.client", "socket")
PRIVATE = {"secret", "_seller_headers", "seller_headers", "_request", "_seller_open", "_NoSellerRedirect"}


def violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    errors = []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None
    transport = filename == "common.py"
    approved = {
        "secret": {"seller_client_id", "perf_client_id", "_seller_headers", "perf_token"},
        "_seller_headers": {"seller_call"},
        "_request": {"_request", "seller_call", "perf_token", "perf_get", "perf_post"},
        "_seller_open": {"_request"}, "_NoSellerRedirect": {"_seller_open"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if any(item.name.startswith(p) for p in HTTP_IMPORTS) and not (transport and item.name == "urllib.request"):
                    errors.append(f"{filename}:{node.lineno}: direct HTTP import")
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                if any((node.module or "").startswith(p) for p in HTTP_IMPORTS) or item.name in PRIVATE:
                    errors.append(f"{filename}:{node.lineno}: transport/credential bypass import")
        elif isinstance(node, ast.Attribute) and node.attr in PRIVATE and not (transport and scope(node) in approved.get(node.attr, set())):
            errors.append(f"{filename}:{node.lineno}: private transport/credential reference")
        elif isinstance(node, ast.Call):
            f = ast.unparse(node.func)
            if (f in PRIVATE and not (transport and scope(node) in approved.get(f, set()))) or f in {"eval", "exec", "__import__"}:
                errors.append(f"{filename}:{node.lineno}: dynamic/raw sink")
            if f == "getattr" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value in PRIVATE:
                errors.append(f"{filename}:{node.lineno}: indirect private sink")
            if transport and f.startswith("urllib.request."):
                allowed = {"urllib.request.urlopen": {"_request"},
                           "urllib.request.build_opener": {"_seller_open"},
                           "urllib.request.Request": {"seller_call", "perf_token", "perf_get", "perf_post"}}
                if f == "urllib.request.build_opener(_NoSellerRedirect()).open":
                    allowed[f] = {"_seller_open"}
                if scope(node) not in allowed.get(f, set()):
                    errors.append(f"{filename}:{node.lineno}: unreviewed transport HTTP sink")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in PRIVATE and transport:
            if scope(node) not in approved.get(node.id, set()):
                errors.append(f"{filename}:{node.lineno}: private transport reference outside boundary")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() in ("api-key", "client-id"):
            if not (transport and scope(node) in {"_seller_headers", "_request"}):
                errors.append(f"{filename}:{node.lineno}: credential header outside transport")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PRIVATE and not transport:
            errors.append(f"{filename}:{node.lineno}: reflective private sink")
    return sorted(set(errors))


def scan(root: Path):
    return [issue for p in sorted(root.glob("*.py")) for issue in violations(p.read_text(), p.name)]


if __name__ == "__main__":
    import sys
    issues = scan(Path(__file__).resolve().parents[2] / "pipelines/ozon/runtime")
    for issue in issues:
        print(issue)
    print("SELLER_TRANSPORT_AST " + ("FAIL" if issues else "PASS"))
    sys.exit(bool(issues))
