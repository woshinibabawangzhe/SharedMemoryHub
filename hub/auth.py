"""Token-to-Identity Binding, Scrypt Admin Passcode & CSRF Session Guard (v0.3.0)."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .db import DEFAULT_CONFIG_DIR, HubError
from .paths import checked_path

DEFAULT_CLIENT_TOKENS_PATH = DEFAULT_CONFIG_DIR / "client_tokens.json"
DEFAULT_ADMIN_AUTH_PATH = DEFAULT_CONFIG_DIR / "admin_auth.json"


@dataclass
class CallerIdentity:
    actor_role: str  # 'agent_client' | 'user_admin'
    actor_id: str    # 'antigravity' | 'codex' | 'user'
    host_id: str     # 'antigravity' | 'codex' | 'web_ui'


class AuthManager:
    """
    Implements §3.2 Identity & Physical Credential Separation:
    - config/client_tokens.json holds ONLY AGY_CLIENT_TOKEN & CODEX_CLIENT_TOKEN.
    - config/admin_auth.json holds ONLY scrypt salted hash (zero plaintext admin token on disk).
    - Admin sessions live strictly in FastAPI process memory with CSRF & Origin verification.
    """

    def __init__(
        self,
        client_tokens_path: Path = DEFAULT_CLIENT_TOKENS_PATH,
        admin_auth_path: Path = DEFAULT_ADMIN_AUTH_PATH,
        initial_passcode: Optional[str] = None,
        allowed_origin: str = "http://127.0.0.1:8765",
    ):
        self.client_tokens_path = checked_path(client_tokens_path)
        self.admin_auth_path = checked_path(admin_auth_path)
        self.client_tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.admin_auth_path.parent.mkdir(parents=True, exist_ok=True)

        self.allowed_origin = allowed_origin
        if not self.admin_auth_path.is_file() and not initial_passcode:
            raise HubError(503, "admin_setup_required", "请先在终端运行管理员初始化脚本；不会自动创建默认口令。")
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._ensure_client_tokens()
        self._ensure_admin_auth(initial_passcode)

    def _ensure_client_tokens(self) -> None:
        if self.client_tokens_path.is_file():
            return
        data = {
            "AGY_CLIENT_TOKEN": f"smh_agy_{secrets.token_hex(24)}",
            "CODEX_CLIENT_TOKEN": f"smh_codex_{secrets.token_hex(24)}",
        }
        self.client_tokens_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _ensure_admin_auth(self, passcode: str) -> None:
        if self.admin_auth_path.is_file():
            return
        self.set_admin_passcode(passcode)

    @staticmethod
    def _scrypt_hash(passcode: str, salt: bytes, n: int = 16384, r: int = 8, p: int = 1, dklen: int = 64) -> str:
        dk = hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
        return dk.hex()

    def set_admin_passcode(self, new_passcode: str) -> None:
        if len(new_passcode) < 12:
            raise HubError(422, "weak_passcode", "管理员口令至少 12 字符。")
        salt = secrets.token_bytes(16)
        n, r, p, dklen = 16384, 8, 1, 64
        hash_hex = self._scrypt_hash(new_passcode, salt, n=n, r=r, p=p, dklen=dklen)
        doc = {
            "algorithm": "scrypt",
            "salt_hex": salt.hex(),
            "hash_hex": hash_hex,
            "n": n,
            "r": r,
            "p": p,
            "dklen": dklen,
            "note": "仅保存管理员口令的 scrypt 盐值哈希；磁盘零明文管理员凭证。",
        }
        self.admin_auth_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        self._sessions.clear()

    def verify_admin_passcode(self, passcode: str) -> bool:
        if not self.admin_auth_path.is_file():
            return False
        doc = json.loads(self.admin_auth_path.read_text(encoding="utf-8"))
        salt = bytes.fromhex(doc["salt_hex"])
        expected_hex = doc["hash_hex"]
        actual_hex = self._scrypt_hash(
            passcode,
            salt=salt,
            n=int(doc.get("n", 16384)),
            r=int(doc.get("r", 8)),
            p=int(doc.get("p", 1)),
            dklen=int(doc.get("dklen", 64)),
        )
        return hmac.compare_digest(expected_hex, actual_hex)

    def create_admin_session(self, passcode: str, ttl_seconds: int = 14400) -> Dict[str, Any]:
        if not self.verify_admin_passcode(passcode):
            raise HubError(401, "invalid_admin_passcode", "管理员口令错误。")
        session_id = f"adm_sess_{secrets.token_urlsafe(32)}"
        csrf_token = f"csrf_{secrets.token_urlsafe(24)}"
        now = time.time()
        self._sessions[session_id] = {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "created_at": now,
            "expires_at": now + ttl_seconds,
        }
        return {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "expires_in": ttl_seconds,
        }

    def get_client_tokens(self) -> Dict[str, str]:
        return json.loads(self.client_tokens_path.read_text(encoding="utf-8"))

    def authenticate_bearer_or_session(
        self,
        authorization_header: Optional[str] = None,
        session_cookie: Optional[str] = None,
        session_header: Optional[str] = None,
    ) -> CallerIdentity:
        # 1. Explicit Authorization: Bearer <token> or X-Admin-Session header takes precedence over browser cookie
        if session_header and session_header in self._sessions:
            sess = self._sessions[session_header]
            if time.time() < sess["expires_at"]:
                return CallerIdentity(actor_role="user_admin", actor_id="user", host_id="web_ui")
            self._sessions.pop(session_header, None)

        if authorization_header and authorization_header.lower().startswith("bearer "):
            token = authorization_header[7:].strip()
            if token in self._sessions:
                sess = self._sessions[token]
                if time.time() < sess["expires_at"]:
                    return CallerIdentity(actor_role="user_admin", actor_id="user", host_id="web_ui")
                self._sessions.pop(token, None)

            tokens = self.get_client_tokens()
            agy_tok = tokens.get("AGY_CLIENT_TOKEN", "")
            codex_tok = tokens.get("CODEX_CLIENT_TOKEN", "")
            if agy_tok and hmac.compare_digest(token, agy_tok):
                return CallerIdentity(actor_role="agent_client", actor_id="antigravity", host_id="antigravity")
            if codex_tok and hmac.compare_digest(token, codex_tok):
                return CallerIdentity(actor_role="agent_client", actor_id="codex", host_id="codex")

        # 2. Fallback to browser cookie smh_admin_session
        if session_cookie and session_cookie in self._sessions:
            sess = self._sessions[session_cookie]
            if time.time() < sess["expires_at"]:
                return CallerIdentity(actor_role="user_admin", actor_id="user", host_id="web_ui")
            self._sessions.pop(session_cookie, None)

        raise HubError(401, "unauthorized", "缺少有效客户端 Token 或管理员会话。")

    def require_admin_session(
        self,
        method: str,
        authorization_header: Optional[str] = None,
        session_cookie: Optional[str] = None,
        session_header: Optional[str] = None,
        csrf_header: Optional[str] = None,
        origin_header: Optional[str] = None,
    ) -> CallerIdentity:
        # First detect if an agent_client Bearer token was used -> 403 Forbidden
        if authorization_header and authorization_header.lower().startswith("bearer "):
            tok = authorization_header[7:].strip()
            tokens = self.get_client_tokens()
            if (tokens.get("AGY_CLIENT_TOKEN") and hmac.compare_digest(tok, tokens["AGY_CLIENT_TOKEN"])) or (
                tokens.get("CODEX_CLIENT_TOKEN") and hmac.compare_digest(tok, tokens["CODEX_CLIENT_TOKEN"])
            ):
                raise HubError(403, "admin_role_required", "AI 客户端 Token 严禁调用管理员端点（/v1/admin/*）。")

        sess_id = session_cookie or session_header
        if not sess_id and authorization_header and authorization_header.lower().startswith("bearer "):
            sess_id = authorization_header[7:].strip()

        if not sess_id or sess_id not in self._sessions:
            raise HubError(401, "admin_session_required", "需要有效的管理员登录会话。")

        sess = self._sessions[sess_id]
        if time.time() >= sess["expires_at"]:
            self._sessions.pop(sess_id, None)
            raise HubError(401, "admin_session_expired", "管理员会话已过期，请重新输入口令登录。")

        if method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
            if origin_header != self.allowed_origin:
                raise HubError(403, "csrf_origin_forbidden", "Origin 缺失或与服务配置不匹配。")
            if not csrf_header or not hmac.compare_digest(csrf_header, sess["csrf_token"]):
                raise HubError(403, "csrf_token_invalid", "缺少或无效的 X-CSRF-Token 头。")

        return CallerIdentity(actor_role="user_admin", actor_id="user", host_id="web_ui")
