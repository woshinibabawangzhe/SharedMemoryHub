"""Shared Zero-Cache Memory CLI for Antigravity & Codex (v0.2.1, §5.1)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

HUB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKENS_PATH = HUB_ROOT / "config" / "client_tokens.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT_SEC = 1.5


def load_client_token(client_name: str, tokens_path: Path = DEFAULT_TOKENS_PATH) -> str:
    env_key = "AGY_CLIENT_TOKEN" if client_name == "antigravity" else "CODEX_CLIENT_TOKEN"
    if os.environ.get(env_key):
        return os.environ[env_key]
    if not tokens_path.is_file():
        raise FileNotFoundError(f"Client token file not found: {tokens_path}")
    doc = json.loads(tokens_path.read_text(encoding="utf-8"))
    tok = doc.get(env_key)
    if not tok:
        raise KeyError(f"Token key {env_key} missing in {tokens_path}")
    return tok


def call_hub(
    method: str,
    path: str,
    client_name: str = "antigravity",
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    tokens_path: Path = DEFAULT_TOKENS_PATH,
) -> Dict[str, Any]:
    """
    Zero-cache HTTP call to 127.0.0.1:8765.
    Never caches responses on disk; returns explicit MEMORY_HUB_UNAVAILABLE on connection/timeout failure.
    """
    try:
        token = load_client_token(client_name, tokens_path=tokens_path)
    except Exception as e:
        return {
            "status": "MEMORY_HUB_UNAVAILABLE",
            "reason": f"client_token_error: {e}",
            "items": [],
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Cache-Control": "no-store, no-cache, must-revalidate",
    }
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(connect=min(timeout_sec,1.5),read=5.0,write=5.0,pool=1.5), trust_env=False) as client:
            if method.upper() == "GET":
                resp = client.get(path, params=params, headers=headers)
            else:
                resp = client.request(method.upper(), path, json=payload, params=params, headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {"raw_text": resp.text}
            data["http_status"] = resp.status_code
            return data
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
        writing = method.upper() not in ('GET','HEAD') and path not in ('/v1/memory/search','/v1/memory/precheck','/v1/admin/query')
        return {
            "status": "WRITE_RESULT_UNKNOWN" if writing else "MEMORY_HUB_UNAVAILABLE",
            "request_key": (payload or {}).get("request_key") if writing else None,
            "retry_policy": "保留原请求体及原 request_key 查询或重试；禁止换键重发。" if writing else None,
            "http_status": 503,
            "reason": f"{type(exc).__name__}: {exc}",
            "items": [],
            "notice": "记忆中心未启动或不可达，本次仅依据当前工作区与会话上下文执行，未加载历史长期记忆。",
        }


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="SharedMemoryHub Zero-Cache CLI (Antigravity / Codex)")
    parser.add_argument("--client", choices=["antigravity", "codex"], default="antigravity")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)

    parser.add_argument("--tokens-path", type=Path, default=DEFAULT_TOKENS_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="Check Hub health")

    p_pre = sub.add_parser("precheck", help="Session/Task memory precheck")
    p_pre.add_argument("--workspace", default=None)
    p_pre.add_argument("--query", required=True)
    p_pre.add_argument("--limit", type=int, default=8)

    p_srch = sub.add_parser("search", help="Hybrid memory search")
    p_srch.add_argument("--workspace", default=None)
    p_srch.add_argument("--query", required=True)
    p_srch.add_argument("--subjects", default=None, help="Comma-separated subjects")
    p_srch.add_argument("--kinds", default=None, help="Comma-separated kinds")
    p_srch.add_argument("--limit", type=int, default=8)

    p_get = sub.add_parser("get", help="Get authoritative memory by ID")
    p_get.add_argument("--id", required=True)
    p_get.add_argument("--workspace", default=None)

    p_wr = sub.add_parser("write", help="Write or propose memory mutation")
    p_wr.add_argument("--op", required=True, choices=["create", "update", "soft_delete", "propose_conflict"])
    p_wr.add_argument("--id", default=None)
    p_wr.add_argument("--expected-revision", type=int, default=None)
    p_wr.add_argument("--request-key", default=None)
    p_wr.add_argument("--scope", default=None, choices=["global", "user_global", "project", "agent_private"])
    p_wr.add_argument("--workspace", default=None)
    p_wr.add_argument("--subject", default=None, choices=["user", "project", "shared", "antigravity", "codex"])
    p_wr.add_argument("--kind", default=None, choices=["preference", "decision", "fact", "context", "experience", "handoff", "rule_candidate"])
    p_wr.add_argument("--topic-key", default=None)
    p_wr.add_argument("--title", default=None)
    p_wr.add_argument("--content", default=None)
    p_wr.add_argument("--confidence", default=None, choices=["confirmed", "hypothesis"])
    p_wr.add_argument("--origin-type", default=None)
    p_wr.add_argument("--evidence-ref", default=None)
    p_wr.add_argument("--evidence-sha256", default=None)
    p_wr.add_argument("--session-id", default=None)
    p_wr.add_argument("--summary", default="CLI write")

    args = parser.parse_args(argv)

    if args.cmd == "health":
        res = call_hub("GET", "/v1/health", client_name=args.client, base_url=args.base_url, tokens_path=args.tokens_path, timeout_sec=args.timeout)
    elif args.cmd == "precheck":
        res = call_hub(
            "POST",
            "/v1/memory/precheck",
            client_name=args.client,
            payload={"workspace_path": args.workspace, "query": args.query, "limit": args.limit},
            base_url=args.base_url, tokens_path=args.tokens_path,
            timeout_sec=args.timeout,
        )
    elif args.cmd == "search":
        res = call_hub(
            "POST",
            "/v1/memory/search",
            client_name=args.client,
            payload={
                "workspace_path": args.workspace,
                "query": args.query,
                "subjects": [s.strip() for s in args.subjects.split(",") if s.strip()] if args.subjects else None,
                "kinds": [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None,
                "limit": args.limit,
            },
            base_url=args.base_url, tokens_path=args.tokens_path,
            timeout_sec=args.timeout,
        )
    elif args.cmd == "get":
        params = {"workspace_path": args.workspace} if args.workspace else None
        res = call_hub(
            "GET",
            f"/v1/memory/{args.id}",
            client_name=args.client,
            params=params,
            base_url=args.base_url, tokens_path=args.tokens_path,
            timeout_sec=args.timeout,
        )
    else:
        if not args.request_key:
            parser.error("写入必须提供 --request-key；超时必须沿用此键和原请求体。")
        payload = {
            "request_key": args.request_key or f"cli_{uuid.uuid4().hex[:16]}",
            "operation": args.op,
            "memory_id": args.id,
            "expected_revision": args.expected_revision,
            "scope": args.scope,
            "workspace_path": args.workspace,
            "subject": args.subject,
            "kind": args.kind,
            "topic_key": args.topic_key,
            "title": args.title,
            "content": args.content,
            "confidence": args.confidence,
            "source": {
                "origin_type": args.origin_type,
                "session_id": args.session_id,
                "evidence_ref": args.evidence_ref,
                "evidence_sha256": args.evidence_sha256,
                "summary": args.summary,
            },
        }
        payload = {k:v for k,v in payload.items() if v is not None}
        payload["source"] = {k:v for k,v in payload["source"].items() if v is not None}
        res = call_hub(
            "POST",
            "/v1/memory/write",
            client_name=args.client,
            payload=payload,
            base_url=args.base_url, tokens_path=args.tokens_path,
            timeout_sec=args.timeout,
        )

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 2 if int(res.get("http_status", 200)) >= 400 or res.get("status") in ("MEMORY_HUB_UNAVAILABLE", "WRITE_RESULT_UNKNOWN", "error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
