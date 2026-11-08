"""Purge (4-step Write-Ahead Tombstone + FTS5 Rebuild + VACUUM) & Backup/Restore (v0.3.0)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .db import HubError, utc_now_iso
from .paths import checked_path


class PurgeAndBackupMixin:
    def _append_or_reuse_tombstone_entry(
        self,
        memory_id: str,
        purged_content_sha256: str,
        request_keys: List[str],
        actor_id: str,
        now: str,
    ) -> Dict[str, Any]:
        """
        Step 1 of Purge (§4.3): Append to data/tombstones/purge_registry.jsonl and os.fsync()
        BEFORE SQLite transaction commits.
        If the tail entry of purge_registry.jsonl already records this exact memory_id (from a prior
        interrupted purge), reuse it idempotently.
        """
        if not self.tombstone_path.is_file():
            raise HubError(503, "missing_or_corrupted_purge_evidence", "外部墓碑文件缺失，拒绝执行 Purge。")

        lines = [ln.strip() for ln in self.tombstone_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise HubError(503, "missing_or_corrupted_purge_evidence", "外部墓碑文件为空。")

        last_entry = json.loads(lines[-1])
        if last_entry.get("memory_id") == memory_id and int(last_entry.get("seq", 0)) > 0:
            return last_entry

        for raw in lines[1:]:
            obj = json.loads(raw)
            if obj.get("memory_id") == memory_id:
                return obj

        seq = int(last_entry["seq"]) + 1
        prev_hash = last_entry["entry_hash"]
        entry_hash = hashlib.sha256(f"{prev_hash}:{seq}:{memory_id}:{purged_content_sha256}".encode("utf-8")).hexdigest()
        record = {
            "seq": seq,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
            "memory_id": memory_id,
            "purged_content_sha256": purged_content_sha256,
            "request_keys": sorted(set(request_keys)),
            "purged_at": now,
            "actor_id": actor_id,
        }
        line_bytes = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with open(self.tombstone_path, "ab") as f:
            f.write(line_bytes)
            f.flush()
            os.fsync(f.fileno())
        return record

    def _apply_tombstone(self, item):
        """Durable logical deletion, replayed even after restoring an older backup."""
        mem_id = item["memory_id"]
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            for table in ("current_memory", "memory_fts", "revisions", "conflict_proposals"):
                con.execute(f"DELETE FROM {table} WHERE memory_id=?", (mem_id,))
            con.execute("UPDATE requests SET is_purged=1,status_code=410 WHERE memory_id=?", (mem_id,))
            for rk in item.get("request_keys", []):
                con.execute("""INSERT INTO requests(request_key,actor_id,body_hash,memory_id,applied_revision,status_code,is_purged,created_at)
                    VALUES(?,?,'purged',?,0,410,1,?) ON CONFLICT(request_key) DO UPDATE SET is_purged=1,status_code=410""",
                    (rk, item.get("actor_id", "user"), mem_id, item.get("purged_at", utc_now_iso())))
            con.execute("UPDATE index_work SET status='superseded',last_error=NULL,updated_at=? WHERE memory_id=?", (utc_now_iso(), mem_id))
            con.execute("""INSERT INTO purge_tombstones(memory_id,purge_seq,entry_hash,purged_content_sha256,purged_at,actor_id,sqlite_done)
                VALUES(?,?,?,?,?,?,1) ON CONFLICT(memory_id) DO UPDATE SET sqlite_done=1""",
                (mem_id,item["seq"],item["entry_hash"],item["purged_content_sha256"],item.get("purged_at",utc_now_iso()),item.get("actor_id","user")))
            self.set_meta("max_purge_seq", max(item["seq"], self.get_meta("max_purge_seq", 0)), con=con)
            con.execute("COMMIT")
        finally:
            con.close()

    def _complete_purge(self, memory_id, qdrant_delete_cb=None):
        # Lock order everywhere: index boundary -> SQLite write boundary.
        with self._index_lock, self._write_lock:
            vector_done = False
            if qdrant_delete_cb:
                try:
                    vector_done = qdrant_delete_cb(memory_id) is True
                except Exception:
                    pass
            exports_done = self._refresh_active_exports()
            compact_done = False
            con = self.connect()
            try:
                con.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
                con.execute("VACUUM")
                checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                compact_done = checkpoint[0] == 0
            except sqlite3.Error:
                pass
            finally:
                con.close()
            con = self.connect()
            try:
                counts = [con.execute(f"SELECT COUNT(*) FROM {t} WHERE memory_id=?", (memory_id,)).fetchone()[0]
                    for t in ("current_memory","revisions","memory_fts","conflict_proposals")]
                sqlite_done = not any(counts)
                con.execute("UPDATE purge_tombstones SET sqlite_done=?,vector_done=?,exports_done=?,compact_done=? WHERE memory_id=?",
                    (sqlite_done,vector_done,exports_done,compact_done,memory_id))
                tomb = dict(con.execute("SELECT * FROM purge_tombstones WHERE memory_id=?",(memory_id,)).fetchone())
            finally:
                con.close()
            complete = all(tomb[k] for k in ("sqlite_done","vector_done","exports_done","compact_done"))
            return {"status":"purged" if complete else "purge_pending", "memory_id":memory_id,
                "purge_receipt":tomb, "scope_notice":"受控活动 SQLite、向量世代和 active_memories 导出；历史备份恢复须重放墓碑。物理磁盘块与宿主日志不在清除范围。"}

    def retry_pending_purges(self, qdrant_delete_cb=None):
        con = self.connect()
        try:
            ids = [r[0] for r in con.execute("SELECT memory_id FROM purge_tombstones WHERE NOT(sqlite_done AND vector_done AND exports_done AND compact_done)")]
        finally:
            con.close()
        return [self._complete_purge(mid, qdrant_delete_cb) for mid in ids]

    def admin_purge(self, memory_id, confirm_purge, expected_revision=None, qdrant_delete_cb=None,
                    actor_id="user", simulate_crash_after_tombstone_file=False):
        if type(expected_revision) is not int:
            raise HubError(422,"expected_revision_required","Purge 必须携带当前修订号。")
        if confirm_purge != f"PURGE {memory_id} r{expected_revision}":
            raise HubError(422,"confirm_purge_required","确认字符串必须为 PURGE <memory_id> r<expected_revision>。")
        with self._index_lock, self._write_lock:
            self._ensure_not_reindexing()
            con = self.connect()
            try:
                existing = con.execute("SELECT * FROM current_memory WHERE memory_id=?",(memory_id,)).fetchone()
                if not existing:
                    tomb = con.execute("SELECT 1 FROM purge_tombstones WHERE memory_id=?",(memory_id,)).fetchone()
                    if tomb:
                        return self._complete_purge(memory_id,qdrant_delete_cb)
                    raise HubError(404,"memory_not_found","记忆不存在。")
                if existing["revision"] != int(expected_revision):
                    raise HubError(409,"RevisionConflict","记忆已修改，请刷新修订号。")
                keys = [r[0] for r in con.execute("SELECT request_key FROM requests WHERE memory_id=?",(memory_id,))]
                digest = hashlib.sha256(f"{existing['title']}\n{existing['content']}".encode()).hexdigest()
            finally:
                con.close()
            item = self._append_or_reuse_tombstone_entry(memory_id,digest,keys,actor_id,utc_now_iso())
            if simulate_crash_after_tombstone_file:
                raise RuntimeError("SIMULATED_CRASH_AFTER_TOMBSTONE_FILE")
            self._apply_tombstone(item)
            return self._complete_purge(memory_id,qdrant_delete_cb)

    def _refresh_active_exports(self) -> bool:
        """Re-exports current active memories into exports/active_memories.jsonl so purged items are removed."""
        try:
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            export_file = self.exports_dir / "active_memories.jsonl"
            con = self.connect()
            try:
                rows = con.execute(
                    """
                    SELECT memory_id, revision, state, locked, scope, project_id, subject, kind,
                           topic_key, title, content, confidence, source_type, source_json,
                           created_by_actor_id, created_in_session_id, expires_at, created_at, updated_at
                    FROM current_memory
                    ORDER BY memory_id ASC
                    """
                ).fetchall()
            finally:
                con.close()
            lines = [json.dumps(dict(r), ensure_ascii=False, separators=(",", ":")) for r in rows]
            tmp = export_file.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            os.replace(tmp, export_file)
            return True
        except Exception:
            return False

    def admin_backup_export(self, backup_name: Optional[str] = None) -> Dict[str, Any]:
        """Creates a consistent SQLite backup (.db) + metadata manifest in data/backups/."""
        with self._index_lock, self._write_lock:
            self.verify_and_reconcile_purge_registry()
            ts = utc_now_iso().replace(":", "").replace("-", "")
            name = backup_name or f"backup_{ts}"
            target_dir = checked_path(self.backups_dir / name)
            target_dir.mkdir(parents=True, exist_ok=True)
            backup_db_path = target_dir / "memory_hub.db"

            src_con = self.connect()
            dst_con = sqlite3.connect(str(backup_db_path))
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
                src_con.close()

            self._refresh_active_exports()
            if (self.exports_dir / "active_memories.jsonl").is_file():
                shutil.copy2(self.exports_dir / "active_memories.jsonl", target_dir / "active_memories.jsonl")

            max_seq = int(self.get_meta("max_purge_seq", 0))
            manifest = {
                "backup_name": name,
                "created_at": utc_now_iso(),
                "db_sha256": hashlib.sha256(backup_db_path.read_bytes()).hexdigest(),
                "max_purge_seq_at_backup": max_seq,
            }
            (target_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            return {
                "status": "exported",
                "backup_dir": str(target_dir),
                "backup_db_path": str(backup_db_path),
                "manifest": manifest,
            }

    def admin_backup_restore(
        self,
        backup_db_path: str | Path,
        qdrant_delete_cb: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Restores a historical SQLite backup and immediately enforces Fail-Closed
        verify_and_reconcile_purge_registry() before allowing the database online (§4.3).
        """
        bpath = checked_path(backup_db_path)
        if not bpath.is_file():
            raise HubError(404, "backup_not_found", f"未找到备份数据库文件: {bpath}")

        with closing(sqlite3.connect(f"file:{bpath.as_posix()}?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA user_version").fetchone()[0] != 300:
                raise HubError(503,"migration_required","备份须先显式迁移到当前 Schema。")
        with self._index_lock, self._write_lock:
            current_max_seq = int(self.get_meta("max_purge_seq", 0))
            # Pre-verify external tombstone file before touching the active DB
            self.verify_and_reconcile_purge_registry(
                qdrant_delete_cb=qdrant_delete_cb,
                target_min_seq=current_max_seq,
            )

            # Reconcile an isolated copy before replacing the live DB. Concurrent readers
            # must never see an old backup between its restore and tombstone replay.
            stage_root = checked_path(self.backups_dir / ('restore-stage-' + uuid.uuid4().hex))
            stage_db = stage_root / 'memory_hub.db'
            stage_root.mkdir(parents=True, exist_ok=False)
            with closing(sqlite3.connect(str(bpath))) as source, closing(sqlite3.connect(str(stage_db))) as target:
                source.backup(target)
            staged = type(self)(stage_db, self.tombstone_path, stage_root / 'exports', stage_root / 'backups')
            staged.verify_and_reconcile_purge_registry(qdrant_delete_cb=qdrant_delete_cb, target_min_seq=current_max_seq)
            src_con = sqlite3.connect(str(stage_db))
            dst_con = self.connect()
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
                src_con.close()

            # Immediately reconcile external tombstones against the restored DB (Fail-Closed)
            reconcile_res = self.verify_and_reconcile_purge_registry(
                qdrant_delete_cb=qdrant_delete_cb,
                target_min_seq=current_max_seq,
            )
            self._refresh_active_exports()
            return {
                "status": "restored_and_reconciled",
                "restored_from": str(bpath),
                "reconciled_memory_ids": reconcile_res["reconciled_memory_ids"],
                "file_max_seq": reconcile_res["file_max_seq"],
            }
