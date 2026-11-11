"""Memory write, conflict proposal, undo/restore, lock/pause, and purge operations (v0.3.0)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .db import DEFAULT_GENERATION, HubError, utc_now_iso


class MemoryOperationsMixin:
    def _has_admin_revision(self, con: sqlite3.Connection, memory_id: str) -> bool:
        row = con.execute(
            "SELECT 1 FROM revisions WHERE memory_id = ? AND actor_role = 'user_admin' LIMIT 1",
            (memory_id,),
        ).fetchone()
        return row is not None

    def _row_to_metadata_snapshot(self, row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
        return {
            "subject": row["subject"],
            "scope": row["scope"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "topic_key": row["topic_key"],
            "confidence": row["confidence"],
            "readers": json.loads(row["readers_json"]),
            "host_constraints": json.loads(row["host_constraints_json"]),
            "expires_at": row["expires_at"],
            "created_by_actor_id": row["created_by_actor_id"],
            "created_in_session_id": row["created_in_session_id"],
            "has_open_conflict": bool(row["has_open_conflict"]),
            "source_type": row["source_type"],
            "source": json.loads(row["source_json"]),
            "domain_id": row["domain_id"],
            "tags": json.loads(row["tags_json"]),
            "review_due_at": row["review_due_at"],
            "last_reviewed_at": row["last_reviewed_at"],
        }

    def _revision_metadata(self, con, row):
        snapshot=self._row_to_metadata_snapshot(row)
        snapshot['relations']=[dict(r) for r in con.execute('SELECT * FROM memory_relations WHERE memory_id=?',(row['memory_id'],))]
        snapshot['source_links']=[dict(r) for r in con.execute('SELECT * FROM source_links WHERE memory_id=?',(row['memory_id'],))]
        return snapshot

    def _check_row_authorized(
        self,
        row: sqlite3.Row,
        actor_role: str,
        actor_id: str,
        host_id: str,
        bound_project_id: Optional[str],
        include_non_active: bool = False,
    ) -> bool:
        allowed_subjects = self.get_meta(
            "allowed_subjects",
            ["user", "project", "shared", "antigravity", "codex", "user:synthetic_test"],
        )
        subj = row["subject"]
        if subj not in allowed_subjects:
            return False

        if not include_non_active:
            if row['state'] != 'active' or row['confidence'] != 'confirmed':
                return False
            if row['expires_at'] and row['expires_at'] <= utc_now_iso():
                return False

        if actor_role == "user_admin":
            return True

        if subj == "antigravity" and actor_id != "antigravity":
            return False
        if subj == "codex" and actor_id != "codex":
            return False

        if not include_non_active:
            if row["state"] != "active" or row["confidence"] != "confirmed":
                return False
            if row["expires_at"] and row["expires_at"] <= utc_now_iso():
                return False

        scope = row["scope"]
        if scope == "project":
            if not bound_project_id or row["project_id"] != bound_project_id:
                return False
        elif scope == "agent_private":
            if row["created_by_actor_id"] != actor_id:
                return False
            if bound_project_id and row["project_id"] and row["project_id"] != bound_project_id:
                return False
        elif scope not in ("global", "user_global"):
            return False

        readers = json.loads(row["readers_json"])
        if actor_id not in readers and "any" not in readers:
            return False

        hosts = json.loads(row["host_constraints_json"])
        if "any" not in hosts and host_id not in hosts:
            return False

        return True

    def format_memory_item(
        self,
        row: sqlite3.Row,
        score: Optional[float] = None,
        con: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        rev = int(row["revision"])
        idx_rev = int(row["indexed_revision"])
        if row["state"] != "active":
            idx_status = "inactive"
        elif idx_rev >= rev:
            idx_status = "synced"
        elif idx_rev == 0:
            idx_status = "pending"
        else:
            idx_status = "lagging"

        conflict_notice = None
        if int(row["has_open_conflict"]) == 1:
            own = con is None
            if own:
                con = self.connect()
            try:
                props = con.execute(
                    """
                    SELECT proposal_id, proposal_type, proposer_id, proposed_title, proposed_content, created_at
                    FROM conflict_proposals
                    WHERE memory_id = ? AND status = 'pending'
                    ORDER BY created_at DESC
                    """,
                    (row["memory_id"],),
                ).fetchall()
                conflict_notice = {
                    "has_open_conflict": True,
                    "open_proposals": [dict(p) for p in props],
                }
            finally:
                if own:
                    con.close()

        return {
            "memory_id": row["memory_id"],
            "revision": rev,
            "indexed_revision": idx_rev,
            "index_generation": row["index_generation"],
            "index_status": idx_status,
            "state": row["state"],
            "locked": bool(row["locked"]),
            "has_open_conflict": bool(row["has_open_conflict"]),
            "conflict_notice": conflict_notice,
            "subject": row["subject"],
            "scope": row["scope"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "topic_key": row["topic_key"],
            "title": row["title"],
            "content": row["content"],
            "confidence": row["confidence"],
            "readers": json.loads(row["readers_json"]),
            "host_constraints": json.loads(row["host_constraints_json"]),
            "source_type": row["source_type"],
            "source": json.loads(row["source_json"]),
            "created_by_actor_id": row["created_by_actor_id"],
            "created_in_session_id": row["created_in_session_id"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "score": score,
            "domain_id": row["domain_id"],
            "tags": json.loads(row["tags_json"]),
            "review_due_at": row["review_due_at"],
            "last_reviewed_at": row["last_reviewed_at"],
            "authority_notice": "HISTORICAL_MEMORY_ONLY: 优先级低于安全规则、真实运行证据、当前用户指令与项目现行代码/配置",
        }

    def write_memory(
        self,
        actor_role: str,
        actor_id: str,
        host_id: str,
        payload: Dict[str, Any],
        bound_project_id: Optional[str] = None,
        bound_workspace_path: Optional[str] = None,
        _con=None,
    ) -> Dict[str, Any]:
        with self._write_lock:
            self.check_not_reindexing()
            incoming_session_id = (payload.get('source') or {}).get('session_id')

            if bound_project_id is None and bound_workspace_path is None:
                ws_raw = payload.get("workspace_path")
                sess_raw = (payload.get("source") or {}).get("session_id")
                bound_project_id, bound_workspace_path = self.resolve_bound_project(
                    workspace_header=ws_raw,
                    session_id_header=sess_raw,
                )

            req_key = (payload.get("request_key") or payload.get("idempotency_key") or "").strip()
            if not req_key:
                raise HubError(422, "missing_idempotency_key", "request_key / idempotency_key 不能为空。")

            canonical_body = json.dumps(
                {k: v for k, v in sorted(payload.items()) if k not in ("request_key", "idempotency_key")},
                ensure_ascii=False,
                sort_keys=True,
            )
            body_hash = "sha256:" + hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()

            con = _con if _con is not None else self.connect()
            try:
                existing_req = con.execute(
                    "SELECT * FROM requests WHERE request_key = ?",
                    (req_key,),
                ).fetchone()
                if existing_req:
                    if existing_req['actor_id'] != actor_id:
                        raise HubError(403, 'idempotency_owner_mismatch', '该幂等键属于其他客户端。')
                    if int(existing_req["is_purged"]) == 1 or int(existing_req["status_code"]) == 410:
                        raise HubError(
                            410,
                            "MemoryPurged",
                            "该幂等键关联的记忆已被永久清除（Purge），严禁旧请求重放重建。",
                        )
                    if existing_req["body_hash"] != body_hash:
                        raise HubError(
                            422,
                            "IdempotencyKeyPayloadMismatch",
                            "相同 request_key / idempotency_key 携带了不同的请求体内容。",
                        )
                    mem_row = con.execute(
                        "SELECT * FROM current_memory WHERE memory_id = ?",
                        (existing_req["memory_id"],),
                    ).fetchone()
                    active_gen = self.get_meta("active_index_generation", DEFAULT_GENERATION)
                    if not mem_row:
                        raise HubError(410, "MemoryPurged", "该记忆已被永久清除。")
                    if not self._check_row_authorized(mem_row, actor_role, actor_id, host_id, bound_project_id, include_non_active=True):
                        raise HubError(403, 'forbidden_memory_access', '当前工作区无权重放该请求。')
                    idx_st = "synced" if int(mem_row["indexed_revision"]) >= int(mem_row["revision"]) else "pending"
                    return {
                        "status": "committed",
                        "memory_id": mem_row["memory_id"],
                        "stored_revision": int(existing_req["applied_revision"]),
                        "current_state": mem_row["state"],
                        "effective_confidence": mem_row["confidence"],
                        "locked": bool(mem_row["locked"]),
                        "has_open_conflict": bool(mem_row["has_open_conflict"]),
                        "proposal_id": None,
                        "index_status": idx_st,
                        "index_generation": active_gen,
                        "idempotent_replay": True,
                    }

                op = payload.get("operation") or "create"
                mem_id = payload.get("memory_id")
                if op != 'create' and mem_id:
                    old = con.execute('SELECT * FROM current_memory WHERE memory_id=?', (mem_id,)).fetchone()
                    if old:
                        merged = {**self._row_to_metadata_snapshot(old), 'title':old['title'], 'content':old['content']}
                        merged.update(payload)
                        if 'source' in payload:
                            merged['source'] = {**json.loads(old['source_json']), **(payload['source'] or {})}
                        payload = merged
                if mem_id:
                    tomb = con.execute("SELECT 1 FROM purge_tombstones WHERE memory_id = ?", (mem_id,)).fetchone()
                    if tomb:
                        raise HubError(410, "MemoryPurged", "目标 memory_id 已被永久清除（Purge），拒绝写入。")

                subject = (payload.get("subject") or "project").strip()
                allowed_subjects = self.get_meta(
                    "allowed_subjects",
                    ["user", "project", "shared", "antigravity", "codex", "user:synthetic_test"],
                )
                if subject not in allowed_subjects:
                    raise HubError(403, "forbidden_subject", f"未授权的 subject: {subject}")

                scope = payload.get("scope") or "project"
                if scope not in ('global','user_global','project','agent_private'):
                    raise HubError(422, 'invalid_scope', '无效的适用范围。')
                kind = payload.get("kind") or "fact"
                req_proj_id = payload.get("project_id")
                if actor_role != "user_admin":
                    if scope == "project":
                        if not bound_project_id:
                            raise HubError(403, "ProjectScopeMismatch", "当前无已绑定工作区，禁止读写 scope='project' 记忆。")
                        if req_proj_id and req_proj_id != bound_project_id:
                            raise HubError(
                                403,
                                "ProjectScopeMismatch",
                                f"请求体 project_id ({req_proj_id}) 与可信工作区绑定 ({bound_project_id}) 不一致。",
                            )
                        req_proj_id = bound_project_id
                    elif scope == "agent_private":
                        if req_proj_id and bound_project_id and req_proj_id != bound_project_id:
                            raise HubError(403, "ProjectScopeMismatch", "agent_private 的 project_id 与当前工作区冲突。")
                        req_proj_id = bound_project_id
                else:
                    if scope == "project" and not req_proj_id:
                        req_proj_id = bound_project_id

                active_gen = self.get_meta("active_index_generation", DEFAULT_GENERATION)
                now = utc_now_iso()

                if op == "create":
                    exp_rev = payload.get("expected_revision", 0)
                    if exp_rev is not None and int(exp_rev) != 0:
                        raise HubError(409, "RevisionConflict", "create 操作的 expected_revision 必须为 0。")

                    source_type, eff_conf, source_dict, eff_expires = self.run_admission_guard_and_calibrate(
                        actor_role, actor_id, payload, bound_workspace_path
                    )
                    title = payload["title"].strip()
                    content = payload["content"].strip()
                    topic_key = (payload.get("topic_key") or "general").strip()
                    readers = payload.get("readers") or ["user", "antigravity", "codex"]
                    host_constraints = payload.get("host_constraints") or ["any"]
                    for values in (readers, host_constraints):
                        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                            raise HubError(422, 'invalid_access_metadata', '读者及宿主约束必须为字符串数组。')
                    session_id = source_dict.get("session_id")
                    new_mem_id = f"mem_{uuid.uuid4().hex[:24]}"

                    needs_approval = (
                        actor_role == "agent_client"
                        and (scope in ("global", "user_global") or kind == "rule_candidate")
                    )
                    init_state = "paused" if needs_approval or kind == 'rule_candidate' else "active"
                    init_locked = 1 if actor_role == "user_admin" else 0
                    init_conflict = 1 if needs_approval else 0
                    proposal_id = None

                    meta_snap = {
                        "subject": subject,
                        "scope": scope,
                        "project_id": req_proj_id,
                        "kind": kind,
                        "topic_key": topic_key,
                        "confidence": eff_conf,
                        "readers": readers,
                        "host_constraints": host_constraints,
                        "expires_at": eff_expires,
                        "created_by_actor_id": actor_id,
                        "created_in_session_id": session_id,
                        "has_open_conflict": bool(init_conflict),
                        "source_type": source_type,
                        "source": source_dict,
                    }
                    organization = self.validate_organization(payload)
                    meta_snap.update(organization)
                    meta_snap.update(relations=[],source_links=[],last_reviewed_at=None)

                    if _con is None:con.execute("BEGIN IMMEDIATE")
                    con.execute(
                        """
                        INSERT INTO current_memory(
                            memory_id, revision, indexed_revision, index_generation,
                            state, locked, has_open_conflict, subject, scope, project_id,
                            kind, topic_key, title, content, confidence,
                            readers_json, host_constraints_json, source_type, source_json,
                            created_by_actor_id, created_in_session_id, expires_at, created_at, updated_at
                        ) VALUES(?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_mem_id,
                            active_gen,
                            init_state,
                            init_locked,
                            init_conflict,
                            subject,
                            scope,
                            req_proj_id,
                            kind,
                            topic_key,
                            title,
                            content,
                            eff_conf,
                            json.dumps(readers, ensure_ascii=False),
                            json.dumps(host_constraints, ensure_ascii=False),
                            source_type,
                            json.dumps(source_dict, ensure_ascii=False),
                            actor_id,
                            session_id,
                            eff_expires,
                            now,
                            now,
                        ),
                    )
                    con.execute('UPDATE current_memory SET domain_id=?,tags_json=?,review_due_at=? WHERE memory_id=?',
                        (organization['domain_id'],json.dumps(organization['tags'],ensure_ascii=False),organization['review_due_at'],new_mem_id))
                    self._link_source(con,new_mem_id,source_dict)
                    if init_state == "active":
                        con.execute(
                            "INSERT INTO memory_fts(memory_id, title, content, topic_key) VALUES(?, ?, ?, ?)",
                            (new_mem_id, title, content, topic_key),
                        )
                        con.execute(
                            """
                            INSERT INTO index_work(memory_id, target_revision, generation, action, status, retry_count, created_at, updated_at)
                            VALUES(?, 1, ?, 'upsert', 'pending', 0, ?, ?)
                            """,
                            (new_mem_id, active_gen, now, now),
                        )
                    else:
                        proposal_id = f"prop_{uuid.uuid4().hex[:20]}"
                        prop_type = "rule_candidate" if kind == "rule_candidate" else "scope_expansion"
                        con.execute(
                            """
                            INSERT INTO conflict_proposals(
                                proposal_id, memory_id, base_revision, proposal_type,
                                proposed_title, proposed_content, proposed_metadata_json,
                                proposer_role, proposer_id, source_json, status, created_at
                            ) VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                            """,
                            (
                                proposal_id,
                                new_mem_id,
                                prop_type,
                                title,
                                content,
                                json.dumps(meta_snap, ensure_ascii=False),
                                actor_role,
                                actor_id,
                                json.dumps(source_dict, ensure_ascii=False),
                                now,
                            ),
                        )

                    con.execute(
                        """
                        INSERT INTO revisions(
                            memory_id, revision, parent_revision, operation, state, locked,
                            title, content, metadata_snapshot_json, actor_role, actor_id,
                            source_type, source_json, restored_from, created_at
                        ) VALUES(?, 1, 0, 'create', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            new_mem_id,
                            init_state,
                            init_locked,
                            title,
                            content,
                            json.dumps(meta_snap, ensure_ascii=False),
                            actor_role,
                            actor_id,
                            source_type,
                            json.dumps(source_dict, ensure_ascii=False),
                            now,
                        ),
                    )
                    con.execute(
                        """
                        INSERT INTO requests(request_key, actor_id, body_hash, memory_id, applied_revision, status_code, is_purged, created_at)
                        VALUES(?, ?, ?, ?, 1, 200, 0, ?)
                        """,
                        (req_key, actor_id, body_hash, new_mem_id, now),
                    )
                    con.execute(
                        """
                        INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                        VALUES('write', ?, 1, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_mem_id,
                            actor_role,
                            actor_id,
                            scope,
                            req_proj_id,
                            "paused_pending_approval" if needs_approval else "committed",
                            json.dumps({"operation": "create", "confidence": eff_conf}, ensure_ascii=False),
                            now,
                        ),
                    )
                    if _con is None:con.execute("COMMIT")

                    return {
                        "status": "paused_pending_approval" if needs_approval else "committed",
                        "memory_id": new_mem_id,
                        "stored_revision": 1,
                        "current_state": init_state,
                        "effective_confidence": eff_conf,
                        "verification_status": source_dict.get("verification_status"),
                        "locked": bool(init_locked),
                        "has_open_conflict": bool(init_conflict),
                        "proposal_id": proposal_id,
                        "index_status": "inactive" if needs_approval else "pending",
                        "index_generation": active_gen,
                        "idempotent_replay": False,
                    }

                return self._write_existing_memory(
                    con, op, mem_id, actor_role, actor_id, host_id, payload,
                    bound_project_id, bound_workspace_path, req_key, body_hash, active_gen, now
                    , incoming_session_id=incoming_session_id
                )
            finally:
                if _con is None:con.close()
