"""Single-Instance Lock & Serial Index Worker with Coalescing, CAS & 5/15/45s Backoff (v0.3.0)."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import DEFAULT_GENERATION, DEFAULT_LOCK_PATH, MemoryDatabase, utc_now_iso
from .embedder import EmbeddingEngine, QdrantManager


class SingleInstanceFileLock:
    """
    OS-level non-blocking file lock using msvcrt.locking on Windows (or fcntl on POSIX)
    at the selected runtime data/hub.lock (§4.4).
    """

    def __init__(self, lock_path: Path = DEFAULT_LOCK_PATH):
        self.lock_path = Path(lock_path)
        self._fh: Optional[Any] = None
        self.acquired: bool = False

    def acquire(self) -> None:
        if self.acquired:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise RuntimeError(f"SINGLE_INSTANCE_LOCK_CONFLICT: {self.lock_path} is already locked ({e})")

        self._fh = fh
        self.acquired = True

    def release(self) -> None:
        if not self.acquired or self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            self.acquired = False

    def __enter__(self) -> "SingleInstanceFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class SerialIndexWorker:
    """
    Single serial background worker that processes index_work tasks with:
    - Task coalescing (folds older pending revisions of the same memory_id into 'superseded')
    - Pre-encode and Post-encode CAS watermark verification
    - 3 retries with 5 * (3 ** (retry_count - 1)) seconds exponential backoff (5s, 15s, 45s)
    """

    def __init__(
        self,
        db: MemoryDatabase,
        embedder: EmbeddingEngine,
        qdrant: QdrantManager,
        poll_interval_sec: float = 0.5,
    ):
        self.db = db
        self.embedder = embedder
        self.qdrant = qdrant
        self.poll_interval_sec = poll_interval_sec
        self._paused = False
        self._step_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.simulate_post_encode_hook: Optional[Any] = None

    def pause(self) -> None:
        self._paused = True
        with self._step_lock:
            pass

    def resume(self) -> None:
        self._paused = False

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="smh_serial_worker", daemon=True)
        self._thread.start()

    def stop_background(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if hasattr(self, "purge_delete_callback"):
                    self.db.retry_pending_purges(self.purge_delete_callback)
                res = self.run_once()
                if res.get("status") in ("idle", "paused_or_reindexing"):
                    self._stop_event.wait(self.poll_interval_sec)
            except Exception:
                self._stop_event.wait(self.poll_interval_sec)

    @staticmethod
    def compute_backoff_seconds(retry_count: int) -> int:
        """Formula from §4.4: 5 * (3 ** (retry_count - 1)) -> 1: 5s, 2: 15s, 3: 45s."""
        return int(5 * (3 ** (max(1, int(retry_count)) - 1)))

    def run_once(
        self,
        simulate_crash_mid_task: bool = False,
        simulate_qdrant_error: bool = False,
        ignore_retry_schedule: bool = False,
    ) -> Dict[str, Any]:
        if self._paused:
            return {"status": "paused_or_reindexing"}

        with self._step_lock, self.db._index_lock:
            if self._paused or self.db.get_meta("maintenance_mode", "normal") == "reindexing":
                return {"status": "paused_or_reindexing"}

            now = utc_now_iso()
            active_gen = self.db.get_meta("active_index_generation", DEFAULT_GENERATION)

            # Step 1: Claim oldest eligible task & coalesce older pending tasks for the same memory_id
            with self.db._write_lock:
                con = self.db.connect()
                try:
                    if ignore_retry_schedule:
                        candidate = con.execute(
                            """
                            SELECT * FROM index_work
                            WHERE status = 'pending'
                            ORDER BY work_id ASC LIMIT 1
                            """
                        ).fetchone()
                    else:
                        candidate = con.execute(
                            """
                            SELECT * FROM index_work
                            WHERE status = 'pending'
                              AND (next_retry_at IS NULL OR next_retry_at <= ?)
                            ORDER BY work_id ASC LIMIT 1
                            """,
                            (now,),
                        ).fetchone()

                    if not candidate:
                        return {"status": "idle"}

                    mem_id = candidate["memory_id"]
                    gen = candidate["generation"]

                    # Find all pending tasks for (memory_id, generation) to coalesce
                    same_mem_tasks = con.execute(
                        """
                        SELECT * FROM index_work
                        WHERE memory_id = ? AND generation = ? AND status = 'pending'
                        ORDER BY target_revision DESC, work_id DESC
                        """,
                        (mem_id, gen),
                    ).fetchall()

                    chosen = same_mem_tasks[0]
                    chosen_id = int(chosen["work_id"])
                    coalesced_ids = [int(r["work_id"]) for r in same_mem_tasks[1:]]

                    con.execute("BEGIN IMMEDIATE")
                    if coalesced_ids:
                        placeholders = ",".join("?" for _ in coalesced_ids)
                        con.execute(
                            f"UPDATE index_work SET status = 'superseded', updated_at = ? WHERE work_id IN ({placeholders})",
                            tuple([now] + coalesced_ids),
                        )
                    con.execute(
                        """
                        UPDATE index_work
                        SET status = 'processing', claimed_at = ?, updated_at = ?
                        WHERE work_id = ?
                        """,
                        (now, now, chosen_id),
                    )
                    con.execute("COMMIT")
                    task = dict(chosen)
                finally:
                    con.close()

            work_id = int(task["work_id"])
            mem_id = task["memory_id"]
            target_rev = int(task["target_revision"])
            task_gen = task["generation"]
            action = task["action"]

            if simulate_crash_mid_task:
                raise RuntimeError("SIMULATED_WORKER_CRASH_MID_TASK")

            # Step 2: Pre-check against SQLite truth & tombstones
            con = self.db.connect()
            try:
                tomb = con.execute("SELECT 1 FROM purge_tombstones WHERE memory_id = ?", (mem_id,)).fetchone()
                curr = con.execute("SELECT * FROM current_memory WHERE memory_id = ?", (mem_id,)).fetchone()
                active_gen_now = self.db.get_meta("active_index_generation", DEFAULT_GENERATION)
            finally:
                con.close()

            if tomb or task_gen != active_gen_now or (curr and int(curr["revision"]) > target_rev):
                with self.db._write_lock:
                    con = self.db.connect()
                    try:
                        con.execute(
                            "UPDATE index_work SET status = 'superseded', updated_at = ? WHERE work_id = ?",
                            (utc_now_iso(), work_id),
                        )
                    finally:
                        con.close()
                return {
                    "status": "superseded",
                    "work_id": work_id,
                    "memory_id": mem_id,
                    "coalesced_count": len(coalesced_ids),
                }

            # Step 3: Perform delete or upsert with Post-Encode CAS re-check
            try:
                if simulate_qdrant_error:
                    raise ConnectionError("SIMULATED_QDRANT_UNAVAILABLE")
                if not self.qdrant.is_healthy():
                    raise ConnectionError('QDRANT_UNAVAILABLE')

                if action == "delete" or not curr or curr["state"] != "active":
                    ok = self.qdrant.delete_point(active_gen_now, mem_id)
                    if not ok:
                        raise RuntimeError("Qdrant delete_point returned False")
                    with self.db._write_lock:
                        con = self.db.connect()
                        try:
                            con.execute(
                                "UPDATE index_work SET status = 'done', updated_at = ? WHERE work_id = ?",
                                (utc_now_iso(), work_id),
                            )
                        finally:
                            con.close()
                    return {
                        "status": "done",
                        "action": "delete",
                        "work_id": work_id,
                        "memory_id": mem_id,
                        "target_revision": target_rev,
                        "coalesced_count": len(coalesced_ids),
                    }

                # Encode text using Qwen3-Embedding-0.6B
                text_to_embed = f"{curr['title']}\n{curr['content']}"
                vec = self.embedder.encode_one(text_to_embed, is_query=False, timeout_sec=15.0)

                if self.simulate_post_encode_hook is not None:
                    self.simulate_post_encode_hook(mem_id, target_rev)

                # Post-Encode CAS re-verification before touching Qdrant or indexed_revision!
                con = self.db.connect()
                try:
                    tomb_after = con.execute("SELECT 1 FROM purge_tombstones WHERE memory_id = ?", (mem_id,)).fetchone()
                    curr_after = con.execute(
                        "SELECT revision, state FROM current_memory WHERE memory_id = ?",
                        (mem_id,),
                    ).fetchone()
                    gen_after = self.db.get_meta("active_index_generation", DEFAULT_GENERATION)
                finally:
                    con.close()

                if (
                    tomb_after
                    or not curr_after
                    or curr_after["state"] != "active"
                    or int(curr_after["revision"]) != target_rev
                    or gen_after != task_gen
                ):
                    with self.db._write_lock:
                        con = self.db.connect()
                        try:
                            con.execute(
                                "UPDATE index_work SET status = 'superseded', updated_at = ? WHERE work_id = ?",
                                (utc_now_iso(), work_id),
                            )
                        finally:
                            con.close()
                    return {
                        "status": "superseded_post_encode",
                        "work_id": work_id,
                        "memory_id": mem_id,
                        "target_revision": target_rev,
                        "coalesced_count": len(coalesced_ids),
                    }

                ok = self.qdrant.upsert_point(task_gen, mem_id, target_rev, vec)
                if not ok:
                    raise RuntimeError("Qdrant upsert_point returned False")

                with self.db._write_lock:
                    con = self.db.connect()
                    try:
                        now_done = utc_now_iso()
                        con.execute("BEGIN IMMEDIATE")
                        applied = con.execute(
                            """
                            UPDATE current_memory
                            SET indexed_revision = ?, index_generation = ?
                            WHERE memory_id = ? AND revision = ? AND state = 'active'
                              AND NOT EXISTS(SELECT 1 FROM purge_tombstones WHERE memory_id=current_memory.memory_id)
                            """,
                            (target_rev, task_gen, mem_id, target_rev),
                        )
                        valid = applied.rowcount == 1 and self.db.get_meta("active_index_generation") == task_gen
                        con.execute("UPDATE index_work SET status=?,updated_at=? WHERE work_id=?",
                            ("done" if valid else "superseded", now_done, work_id))
                        con.execute("COMMIT")
                    finally:
                        con.close()

                if not valid:
                    if not self.qdrant.delete_point(task_gen, mem_id):
                        con = self.db.connect()
                        try:
                            con.execute('UPDATE purge_tombstones SET vector_done=0 WHERE memory_id=?', (mem_id,))
                        finally:
                            con.close()
                        raise RuntimeError("stale_vector_compensation_pending")
                    return {"status":"superseded_compensated", "memory_id":mem_id}
                return {
                    "status": "done",
                    "action": "upsert",
                    "work_id": work_id,
                    "memory_id": mem_id,
                    "target_revision": target_rev,
                    "coalesced_count": len(coalesced_ids),
                }

            except Exception as exc:
                new_retry = int(task["retry_count"]) + 1
                now_err = datetime.now(timezone.utc)
                now_err_iso = now_err.strftime("%Y-%m-%dT%H:%M:%SZ")
                err_msg = f"{type(exc).__name__}: {exc}"

                with self.db._write_lock:
                    con = self.db.connect()
                    try:
                        if new_retry <= 3:
                            backoff_sec = self.compute_backoff_seconds(new_retry)
                            next_retry_iso = (now_err + timedelta(seconds=backoff_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
                            con.execute(
                                """
                                UPDATE index_work
                                SET status = 'pending', retry_count = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                                WHERE work_id = ? AND status = 'processing'
                                """,
                                (new_retry, next_retry_iso, err_msg, now_err_iso, work_id),
                            )
                            return {
                                "status": "retry_scheduled",
                                "work_id": work_id,
                                "memory_id": mem_id,
                                "retry_count": new_retry,
                                "backoff_seconds": backoff_sec,
                                "next_retry_at": next_retry_iso,
                                "error": err_msg,
                            }
                        else:
                            con.execute(
                                """
                                UPDATE index_work
                                SET status = 'failed', retry_count = ?, last_error = ?, updated_at = ?
                                WHERE work_id = ? AND status = 'processing'
                                """,
                                (new_retry, err_msg, now_err_iso, work_id),
                            )
                            return {
                                "status": "failed",
                                "work_id": work_id,
                                "memory_id": mem_id,
                                "retry_count": new_retry,
                                "error": err_msg,
                            }
                    finally:
                        con.close()
