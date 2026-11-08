"""3-way Hybrid Search, Precheck, Row-Level Authorization & Atomic Reindex (v0.3.0)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from .db import DEFAULT_GENERATION, HubError, utc_now_iso

CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
NON_SPACE_TOKEN_RE = re.compile(r"[^\s,;，；。！？!?()（）\[\]【】\"'`]+")


class SearchReindexMixin:
    @staticmethod
    def _extract_search_terms(query: str) -> Tuple[List[str], List[str], List[str]]:
        """
        Splits query into:
        - fts_grams: >=3 char substrings/tokens for FTS5 trigram MATCH
        - two_char_terms: ==2 char CJK or token terms for parameterized LIKE
        - all_terms: all meaningful tokens/subterms for scoring
        """
        q = (query or "").strip()
        if not q:
            return [], [], []

        raw_tokens = NON_SPACE_TOKEN_RE.findall(q)
        fts_grams: List[str] = []
        two_char_terms: List[str] = []
        all_terms: List[str] = []

        for tok in raw_tokens:
            t_low = tok.lower()
            if t_low not in all_terms:
                all_terms.append(t_low)

            cjk_runs = CJK_RUN_RE.findall(tok)
            if cjk_runs:
                for run in cjk_runs:
                    if run not in all_terms:
                        all_terms.append(run)
                    if len(run) == 2:
                        if run not in two_char_terms:
                            two_char_terms.append(run)
                    elif len(run) >= 3:
                        for i in range(len(run) - 2):
                            gram = run[i : i + 3]
                            if gram not in fts_grams:
                                fts_grams.append(gram)
                            if gram not in all_terms:
                                all_terms.append(gram)
                        for i in range(len(run) - 1):
                            bg = run[i : i + 2]
                            if bg not in all_terms:
                                all_terms.append(bg)

            non_cjk = CJK_RUN_RE.sub(" ", tok).strip()
            for sub in non_cjk.split():
                s_low = sub.lower()
                if s_low not in all_terms:
                    all_terms.append(s_low)
                if len(s_low) >= 3:
                    if s_low not in fts_grams:
                        fts_grams.append(s_low)
                elif len(s_low) == 2:
                    if s_low not in two_char_terms:
                        two_char_terms.append(s_low)

        return fts_grams[:16], two_char_terms[:10], all_terms[:24]

    @staticmethod
    def _compute_keyword_score(
        query: str,
        all_terms: List[str],
        title: str,
        content: str,
        topic_key: str,
    ) -> float:
        q_low = (query or "").strip().lower()
        if not q_low:
            return 0.0
        hay_title = (title or "").lower()
        hay_topic = (topic_key or "").lower()
        hay_content = (content or "").lower()
        hay_all = f"{hay_title}\n{hay_topic}\n{hay_content}"

        score = 0.0
        if q_low in hay_title or q_low in hay_topic:
            score += 0.65
        elif q_low in hay_content:
            score += 0.45

        if all_terms:
            matched = 0
            title_hits = 0
            for term in all_terms:
                if term in hay_all:
                    matched += 1
                    if term in hay_title or term in hay_topic:
                        title_hits += 1
            ratio = matched / len(all_terms)
            title_bonus = min(0.25, 0.1 * title_hits)
            score = max(score, min(1.0, ratio * 0.8 + title_bonus))

        return min(1.0, round(score, 4))

    def search_memories(
        self,
        actor_role: str,
        actor_id: str,
        host_id: str,
        query: str,
        workspace_path: Optional[str] = None,
        subjects: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        limit: int = 8,
        include_non_active: bool = False,
        vector_search_fn: Optional[Callable[[str, str, int], Tuple[List[Dict[str, Any]], bool]]] = None,
        live_similarity_fn: Optional[Callable[[str, str], Optional[float]]] = None,
        event_type: str = "search",
    ) -> Dict[str, Any]:
        """
        4-Step Hybrid Search & Mandatory Row-Level Authorization (§4.2):
        1. Resolve trusted bound_project_id from workspace_path (never trust request project_id).
        2. 3-way candidate recall: Qdrant vector + FTS5 trigram / 2-char LIKE + lagging active rows.
        3. Mandatory SQLite row-level check (_check_row_authorized).
        4. Hybrid score fusion (0.65 * vector + 0.35 * keyword) + conflict_notice.
        """
        q_clean = (query or "").strip()
        top_k = max(1, min(int(limit or 8), 50))
        allow_non_active = bool(include_non_active and actor_role == "user_admin")

        con = self.connect()
        try:
            bound_project_id, _ = self.resolve_bound_project(con, workspace_path)
            active_gen = self.get_meta("active_index_generation", DEFAULT_GENERATION)

            fts_grams, two_char_terms, all_terms = self._extract_search_terms(q_clean)
            candidate_ids: set[str] = set()
            vector_scores: Dict[str, Tuple[ float, int, str ]] = {}
            degraded_mode: Optional[str] = None

            # 2a. Qdrant vector recall
            if vector_search_fn is not None and q_clean:
                try:
                    vec_hits, is_degraded = vector_search_fn(q_clean, active_gen, top_k * 3)
                    if is_degraded:
                        degraded_mode = "keyword_only"
                    for hit in vec_hits:
                        mid = hit.get("memory_id")
                        if mid:
                            candidate_ids.add(mid)
                            vector_scores[mid] = (
                                float(hit.get("score", 0.0)),
                                int(hit.get("revision", 0)),
                                str(hit.get("generation", "")),
                            )
                except Exception:
                    degraded_mode = "keyword_only"
            else:
                degraded_mode = "keyword_only"

            # 2b. FTS5 trigram recall for >=3 char terms
            for gram in fts_grams:
                safe_gram = '"' + gram.replace('"', '""') + '"'
                try:
                    fts_rows = con.execute(
                        "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
                        (safe_gram, top_k * 4),
                    ).fetchall()
                    for r in fts_rows:
                        candidate_ids.add(r["memory_id"])
                except sqlite3.Error:
                    pass

            # 2c. Parameterized LIKE recall for 2-char Chinese terms (or full query if short)
            like_terms = list(two_char_terms)
            if 0 < len(q_clean) < 3 and q_clean not in like_terms:
                like_terms.append(q_clean)
            for lt in like_terms:
                pat = f"%{lt}%"
                l_rows = con.execute(
                    """
                    SELECT memory_id FROM current_memory
                    WHERE title LIKE ? OR content LIKE ? OR topic_key LIKE ?
                    LIMIT ?
                    """,
                    (pat, pat, pat, top_k * 4),
                ).fetchall()
                for r in l_rows:
                    candidate_ids.add(r["memory_id"])

            # 2d. Lagging active rows scan (where indexed_revision < revision or generation mismatch)
            lag_rows = con.execute(
                """
                SELECT memory_id FROM current_memory
                WHERE state = 'active' AND (indexed_revision < revision OR index_generation != ?)
                LIMIT 200
                """,
                (active_gen,),
            ).fetchall()
            for r in lag_rows:
                candidate_ids.add(r["memory_id"])

            # If query is empty or keyword_only with no hits yet, scan recent rows
            if not candidate_ids:
                recent_rows = con.execute(
                    "SELECT memory_id FROM current_memory ORDER BY updated_at DESC LIMIT 100"
                ).fetchall()
                for r in recent_rows:
                    candidate_ids.add(r["memory_id"])

            # Step 3 & 4: SQLite Row-Level Authorization & Score Fusion
            scored_items: List[Dict[str, Any]] = []
            for mid in candidate_ids:
                row = con.execute("SELECT * FROM current_memory WHERE memory_id = ?", (mid,)).fetchone()
                if not row:
                    continue

                if not self._check_row_authorized(
                    row=row,
                    actor_role=actor_role,
                    actor_id=actor_id,
                    host_id=host_id,
                    bound_project_id=bound_project_id,
                    include_non_active=allow_non_active,
                ):
                    continue

                if subjects and row["subject"] not in subjects:
                    continue
                if kinds and row["kind"] not in kinds:
                    continue

                curr_rev = int(row["revision"])
                idx_rev = int(row["indexed_revision"])
                row_gen = row["index_generation"]
                is_lagging = (idx_rev < curr_rev) or (row_gen != active_gen)

                kw_score = self._compute_keyword_score(
                    q_clean, all_terms, row["title"], row["content"], row["topic_key"]
                )

                vec_score: Optional[float] = None
                if mid in vector_scores:
                    v_val, v_rev, v_gen = vector_scores[mid]
                    # Discard stale vector hits if revision or generation does not match SQLite truth
                    if v_rev == curr_rev and v_gen == active_gen:
                        vec_score = max(0.0, min(1.0, v_val))

                if vec_score is None and is_lagging and live_similarity_fn is not None and degraded_mode is None and q_clean:
                    try:
                        live_sim = live_similarity_fn(q_clean, f"{row['title']}\n{row['content']}")
                        if live_sim is not None:
                            vec_score = max(0.0, min(1.0, float(live_sim)))
                    except Exception:
                        vec_score = None

                if q_clean:
                    if vec_score is None and kw_score <= 0.0:
                        continue
                    if vec_score is not None and kw_score <= 0.0 and vec_score < 0.48:
                        continue
                    if vec_score is not None:
                        final_score = round(0.65 * vec_score + 0.35 * kw_score, 4)
                    else:
                        final_score = round(kw_score, 4)
                else:
                    final_score = 1.0

                item = self.format_memory_item(row, score=final_score)
                item["keyword_score"] = kw_score
                item["vector_score"] = round(vec_score, 4) if vec_score is not None else None
                item["index_lagging"] = is_lagging
                if int(row["has_open_conflict"]) == 1:
                    item["conflict_notice"] = "该记忆存在待人工裁决的冲突提案，当前返回最后已确认版本。"
                scored_items.append(item)

            scored_items.sort(key=lambda x: (x.get("score") or 0.0, x.get("updated_at") or ""), reverse=True)
            top_items = scored_items[:top_k]

            now = utc_now_iso()
            with self._write_lock:
                con.execute(
                    """
                    INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                    VALUES(?, NULL, NULL, ?, ?, ?, ?, 'ok', ?, ?)
                    """,
                    (
                        event_type,
                        actor_role,
                        actor_id,
                        "project" if bound_project_id else "global",
                        bound_project_id,
                        json.dumps({"result_count": len(top_items), "degraded_mode": degraded_mode}),
                        now,
                    ),
                )
            return {
                "status": "ok",
                "degraded_mode": degraded_mode,
                "bound_project_id": bound_project_id,
                "active_index_generation": active_gen,
                "count": len(top_items),
                "items": top_items,
            }
        finally:
            con.close()

    def get_memory(
        self,
        memory_id: str,
        actor_role: str,
        actor_id: str,
        host_id: str,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        con = self.connect()
        try:
            tomb = con.execute("SELECT 1 FROM purge_tombstones WHERE memory_id = ?", (memory_id,)).fetchone()
            if tomb:
                raise HubError(410, "MemoryPurged", f"记忆 {memory_id} 已永久销毁。")

            row = con.execute("SELECT * FROM current_memory WHERE memory_id = ?", (memory_id,)).fetchone()
            if not row:
                raise HubError(404, "memory_not_found", f"未找到记忆: {memory_id}")

            bound_project_id, _ = self.resolve_bound_project(con, workspace_path)
            if not self._check_row_authorized(
                row=row,
                actor_role=actor_role,
                actor_id=actor_id,
                host_id=host_id,
                bound_project_id=bound_project_id,
                include_non_active=(actor_role == "user_admin"),
            ):
                raise HubError(403, "forbidden_memory_access", "无权访问该记忆。")

            item = self.format_memory_item(row)
            if int(row["has_open_conflict"]) == 1:
                item["conflict_notice"] = "该记忆存在待人工裁决的冲突提案，当前返回最后已确认版本。"
            return {"status": "ok", "item": item}
        finally:
            con.close()

    def list_memories_for_admin(
        self,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
        state: Optional[str] = None,
        subject: Optional[str] = None,
        has_conflict: Optional[bool] = None,
        query: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        con = self.connect()
        try:
            clauses = ["1=1"]
            params: List[Any] = []
            if scope:
                clauses.append("scope = ?")
                params.append(scope)
            if project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
            if state:
                clauses.append("state = ?")
                params.append(state)
            if subject:
                clauses.append("subject = ?")
                params.append(subject)
            if has_conflict is not None:
                clauses.append("has_open_conflict = ?")
                params.append(1 if has_conflict else 0)
            if query and query.strip():
                pat = f"%{query.strip()}%"
                clauses.append("(title LIKE ? OR content LIKE ? OR topic_key LIKE ? OR memory_id LIKE ?)")
                params.extend([pat, pat, pat, pat])

            params.append(max(1, min(int(limit or 100), 500)))
            sql = f"SELECT * FROM current_memory WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?"
            rows = con.execute(sql, tuple(params)).fetchall()
            items = [self.format_memory_item(r) for r in rows]

            projects = [dict(r) for r in con.execute("SELECT * FROM project_bindings ORDER BY project_id ASC").fetchall()]
            open_proposals = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM conflict_proposals WHERE status = 'pending' ORDER BY created_at DESC"
                ).fetchall()
            ]
            return {
                "status": "ok",
                "count": len(items),
                "items": items,
                "project_bindings": projects,
                "open_proposals": open_proposals,
            }
        finally:
            con.close()

    def get_memory_detail_for_admin(self, memory_id: str) -> Dict[str, Any]:
        con = self.connect()
        try:
            row = con.execute("SELECT * FROM current_memory WHERE memory_id = ?", (memory_id,)).fetchone()
            if not row:
                tomb = con.execute("SELECT * FROM purge_tombstones WHERE memory_id = ?", (memory_id,)).fetchone()
                if tomb:
                    raise HubError(410, "MemoryPurged", f"记忆 {memory_id} 已永久销毁。")
                raise HubError(404, "memory_not_found", f"未找到记忆: {memory_id}")

            revs = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM revisions WHERE memory_id = ? ORDER BY revision DESC",
                    (memory_id,),
                ).fetchall()
            ]
            props = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM conflict_proposals WHERE memory_id = ? ORDER BY created_at DESC",
                    (memory_id,),
                ).fetchall()
            ]
            audits = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM audit_events WHERE memory_id = ? ORDER BY event_id DESC LIMIT 50",
                    (memory_id,),
                ).fetchall()
            ]
            return {
                "status": "ok",
                "item": self.format_memory_item(row),
                "revisions": revs,
                "proposals": props,
                "audit_events": audits,
            }
        finally:
            con.close()

    def admin_reindex(
        self,
        target_generation: str,
        build_and_verify_qdrant_fn: Callable[[str, List[Dict[str, Any]]], Tuple[int, str]],
        drop_qdrant_collection_fn: Optional[Callable[[str], None]] = None,
        pause_worker_fn: Optional[Callable[[], None]] = None,
        resume_worker_fn: Optional[Callable[[], None]] = None,
        simulate_mismatch: bool = False,
        simulate_crash_mid_reindex: bool = False,
    ) -> Dict[str, Any]:
        """
        7-Step Atomic Reindex Protocol (§4.3):
        1. Enter maintenance_mode='reindexing' (freezes writes with HTTP 503) & set staging_index_generation.
        2. Pause Worker without prematurely marking old generation tasks 'superseded'.
        3. Snapshot all state='active' memories and compute expected_manifest_sha256.
        4. Build new Qdrant collection memories_{target_generation} and verify point_count + actual_manifest_sha256.
        5. Only after verification passes, atomically switch active_index_generation, update indexed_revision,
           mark old tasks 'superseded', and exit maintenance_mode in a single SQLite transaction.
        6. On any failure or mismatch, keep old active_index_generation and old pending tasks untouched!
        """
        target_gen = (target_generation or "").strip()
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', target_gen) or target_gen.startswith('memories_'):
            raise HubError(422, "invalid_generation", "target_generation 不能为空。")

        old_gen = self.get_meta("active_index_generation", DEFAULT_GENERATION)
        if target_gen == old_gen:
            raise HubError(409, "generation_already_active", f"目标世代 {target_gen} 已是当前活跃世代。")

        if not self._maintenance_lock.acquire(blocking=False):
            raise HubError(409, 'maintenance_busy', '另一个维护任务正在执行。')
        if pause_worker_fn:
            pause_worker_fn()
        self._index_lock.acquire()
        staging_started = False
        try:
            with self._write_lock:
                old_gen = self.get_meta('active_index_generation', DEFAULT_GENERATION)
                if target_gen == old_gen:
                    raise HubError(409, 'generation_already_active', '不能重建当前读取集合。')
                if self.get_meta('staging_index_generation'):
                    raise HubError(503, 'staging_cleanup_pending', '上次重建集合尚未确认清理；恢复对账后再重建。')
                self.set_meta("maintenance_mode", "reindexing")
                self.set_meta("staging_index_generation", target_gen)
                staging_started = True

            con = self.connect()
            try:
                active_rows = [
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM current_memory WHERE state = 'active' ORDER BY memory_id ASC"
                    ).fetchall()
                ]
            finally:
                con.close()

            expected_lines = [
                f"{r['memory_id']}:{r['revision']}:{target_gen}"
                for r in sorted(active_rows, key=lambda x: x["memory_id"])
            ]
            expected_manifest_sha256 = hashlib.sha256("\n".join(expected_lines).encode("utf-8")).hexdigest()

            if simulate_crash_mid_reindex:
                raise RuntimeError("SIMULATED_CRASH_MID_REINDEX")

            point_count, actual_manifest_sha256 = build_and_verify_qdrant_fn(target_gen, active_rows)
            if simulate_mismatch:
                actual_manifest_sha256 = "simulated_corrupted_manifest_sha256_0000"

            if point_count != len(active_rows) or actual_manifest_sha256 != expected_manifest_sha256:
                raise HubError(
                    500,
                    "reindex_manifest_mismatch",
                    f"索引重建清单校验失败（expected={expected_manifest_sha256[:16]}, actual={actual_manifest_sha256[:16]}），已安全回退至旧世代 {old_gen}。",
                )

            # Step 5: Single atomic SQLite transaction to switch active_index_generation and supersede old tasks
            with self._write_lock:
                con = self.connect()
                try:
                    now = utc_now_iso()
                    con.execute("BEGIN IMMEDIATE")
                    for r in active_rows:
                        applied = con.execute(
                            """
                            UPDATE current_memory
                            SET indexed_revision = ?, index_generation = ?
                            WHERE memory_id = ? AND revision = ?
                            """,
                            (r["revision"], target_gen, r["memory_id"], r["revision"]),
                        )
                        if applied.rowcount != 1:
                            raise RuntimeError('reindex_snapshot_invalidated')
                    con.execute(
                        """
                        UPDATE index_work
                        SET status = 'superseded', updated_at = ?
                        WHERE generation != ? AND status IN ('pending', 'processing', 'failed')
                        """,
                        (now, target_gen),
                    )
                    self.set_meta("active_index_generation", target_gen, con=con)
                    known = set(self.get_meta('known_index_generations', [DEFAULT_GENERATION]))
                    known.update((old_gen, target_gen))
                    self.set_meta('known_index_generations', sorted(known), con=con)
                    self.set_meta("staging_index_generation", None, con=con)
                    self.set_meta("maintenance_mode", "normal", con=con)
                    con.execute(
                        """
                        INSERT INTO audit_events(event_type, memory_id, revision, actor_role, actor_id, scope, project_id, result_status, detail_meta_json, created_at)
                        VALUES('reindex', NULL, NULL, 'user_admin', 'user', 'global', NULL, 'switched', ?, ?)
                        """,
                        (
                            json.dumps(
                                {
                                    "previous_generation": old_gen,
                                    "active_generation": target_gen,
                                    "point_count": point_count,
                                    "manifest_sha256": actual_manifest_sha256,
                                }
                            ),
                            now,
                        ),
                    )
                    con.execute("COMMIT")
                finally:
                    con.close()

            return {
                "status": "switched",
                "previous_generation": old_gen,
                "active_index_generation": target_gen,
                "point_count": point_count,
                "expected_manifest_sha256": expected_manifest_sha256,
                "actual_manifest_sha256": actual_manifest_sha256,
            }
        except Exception:
            if staging_started and not simulate_crash_mid_reindex:
                cleanup_done = False
                if drop_qdrant_collection_fn:
                    try:
                        cleanup_done = drop_qdrant_collection_fn(target_gen) is True
                    except Exception:
                        pass
                with self._write_lock:
                    self.set_meta("maintenance_mode", "normal")
                    if cleanup_done:
                        self.set_meta("staging_index_generation", None)
            raise
        finally:
            self._index_lock.release()
            self._maintenance_lock.release()
            if resume_worker_fn and not simulate_crash_mid_reindex:
                resume_worker_fn()
