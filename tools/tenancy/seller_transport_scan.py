"""Offline AST gate for normal runtime credentialed Seller sinks (no runtime imports)."""
from __future__ import annotations

import ast
from pathlib import Path

HTTP_IMPORTS = ("urllib.request", "requests", "httpx", "aiohttp", "http.client", "socket", "google.auth.transport", "googleapiclient", "grpc", "subprocess", "runpy", "importlib", "ctypes")
PUBLIC = {
    "ApiPathDenied", "ConfigError", "JournalWriteError", "DATASET", "PROJECT", "REF_DATASET",
    "LOCATION", "RUNS_TABLE", "STATS", "STRICT_PAGE_CAPS", "LEGACY_INGESTION_PROJECT", "CONFIG",
    "SELLER_ALLOWED_PATHS", "SELLER_PROFILES", "seller_call", "seller_post", "seller_client_id",
    "perf_client_id", "perf_token", "perf_get", "perf_post", "bq", "h", "log", "now_msk",
    "merge_rows", "append_rows", "record_run", "safe_error_text", "safe_excepthook", "redact_value",
    "promo_slot", "promo_observation_id", "promo_load_job_id", "PROMO_SLOT_HOURS_UTC",
}
PRIVATE = {"secret", "_seller_headers", "seller_headers", "_request", "_seller_open", "_NoSellerRedirect", "_secrets", "_sm", "urllib", "secretmanager", "_validate_seller_route", "__globals__", "__closure__", "__dict__", "_getframe", "_http", "_connection", "_credentials", "api_request", "transport", "_transport", "_session"}


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
    common_aliases = {i.asname or i.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                      for i in n.names if i.name == "common"}
    credential_sdk = "google.cloud.secretmanager"

    approved = {
        "_secrets": {"secret"}, "_sm": {"secret"},
        "_validate_seller_route": {"seller_call", "_request"},
        "_getframe": {"secret", "_check", "_seller_headers"},
        "secret": {"secret", "_check", "seller_client_id", "perf_client_id", "_seller_headers", "perf_token"},
        "_seller_headers": {"seller_call", "secret"},
        "_request": {"_request", "seller_call", "perf_token", "perf_get", "perf_post"},
        "_seller_open": {"_request"}, "_NoSellerRedirect": {"_seller_open"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.startswith(credential_sdk) or any(item.name.startswith(p) for p in HTTP_IMPORTS) and not (transport and item.name == "urllib.request"):
                    errors.append(f"{filename}:{node.lineno}: direct HTTP import")
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                if (any((node.module or "").startswith(p) or ((node.module or "") + "." + item.name).startswith(p) for p in HTTP_IMPORTS)
                        or (node.module or "").startswith(credential_sdk)
                        or (node.module == "common" and item.name not in PUBLIC)
                        or (item.name in PRIVATE and not (transport and node.module == "google.cloud" and item.name == "secretmanager"))):
                    errors.append(f"{filename}:{node.lineno}: transport/credential bypass import")
        elif isinstance(node, ast.Attribute) and (
                (node.attr in PRIVATE and not (transport and (node.attr in {"urllib", "secretmanager"} or scope(node) in approved.get(node.attr, set()))))
                or (isinstance(node.value, ast.Name) and node.value.id in common_aliases
                    and (node.attr not in PUBLIC or isinstance(node.ctx, (ast.Store, ast.Del))) and not (
                        node.attr == "_seller_execution_scope" and filename in {"main.py", "lifecycle.py"}
                        and scope(node) in {"ingestion_execution_contract", "main"}))
                or (node.attr in {"SecretManagerServiceClient", "access_secret_version"} and not (transport and scope(node) == "secret"))):
            errors.append(f"{filename}:{node.lineno}: private transport/credential reference")
        elif isinstance(node, ast.Call):
            f = ast.unparse(node.func)
            if (f in PRIVATE and not (transport and scope(node) in approved.get(f, set()))) or f in {"eval", "exec", "__import__", "globals", "locals"}:
                errors.append(f"{filename}:{node.lineno}: dynamic/raw sink")
            if (f in {"getattr", "setattr", "delattr", "vars"} and node.args
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in common_aliases) or (
                    f == "getattr" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value in PRIVATE):
                errors.append(f"{filename}:{node.lineno}: indirect private sink")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "request", "api_request", "urlopen", "urlretrieve", "execute", "send", "system", "popen", "execv", "execl", "spawnv"}:
                if not (transport and f == "urllib.request.urlopen" and scope(node) == "_request"):
                    errors.append(f"{filename}:{node.lineno}: raw SDK/HTTP dispatch")
            if f == "getattr" and (len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)):
                errors.append(f"{filename}:{node.lineno}: dynamic attribute dispatch")
            if transport and f.startswith("urllib.request."):
                allowed = {"urllib.request.urlopen": {"_request"},
                           "urllib.request.build_opener": {"_seller_open"},
                           "urllib.request.Request": {"seller_call", "perf_token", "perf_get", "perf_post"}}
                if f == "urllib.request.build_opener(_NoSellerRedirect()).open":
                    allowed[f] = {"_seller_open"}
                if scope(node) not in allowed.get(f, set()):
                    errors.append(f"{filename}:{node.lineno}: unreviewed transport HTTP sink")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in PRIVATE - {"urllib", "secretmanager"} and transport:
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
