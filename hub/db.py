"""Authoritative SQLite storage, AdmissionGuard, Hybrid Search, Undo/Restore, Reindex & Purge (v0.3.0)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from .paths import PROJECT_ROOT, checked_path

HUB_ROOT = PROJECT_ROOT
DEFAULT_DB_PATH = HUB_ROOT / "data" / "sqlite" / "memory_hub.db"
DEFAULT_TOMBSTONE_PATH = HUB_ROOT / "data" / "tombstones" / "purge_registry.jsonl"
DEFAULT_EXPORTS_DIR = HUB_ROOT / "exports"
DEFAULT_BACKUPS_DIR = HUB_ROOT / "backups"
DEFAULT_CONFIG_DIR = HUB_ROOT / "config"
DEFAULT_LOCK_PATH = HUB_ROOT / "data" / "hub.lock"
DEFAULT_SESSIONS_DIR = HUB_ROOT / "source-snapshots" / "sessions"

DEFAULT_GENERATION = "v1"
SCHEMA_VERSION = 600

TOPIC_KEY_RE = re.compile(r"^[a-z0-9_.-]{3,80}$")

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\b(api_key|password|secret_key|access_token)\s*=\s*['\"]?[A-Za-z0-9._\-]{8,}"),
]

PRIVILEGE_ESCALATION_PATTERNS = [
    re.compile(r"(?i)ignore\s+previous\s+instructions"),
    re.compile(r"忽略审批"),
    re.compile(r"自动开启\s*auto_continue"),
    re.compile(r"允许读取\s*\.env"),
    re.compile(r"绕过权限"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def deterministic_point_id(generation: str, memory_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"shared-memory-hub:{generation}:{memory_id}"))


class HubError(Exception):
    def __init__(self, status_code: int, error_code: str, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.extra = extra or {}
        self.detail = self.extra


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS current_memory (
    memory_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    indexed_revision INTEGER NOT NULL DEFAULT 0,
    index_generation TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','paused','conflict_pending','deleted')),
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    has_open_conflict INTEGER NOT NULL DEFAULT 0 CHECK(has_open_conflict IN (0, 1)),
    subject TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('global','user_global','project','agent_private')),
    project_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('preference','decision','fact','context','experience','handoff','rule_candidate')),
    topic_key TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('confirmed','hypothesis')),
    readers_json TEXT NOT NULL,
    host_constraints_json TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('user_explicit','project_fact','verified_receipt','model_inference','task_state','admin_manual')),
    source_json TEXT NOT NULL,
    domain_id TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    review_due_at TEXT,
    last_reviewed_at TEXT,
    created_by_actor_id TEXT NOT NULL,
    created_in_session_id TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_bindings (
    project_id TEXT PRIMARY KEY,
    workspace_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_current_memory_lookup
ON current_memory(subject, scope, project_id, state, confidence);

CREATE INDEX IF NOT EXISTS idx_current_memory_topic
ON current_memory(subject, scope, project_id, topic_key);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    topic_key,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS revisions (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    state TEXT NOT NULL,
    locked INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_snapshot_json TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_json TEXT NOT NULL,
    restored_from INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, revision)
);

CREATE TABLE IF NOT EXISTS requests (
    request_key TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    applied_revision INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    is_purged INTEGER NOT NULL DEFAULT 0 CHECK(is_purged IN (0, 1)),
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_work (
    work_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    generation TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('upsert','delete')),
    status TEXT NOT NULL CHECK(status IN ('pending','processing','done','superseded','failed')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    next_retry_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_index_work_queue
ON index_work(status, next_retry_at, work_id);

CREATE TABLE IF NOT EXISTS conflict_proposals (
    proposal_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    proposal_type TEXT NOT NULL CHECK(proposal_type IN (
        'credible_fact_conflict',
        'update_proposal',
        'delete_proposal',
        'locked_override_attempt',
        'scope_expansion',
        'rule_candidate'
    )),
    proposed_title TEXT,
    proposed_content TEXT,
    proposed_metadata_json TEXT NOT NULL,
    proposer_role TEXT NOT NULL,
    proposer_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','edited','merged')),
    resolution TEXT,
    resolved_by TEXT,
    resolved_by_revision INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK(event_type IN ('precheck','search','get','write','conflict_hold','undo','restore','lock_toggle','soft_delete','purge','backup','reindex')),
    memory_id TEXT,
    revision INTEGER,
    actor_role TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    scope TEXT,
    project_id TEXT,
    result_status TEXT NOT NULL,
    detail_meta_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purge_tombstones (
    memory_id TEXT PRIMARY KEY,
    purge_seq INTEGER NOT NULL UNIQUE,
    entry_hash TEXT NOT NULL,
    purged_content_sha256 TEXT NOT NULL,
    purged_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    sqlite_done INTEGER NOT NULL DEFAULT 0,
    vector_done INTEGER NOT NULL DEFAULT 0,
    exports_done INTEGER NOT NULL DEFAULT 0,
    compact_done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_redactions (
    source_id TEXT PRIMARY KEY,
    purged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hub_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domains (
    domain_id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS source_versions (
    source_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
    content TEXT NOT NULL, sha256 TEXT NOT NULL, original_sha256 TEXT,
    original_path TEXT, registered_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
    PRIMARY KEY(source_id,revision)
);
CREATE TABLE IF NOT EXISTS source_links (
    memory_id TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
    PRIMARY KEY(memory_id,source_id,source_revision)
);
CREATE TABLE IF NOT EXISTS saved_views (
    view_id TEXT PRIMARY KEY, name TEXT NOT NULL, filters_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
"""


from .drafts import DRAFT_SCHEMA_SQL, DraftsMixin  # noqa: E402
from .related import RELATED_SCHEMA_SQL, RelatedMemoryMixin
SCHEMA_SQL += DRAFT_SCHEMA_SQL + RELATED_SCHEMA_SQL


def tombstone_digest(item):
    if item.get('record_version') == 2:
        body={k:v for k,v in item.items() if k!='entry_hash'}
        return hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return hashlib.sha256(f"{item['prev_hash']}:{item['seq']}:{item['memory_id']}:{item['purged_content_sha256']}".encode()).hexdigest()


def norm_path_str(p: str | Path) -> str:
    return os.path.realpath(str(p)).replace("\\", "/").rstrip("/")


class _MemoryDatabaseBase:
    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        tombstone_path: Path = DEFAULT_TOMBSTONE_PATH,
        exports_dir: Path = DEFAULT_EXPORTS_DIR,
        backups_dir: Path = DEFAULT_BACKUPS_DIR,
    ):
        self.db_path = checked_path(db_path)
        self.tombstone_path = checked_path(tombstone_path)
        self.exports_dir = checked_path(exports_dir)
        self.backups_dir = checked_path(backups_dir)
        self._write_lock = threading.RLock()
        self._index_lock = threading.RLock()
        self._maintenance_lock = threading.Lock()
        self._new_database = not self.db_path.exists()
        if not self._new_database:
            with closing(sqlite3.connect(f'file:{self.db_path.as_posix()}?mode=ro', uri=True)) as existing:
                version = existing.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION:
                raise HubError(503, 'migration_required', '检测到旧 Schema；请显式迁移，服务不会自动改库。')
            if not self.tombstone_path.is_file():
                raise HubError(503, 'missing_or_corrupted_purge_evidence', '已有数据库缺少墓碑，拒绝启动。')

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tombstone_path.parent.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        self._init_schema_and_meta()
        self._init_tombstone_file_if_fresh()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA secure_delete=ON;")
        con.execute("PRAGMA busy_timeout=5000;")
        return con

    def _init_schema_and_meta(self) -> None:
        with self._write_lock:
            con = self.connect()
            try:
                con.executescript(SCHEMA_SQL)
                con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                for domain_id, name in [('personal','个人背景'),('communication','沟通协作'),('tools','工具环境'),('learning','学习与能力'),('projects','项目工作'),('organization','组织业务')]:
                    con.execute('INSERT OR IGNORE INTO domains(domain_id,name) VALUES(?,?)',(domain_id,name))
                con.execute("INSERT INTO memory_fts(memory_fts, rank) VALUES(?, ?)", ("secure-delete", 1))
                now = utc_now_iso()
                defaults = {
                    "active_index_generation": DEFAULT_GENERATION,
                    "maintenance_mode": "normal",
                    "staging_index_generation": None,
                    "max_purge_seq": 0,
                    "allowed_subjects": [
                        "user",
                        "project",
                        "shared",
                        "antigravity",
                        "codex",
                        "user:synthetic_test",
                    ],
                    "project_registry": {},
                }
                con.execute("BEGIN IMMEDIATE")
                for k, v in defaults.items():
                    row = con.execute("SELECT 1 FROM hub_meta WHERE meta_key = ?", (k,)).fetchone()
                    if not row:
                        con.execute(
                            "INSERT INTO hub_meta(meta_key, meta_value_json, updated_at) VALUES(?, ?, ?)",
                            (k, json.dumps(v, ensure_ascii=False), now),
                        )
                con.execute("COMMIT")
            finally:
                con.close()

    def _init_tombstone_file_if_fresh(self) -> None:
        max_seq = self.get_meta("max_purge_seq", 0)
        if not self.tombstone_path.exists() and self._new_database:
            genesis_hash = hashlib.sha256(b"shared_memory_hub_purge_genesis_v1").hexdigest()
            genesis = {
                "schema": "shared_memory_hub_purge_registry_v1",
                "seq": 0,
                "prev_hash": "0" * 64,
                "entry_hash": genesis_hash,
                "created_at": utc_now_iso(),
            }
            with open(self.tombstone_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(genesis, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def get_meta(self, key: str, default: Any = None) -> Any:
        con = self.connect()
        try:
            row = con.execute("SELECT meta_value_json FROM hub_meta WHERE meta_key = ?", (key,)).fetchone()
            return json.loads(row["meta_value_json"]) if row else default
        finally:
            con.close()

    def set_meta(self, key: str, value: Any, con: Optional[sqlite3.Connection] = None) -> None:
        now = utc_now_iso()
        own = con is None
        if own:
            con = self.connect()
            con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """
                INSERT INTO hub_meta(meta_key, meta_value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET
                    meta_value_json = excluded.meta_value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), now),
            )
            if own:
                con.execute("COMMIT")
        finally:
            if own:
                con.close()

    def register_project_binding(self, project_id: str, workspace_path: str | Path) -> Dict[str, Any]:
        norm_wp = norm_path_str(workspace_path)
        now = utc_now_iso()
        with self._write_lock:
            reg = self.get_meta("project_registry", {})
            reg[norm_wp] = project_id
            self.set_meta("project_registry", reg)
            con = self.connect()
            try:
                con.execute(
                    """
                    INSERT INTO project_bindings(project_id, workspace_path, created_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET workspace_path = excluded.workspace_path
                    """,
                    (project_id, norm_wp, now),
                )
            finally:
                con.close()
            return {"status": "ok", "project_id": project_id, "workspace_path": norm_wp}

    def register_project_workspace(self, workspace_path: str | Path, project_id: str) -> Dict[str, Any]:
        return self.register_project_binding(project_id=project_id, workspace_path=workspace_path)

    def resolve_bound_project(
        self,
        con_or_workspace: Any = None,
        workspace_header: Optional[str] = None,
        session_id_header: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Returns (bound_project_id, bound_workspace_path)."""
        if isinstance(con_or_workspace, (str, Path)) and workspace_header is None:
            workspace_header = str(con_or_workspace)

        reg: Dict[str, str] = self.get_meta("project_registry", {})
        candidate_paths: List[str] = []

        if session_id_header and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id_header):
            ctx_file = checked_path(DEFAULT_SESSIONS_DIR / session_id_header / "context.json")
            if ctx_file.is_file():
                try:
                    ctx_data = json.loads(ctx_file.read_text(encoding="utf-8"))
                    for wp in ctx_data.get("workspace_paths", []):
                        if wp:
                            candidate_paths.append(norm_path_str(wp))
                except Exception:
                    pass

        if workspace_header and str(workspace_header).strip():
            candidate_paths.append(norm_path_str(str(workspace_header).strip()))

        for cp in candidate_paths:
            if not os.path.isdir(cp):
                continue
            for reg_path, proj_id in sorted(reg.items(), key=lambda x: len(x[0]), reverse=True):
                if cp == reg_path or cp.startswith(reg_path + "/"):
                    return proj_id, reg_path
        return None, None

    def check_not_reindexing(self) -> None:
        if self.get_meta("maintenance_mode", "normal") == "reindexing":
            raise HubError(
                503,
                "reindex_in_progress",
                "索引世代正在全量重建写冻结中，请稍后重试（读请求不受影响）。",
                {"retry_after_seconds": 15},
            )

    def run_admission_guard_and_calibrate(
        self,
        actor_role: str,
        actor_id: str,
        payload: Dict[str, Any],
        bound_workspace_path: Optional[str],
    ) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
        """
        Validates structure, secrets, privilege escalation, and calibrates:
        (source_type, effective_confidence, enriched_source_dict, effective_expires_at).
        """
        title = (payload.get("title") or "").strip()
        content = (payload.get("content") or "").strip()
        topic_key = (payload.get("topic_key") or "general").strip()
        kind = payload.get("kind") or "fact"
        requested_conf = payload.get("confidence") or "confirmed"
        expires_at = payload.get("expires_at")
        source = dict(payload.get("source") or {})

        if not title or len(title) > 120:
            raise HubError(422, "invalid_title_length", "title 必须非空且长度 <= 120 字符。")
        if not content or len(content) > 4000:
            raise HubError(422, "invalid_content_length", "content 必须非空且长度 <= 4000 字符。")
        if not TOPIC_KEY_RE.match(topic_key):
            raise HubError(422, "invalid_topic_key", "topic_key 必须符合 ^[a-z0-9_.-]{3,80}$。")
        if kind not in ("preference", "decision", "fact", "context", "experience", "handoff", "rule_candidate"):
            raise HubError(422, "invalid_kind", f"不支持的 kind: {kind}")

        combined_text = f"{title}\n{content}\n{json.dumps(source, ensure_ascii=False)}"
        for pat in SECRET_PATTERNS:
            if pat.search(combined_text):
                raise HubError(422, "sensitive_credential_rejected", "检测到疑似密钥或明文凭据，准入守卫拒绝入库。")
        for pat in PRIVILEGE_ESCALATION_PATTERNS:
            if pat.search(combined_text):
                raise HubError(422, "policy_violation_rejected", "检测到越权或篡改审批边界指令词，准入守卫拒绝入库。")

        # Calibrate expires_at for handoff
        now_dt = datetime.now(timezone.utc)
        if expires_at is not None:
            try:
                expires_at = parse_iso(expires_at).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            except (ValueError, TypeError, AttributeError):
                raise HubError(422, 'invalid_expiry', '过期时间必须为 ISO 时间。')
        if kind == "handoff":
            max_dt = now_dt + timedelta(days=30)
            if not expires_at:
                expires_at = (now_dt + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            else:
                try:
                    exp_dt = parse_iso(expires_at)
                except (ValueError, TypeError, AttributeError):
                    raise HubError(422, 'invalid_expiry', '过期时间必须为 ISO 时间。')
                if exp_dt > max_dt:
                    expires_at = max_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if actor_role == "user_admin":
            source_type = source.get("origin_type") or "admin_manual"
            if source_type not in ("user_explicit", "project_fact", "verified_receipt", "model_inference", "task_state", "admin_manual"):
                source_type = "admin_manual"
            source["origin_type"] = source_type
            source["verification_status"] = "admin_confirmed"
            effective_conf = requested_conf if requested_conf in ("confirmed", "hypothesis") else "confirmed"
            return source_type, effective_conf, source, expires_at

        # agent_client source verification
        origin_type = source.get("origin_type") or "model_inference"
        if origin_type == "admin_manual":
            raise HubError(403, "forbidden_source_type", "agent_client 禁止伪造 admin_manual 来源。")
        if origin_type not in ("user_explicit", "project_fact", "verified_receipt", "model_inference", "task_state"):
            raise HubError(422, "invalid_source_type", f"未知的 source.origin_type: {origin_type}")

        effective_conf = "confirmed" if requested_conf == "confirmed" else "hypothesis"

        if origin_type == "model_inference":
            effective_conf = "hypothesis"
            source["verification_status"] = "model_inference_Forced_hypothesis"
            source["downgrade_reason"] = "model_inference_always_hypothesis"

        elif origin_type in ("project_fact", "verified_receipt"):
            ev_path_raw = (source.get("evidence_path") or source.get("evidence_ref") or "").strip()
            ev_sha256 = (source.get("evidence_sha256") or "").strip().lower()
            ev_snippet = (source.get("evidence_snippet") or "").strip()

            verified_ok = False
            if ev_path_raw and ".." not in Path(ev_path_raw).parts:
                cand_path = Path(ev_path_raw)
                if not cand_path.is_absolute():
                    if bound_workspace_path:
                        cand_path = Path(bound_workspace_path) / cand_path
                    else:
                        cand_path = HUB_ROOT / cand_path
                real_cand = norm_path_str(cand_path)
                allowed_roots = [norm_path_str(HUB_ROOT)]
                if bound_workspace_path:
                    allowed_roots.append(norm_path_str(bound_workspace_path))

                if (
                    any(real_cand == r or real_cand.startswith(r + "/") for r in allowed_roots)
                    and os.path.isfile(real_cand)
                    and os.path.getsize(real_cand) > 0
                ):
                    file_bytes = Path(real_cand).read_bytes()
                    actual_sha = hashlib.sha256(file_bytes).hexdigest().lower()
                    if (not ev_sha256 or actual_sha == ev_sha256) and (
                        not ev_snippet or ev_snippet.encode("utf-8") in file_bytes
                    ):
                        verified_ok = True
                        source["verified_file_sha256_16"] = actual_sha[:16]

            if verified_ok:
                source["verification_status"] = "disk_verified"
            else:
                effective_conf = "hypothesis"
                source["verification_status"] = "unverified_downgraded"
                source["downgrade_reason"] = "unverified_evidence_ref"

        elif origin_type == "user_explicit":
            session_id = (source.get("session_id") or "").strip()
            ev_ref = (source.get("evidence_ref") or "").strip()
            if len(session_id) < 2 or len(ev_ref) < 4:
                effective_conf = "hypothesis"
                source["verification_status"] = "unverified_downgraded"
                source["downgrade_reason"] = "missing_session_id_or_short_evidence_ref"
            else:
                source["verification_status"] = "unverified_excerpt"

        if kind == "experience" and origin_type not in ("verified_receipt", "user_explicit"):
            effective_conf = "hypothesis"
            source["downgrade_reason"] = "experience_requires_verified_receipt_or_user_explicit"

        return origin_type, effective_conf, source, expires_at

    def verify_and_reconcile_purge_registry(
        self,
        qdrant_delete_cb: Optional[Callable[[str], None]] = None,
        target_min_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Fail-Closed verification of data/tombstones/purge_registry.jsonl and reconciliation
        against SQLite and Qdrant.
        """
        with self._index_lock, self._write_lock:
            db_max_seq = int(self.get_meta("max_purge_seq", 0))
            required_seq = max(db_max_seq, int(target_min_seq or 0))

            if not self.tombstone_path.is_file():
                raise HubError(
                    503,
                    "missing_or_corrupted_purge_evidence",
                    f"外部墓碑文件缺失 ({self.tombstone_path})，拒绝上线或恢复（Fail-Closed）。",
                )

            try:
                lines = [ln.strip() for ln in self.tombstone_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            except Exception as e:
                raise HubError(503, "missing_or_corrupted_purge_evidence", f"读取外部墓碑文件失败: {e}")

            if not lines:
                raise HubError(503, "missing_or_corrupted_purge_evidence", "外部墓碑文件为空，缺失创世头记录。")

            try:
                genesis = json.loads(lines[0])
            except Exception as e:
                raise HubError(503, "missing_or_corrupted_purge_evidence", f"创世墓碑 JSON 损坏: {e}")

            expected_genesis_hash = hashlib.sha256(b"shared_memory_hub_purge_genesis_v1").hexdigest()
            if genesis.get("seq") != 0 or genesis.get("entry_hash") != expected_genesis_hash:
                raise HubError(503, "missing_or_corrupted_purge_evidence", "创世墓碑哈希校验失败。")

            prev_hash = expected_genesis_hash
            file_max_seq = 0
            entries: List[Dict[str, Any]] = []

            for idx, raw_line in enumerate(lines[1:], start=1):
                try:
                    item = json.loads(raw_line)
                except Exception as e:
                    raise HubError(503, "missing_or_corrupted_purge_evidence", f"墓碑第 {idx} 行 JSON 解析失败: {e}")

                seq = item.get("seq")
                mem_id = item.get("memory_id")
                c_sha = item.get("purged_content_sha256")
                e_hash = item.get("entry_hash")
                p_hash = item.get("prev_hash")
                if seq != idx or p_hash != prev_hash or not mem_id or not c_sha:
                    raise HubError(503, "missing_or_corrupted_purge_evidence", f"墓碑哈希链在第 {idx} 行断裂或篡改。")

                calc_hash = tombstone_digest(item)
                if e_hash != calc_hash:
                    raise HubError(503, "missing_or_corrupted_purge_evidence", f"墓碑第 {idx} 行 entry_hash 不匹配。")

                prev_hash = calc_hash
                file_max_seq = seq
                entries.append(item)

            if file_max_seq < required_seq:
                raise HubError(
                    503,
                    "missing_or_corrupted_purge_evidence",
                    f"外部墓碑序号 ({file_max_seq}) 低于所需水位 ({required_seq})，拒绝上线。",
                )

            reconciled_ids = []
            for item in entries:
                self._apply_tombstone(item)
                self._complete_purge(item["memory_id"], qdrant_delete_cb)
                reconciled_ids.append(item["memory_id"])
            return {"verified":True,"file_max_seq":file_max_seq,"reconciled_memory_ids":reconciled_ids}

    def startup_recover_reindex_and_worker(
        self,
        qdrant_drop_collection_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """
        Recovers from interrupted maintenance_mode=='reindexing' and stuck processing tasks,
        and ensures all active lagging rows have pending index_work tasks.
        """
        with self._index_lock, self._write_lock:
            mode = self.get_meta("maintenance_mode", "normal")
            staging_gen = self.get_meta("staging_index_generation", None)
            active_gen = self.get_meta("active_index_generation", DEFAULT_GENERATION)
            recovered_reindex = False

            cleanup_pending = None
            if mode == "reindexing" or staging_gen:
                recovered_reindex = True
                cleanup_done = not staging_gen or staging_gen == active_gen
                if staging_gen and staging_gen != active_gen:
                    if qdrant_drop_collection_cb:
                        try:
                            cleanup_done = qdrant_drop_collection_cb(staging_gen) is True
                        except Exception:
                            pass
                    if not cleanup_done:
                        cleanup_pending = staging_gen
                self.set_meta("maintenance_mode", "normal")
                if cleanup_done:
                    self.set_meta("staging_index_generation", None)

            con = self.connect()
            try:
                now = utc_now_iso()
                con.execute("BEGIN IMMEDIATE")
                cur_proc = con.execute(
                    """
                    UPDATE index_work
                    SET status = 'pending', claimed_at = NULL, last_error = 'recovered_after_restart', updated_at = ?
                    WHERE status = 'processing'
                    """,
                    (now,),
                )
                reset_processing_count = cur_proc.rowcount

                lagging_rows = con.execute(
                    """
                    SELECT memory_id, revision
                    FROM current_memory
                    WHERE state = 'active'
                      AND (indexed_revision < revision OR index_generation != ?)
                    """,
                    (active_gen,),
                ).fetchall()

                enqueued_missing = 0
                for r in lagging_rows:
                    has_pend = con.execute(
                        """
                        SELECT 1 FROM index_work
                        WHERE memory_id = ? AND target_revision = ? AND generation = ? AND status = 'pending'
                        """,
                        (r["memory_id"], r["revision"], active_gen),
                    ).fetchone()
                    if not has_pend:
                        con.execute(
                            """
                            INSERT INTO index_work(memory_id, target_revision, generation, action, status, retry_count, created_at, updated_at)
                            VALUES(?, ?, ?, 'upsert', 'pending', 0, ?, ?)
                            """,
                            (r["memory_id"], r["revision"], active_gen, now, now),
                        )
                        enqueued_missing += 1
                con.execute("COMMIT")
            finally:
                con.close()

            return {
                "recovered_reindex": recovered_reindex,
                "reset_processing_count": reset_processing_count,
                "enqueued_missing_count": enqueued_missing,
                "active_index_generation": active_gen,
                "staging_cleanup_pending": cleanup_pending,
            }


from .admin_actions import AdminActionsMixin  # noqa: E402
from .admin_ops import AdminOperationsMixin  # noqa: E402
from .operations import MemoryOperationsMixin  # noqa: E402
from .purge_backup import PurgeAndBackupMixin  # noqa: E402
from .search_reindex import SearchReindexMixin  # noqa: E402
from .workbench import WorkbenchMixin  # noqa: E402


class MemoryDatabase(
    RelatedMemoryMixin,
    DraftsMixin,
    WorkbenchMixin,
    _MemoryDatabaseBase,
    MemoryOperationsMixin,
    AdminOperationsMixin,
    AdminActionsMixin,
    PurgeAndBackupMixin,
    SearchReindexMixin,
):
    """Unified Authoritative SQLite Memory Store for SharedMemoryHub (v0.3.0)."""

