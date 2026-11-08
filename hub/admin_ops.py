"""Existing memory mutations, Undo/Restore, Lock/Pause, Conflict Resolution & Purge (v0.3.0)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .db import DEFAULT_GENERATION, HubError, utc_now_iso


class AdminOperationsMixin:
    def _write_existing_memory(
        self,
        con: sqlite3.Connection,
        op: str,
        mem_id: Optional[str],
        actor_role: str,
        actor_id: str,
        host_id: str,
        payload: Dict[str, Any],
        bound_project_id: Optional[str],
        bound_workspace_path: Optional[str],
        req_key: str,
        body_hash: str,
        active_gen: str,
        now: str,
        incoming_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not mem_id:
            raise HubError(422, "missing_memory_id", f"{op} 操作必须提供 memory_id。")
        existing = con.execute("SELECT * FROM current_memory WHERE memory_id = ?", (mem_id,)).fetchone()
        if not existing:
            raise HubError(404, "memory_not_found", f"未找到记忆: {mem_id}")

        if not self._check_row_authorized(existing, actor_role, actor_id, host_id, bound_project_id, include_non_active=True):
            raise HubError(403, "forbidden_memory_access", "无权访问或修改该范围的记忆。")

        curr_rev = int(existing["revision"])
        if actor_role == 'user_admin' and op == 'update':
            return self._admin_commit(existing, payload, payload.get('expected_revision'), 'update', actor_id,
                request_info=(req_key, body_hash))

        if op == "propose_conflict":
            source_type, eff_conf, source_dict, eff_expires = self.run_admission_guard_and_calibrate(
                actor_role, actor_id, payload, bound_workspace_path
            )
            proposal_id = f"prop_{uuid.uuid4().hex[:20]}"
            meta_snap = self._row_to_metadata_snapshot(existing)
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE current_memory SET has_open_conflict = 1, updated_at = ? WHERE memory_id = ?",
                (now, mem_id),
            )
            con.execute(
                """
                INSERT INTO conflict_proposals(
                    proposal_id, memory_id, base_revision, proposal_type,
                    proposed_title, proposed_content, proposed_metadata_json,
                    proposer_role, proposer_id, source_json, status, created_at
                ) VALUES(?, ?, ?, 'credible_fact_conflict', ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    proposal_id,
                    mem_id,
                    curr_rev,
                    payload["title"].strip(),
                    payload["content"].strip(),
                    json.dumps(meta_snap, ensure_ascii=False),
                    actor_role,
                    actor_id,
                    json.dumps(source_dict, ensure_ascii=False),
                    now,
                ),
            )
            con.execute(
                """
                INSERT INTO requests(request_key, actor_id, body_hash, memory_id, applied_revision, status_code, is_purged, created_at)
                VALUES(?, ?, ?, ?, ?, 200, 0, ?)
                """,
                (req_key, actor_id, body_hash, mem_id, curr_rev, now),
            )
            con.execute(
                """
                INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                VALUES('conflict_hold', ?, ?, ?, ?, ?, ?, 'conflict_proposed', ?, ?)
                """,
                (mem_id, curr_rev, actor_role, actor_id, existing["scope"], existing["project_id"], json.dumps({"proposal_id": proposal_id}), now),
            )
            con.execute("COMMIT")
            return {
                "status": "conflict_proposed",
                "memory_id": mem_id,
                "stored_revision": curr_rev,
                "current_state": existing["state"],
                "effective_confidence": existing["confidence"],
                "locked": bool(existing["locked"]),
                "has_open_conflict": True,
                "proposal_id": proposal_id,
                "index_status": "synced" if int(existing["indexed_revision"]) >= curr_rev else "pending",
                "index_generation": active_gen,
                "idempotent_replay": False,
            }

        if actor_role == "agent_client" and int(existing["locked"]) == 1:
            raise HubError(409, "LockedConflict", "该记忆已被人工锁定（locked=1），AI 禁止修改或删除；原记录保持有效。")

        exp_rev = payload.get("expected_revision")
        if exp_rev is None or int(exp_rev) != curr_rev:
            raise HubError(
                409,
                "RevisionConflict",
                f"版本冲突：expected_revision={exp_rev}，实际最新 revision={curr_rev}。",
            )

        # Inherited source metadata must never grant same-session write authority.
        req_session_id = incoming_session_id
        is_same_session_self_fix = (
            existing["created_by_actor_id"] == actor_id
            and bool(existing["created_in_session_id"])
            and existing["created_in_session_id"] == req_session_id
            and not self._has_admin_revision(con, mem_id)
        )
        allow_direct_agent_modify = (
            actor_role == "user_admin"
            or existing["kind"] == "handoff"
            or is_same_session_self_fix
        )

        if op == "update":
            source_type, eff_conf, source_dict, eff_expires = self.run_admission_guard_and_calibrate(
                actor_role, actor_id, payload, bound_workspace_path
            )
            new_title = payload["title"].strip()
            new_content = payload["content"].strip()
            new_topic = (payload.get("topic_key") or existing["topic_key"]).strip()
            meta_snap = self._row_to_metadata_snapshot(existing)
            meta_snap["topic_key"] = new_topic
            meta_snap["confidence"] = eff_conf
            meta_snap["expires_at"] = eff_expires
            meta_snap['source_type'] = source_type
            meta_snap['source'] = source_dict
            for field in ('subject','scope','project_id','kind','readers','host_constraints'):
                meta_snap[field] = payload[field]
            if actor_role == 'agent_client' and any(meta_snap[k] != self._row_to_metadata_snapshot(existing)[k]
                for k in ('subject','scope','project_id','kind','readers','host_constraints')):
                allow_direct_agent_modify = False

            if not allow_direct_agent_modify:
                proposal_id = f"prop_{uuid.uuid4().hex[:20]}"
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "UPDATE current_memory SET has_open_conflict = 1, updated_at = ? WHERE memory_id = ?",
                    (now, mem_id),
                )
                con.execute(
                    """
                    INSERT INTO conflict_proposals(
                        proposal_id, memory_id, base_revision, proposal_type,
                        proposed_title, proposed_content, proposed_metadata_json,
                        proposer_role, proposer_id, source_json, status, created_at
                    ) VALUES(?, ?, ?, 'update_proposal', ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        proposal_id,
                        mem_id,
                        curr_rev,
                        new_title,
                        new_content,
                        json.dumps(meta_snap, ensure_ascii=False),
                        actor_role,
                        actor_id,
                        json.dumps(source_dict, ensure_ascii=False),
                        now,
                    ),
                )
                con.execute(
                    """
                    INSERT INTO requests(request_key, actor_id, body_hash, memory_id, applied_revision, status_code, is_purged, created_at)
                    VALUES(?, ?, ?, ?, ?, 200, 0, ?)
                    """,
                    (req_key, actor_id, body_hash, mem_id, curr_rev, now),
                )
                con.execute("COMMIT")
                return {
                    "status": "queued_as_proposal",
                    "memory_id": mem_id,
                    "stored_revision": curr_rev,
                    "current_state": existing["state"],
                    "effective_confidence": existing["confidence"],
                    "locked": bool(existing["locked"]),
                    "has_open_conflict": True,
                    "proposal_id": proposal_id,
                    "index_status": "synced" if int(existing["indexed_revision"]) >= curr_rev else "pending",
                    "index_generation": active_gen,
                    "idempotent_replay": False,
                }

            new_rev = curr_rev + 1
            new_locked = 1 if actor_role == "user_admin" else int(existing["locked"])
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                UPDATE current_memory
                SET revision = ?, locked = ?, topic_key = ?, title = ?, content = ?,
                    confidence = ?, source_type = ?, source_json = ?, expires_at = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (
                    new_rev,
                    new_locked,
                    new_topic,
                    new_title,
                    new_content,
                    eff_conf,
                    source_type,
                    json.dumps(source_dict, ensure_ascii=False),
                    eff_expires,
                    now,
                    mem_id,
                ),
            )
            con.execute("DELETE FROM memory_fts WHERE memory_id = ?", (mem_id,))
            if existing["state"] == "active":
                con.execute(
                    "INSERT INTO memory_fts(memory_id, title, content, topic_key) VALUES(?, ?, ?, ?)",
                    (mem_id, new_title, new_content, new_topic),
                )
                con.execute(
                    """
                    INSERT INTO index_work(memory_id, target_revision, generation, action, status, retry_count, created_at, updated_at)
                    VALUES(?, ?, ?, 'upsert', 'pending', 0, ?, ?)
                    """,
                    (mem_id, new_rev, active_gen, now, now),
                )
            con.execute(
                """
                INSERT INTO revisions(
                    memory_id, revision, parent_revision, operation, state, locked,
                    title, content, metadata_snapshot_json, actor_role, actor_id,
                    source_type, source_json, restored_from, created_at
                ) VALUES(?, ?, ?, 'update', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    mem_id,
                    new_rev,
                    curr_rev,
                    existing["state"],
                    new_locked,
                    new_title,
                    new_content,
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
                VALUES(?, ?, ?, ?, ?, 200, 0, ?)
                """,
                (req_key, actor_id, body_hash, mem_id, new_rev, now),
            )
            con.execute(
                """
                INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                VALUES('write', ?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                """,
                (mem_id, new_rev, actor_role, actor_id, existing["scope"], existing["project_id"], json.dumps({"operation": "update"}), now),
            )
            con.execute("COMMIT")
            return {
                "status": "committed",
                "memory_id": mem_id,
                "stored_revision": new_rev,
                "current_state": existing["state"],
                "effective_confidence": eff_conf,
                "locked": bool(new_locked),
                "has_open_conflict": bool(existing["has_open_conflict"]),
                "proposal_id": None,
                "index_status": "pending" if existing["state"] == "active" else "inactive",
                "index_generation": active_gen,
                "idempotent_replay": False,
            }

        if op == "soft_delete":
            if not allow_direct_agent_modify:
                proposal_id = f"prop_{uuid.uuid4().hex[:20]}"
                meta_snap = self._row_to_metadata_snapshot(existing)
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "UPDATE current_memory SET has_open_conflict = 1, updated_at = ? WHERE memory_id = ?",
                    (now, mem_id),
                )
                con.execute(
                    """
                    INSERT INTO conflict_proposals(
                        proposal_id, memory_id, base_revision, proposal_type,
                        proposed_title, proposed_content, proposed_metadata_json,
                        proposer_role, proposer_id, source_json, status, created_at
                    ) VALUES(?, ?, ?, 'delete_proposal', ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        proposal_id,
                        mem_id,
                        curr_rev,
                        existing["title"],
                        existing["content"],
                        json.dumps(meta_snap, ensure_ascii=False),
                        actor_role,
                        actor_id,
                        json.dumps(payload.get("source") or {}, ensure_ascii=False),
                        now,
                    ),
                )
                con.execute(
                    """
                    INSERT INTO requests(request_key, actor_id, body_hash, memory_id, applied_revision, status_code, is_purged, created_at)
                    VALUES(?, ?, ?, ?, ?, 200, 0, ?)
                    """,
                    (req_key, actor_id, body_hash, mem_id, curr_rev, now),
                )
                con.execute("COMMIT")
                return {
                    "status": "queued_as_proposal",
                    "memory_id": mem_id,
                    "stored_revision": curr_rev,
                    "current_state": existing["state"],
                    "effective_confidence": existing["confidence"],
                    "locked": bool(existing["locked"]),
                    "has_open_conflict": True,
                    "proposal_id": proposal_id,
                    "index_status": "synced" if int(existing["indexed_revision"]) >= curr_rev else "pending",
                    "index_generation": active_gen,
                    "idempotent_replay": False,
                }

            new_rev = curr_rev + 1
            meta_snap = self._row_to_metadata_snapshot(existing)
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE current_memory SET revision = ?, state = 'deleted', updated_at = ? WHERE memory_id = ?",
                (new_rev, now, mem_id),
            )
            con.execute("DELETE FROM memory_fts WHERE memory_id = ?", (mem_id,))
            con.execute(
                """
                INSERT INTO index_work(memory_id, target_revision, generation, action, status, retry_count, created_at, updated_at)
                VALUES(?, ?, ?, 'delete', 'pending', 0, ?, ?)
                """,
                (mem_id, new_rev, active_gen, now, now),
            )
            con.execute(
                """
                INSERT INTO revisions(
                    memory_id, revision, parent_revision, operation, state, locked,
                    title, content, metadata_snapshot_json, actor_role, actor_id,
                    source_type, source_json, restored_from, created_at
                ) VALUES(?, ?, ?, 'soft_delete', 'deleted', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    mem_id,
                    new_rev,
                    curr_rev,
                    existing["locked"],
                    existing["title"],
                    existing["content"],
                    json.dumps(meta_snap, ensure_ascii=False),
                    actor_role,
                    actor_id,
                    existing["source_type"],
                    existing["source_json"],
                    now,
                ),
            )
            con.execute(
                """
                INSERT INTO requests(request_key, actor_id, body_hash, memory_id, applied_revision, status_code, is_purged, created_at)
                VALUES(?, ?, ?, ?, ?, 200, 0, ?)
                """,
                (req_key, actor_id, body_hash, mem_id, new_rev, now),
            )
            con.execute(
                """
                INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                VALUES('soft_delete', ?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                """,
                (mem_id, new_rev, actor_role, actor_id, existing["scope"], existing["project_id"], "{}", now),
            )
            con.execute("COMMIT")
            return {
                "status": "committed",
                "memory_id": mem_id,
                "stored_revision": new_rev,
                "current_state": "deleted",
                "effective_confidence": existing["confidence"],
                "locked": bool(existing["locked"]),
                "has_open_conflict": bool(existing["has_open_conflict"]),
                "proposal_id": None,
                "index_status": "inactive",
                "index_generation": active_gen,
                "idempotent_replay": False,
            }

        raise HubError(422, "unsupported_operation", f"不支持的 operation: {op}")
