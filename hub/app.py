"""FastAPI Unified Gateway for SharedMemoryHub (127.0.0.1:8765, v0.3.0)."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from starlette.concurrency import run_in_threadpool
from .paths import PROJECT_ROOT, checked_path, checked_asset
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from .auth import AuthManager, CallerIdentity
from .db import DEFAULT_GENERATION, DEFAULT_LOCK_PATH, HubError, MemoryDatabase
from .embedder import EmbeddingEngine, QdrantManager
from .worker import SerialIndexWorker, SingleInstanceFileLock

UI_HTML_PATH = Path(__file__).parent / "ui.html"


def create_hub_app(
    db: Optional[MemoryDatabase] = None,
    auth: Optional[AuthManager] = None,
    embedder: Optional[EmbeddingEngine] = None,
    qdrant: Optional[QdrantManager] = None,
    worker: Optional[SerialIndexWorker] = None,
    lock_path: Path = DEFAULT_LOCK_PATH,
    acquire_process_lock: bool = True,
    auto_start_worker: bool = True,
) -> FastAPI:
    # No database, credentials, vector client or model construction during import.
    db_inst, auth_inst, embed_inst, qdrant_inst, worker_inst = db, auth, embedder, qdrant, worker
    runtime = checked_path(os.environ.get("SMH_RUNTIME_ROOT", str(PROJECT_ROOT)))
    auth_dir = checked_path(os.environ.get("SMH_AUTH_DIR", str(runtime / "config")))
    proc_lock = SingleInstanceFileLock(lock_path=lock_path if db else runtime / "data/hub.lock")

    def _delete_point_all_gens(mem_id: str) -> bool:
        with db_inst._index_lock:
            generations = set(db_inst.get_meta("known_index_generations", [DEFAULT_GENERATION]))
            generations.add(db_inst.get_meta("active_index_generation", DEFAULT_GENERATION))
            staging = db_inst.get_meta("staging_index_generation", None)
            if staging:
                generations.add(staging)
            try:
                if hasattr(qdrant_inst, 'list_generations'):
                    generations.update(qdrant_inst.list_generations())
            except Exception:
                return False
            results = [qdrant_inst.delete_point(gen, mem_id) for gen in generations]
            return all(results)

    def _vector_search_bridge(query_text: str, generation: str, top_k: int) -> Tuple[List[Dict[str, Any]], bool]:
        if embed_inst.wedged or not qdrant_inst.is_healthy():
            return [], True
        try:
            q_vec = embed_inst.encode_one(query_text, is_query=True, timeout_sec=15.0)
            hits = qdrant_inst.search_points(generation=generation, query_vector=q_vec, top_k=top_k)
            return hits, False
        except Exception:
            return [], True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal db_inst, auth_inst, embed_inst, qdrant_inst, worker_inst
        if acquire_process_lock:
            proc_lock.acquire()
        try:
            auth_inst = auth or AuthManager(client_tokens_path=auth_dir / "client_tokens.json",
                admin_auth_path=auth_dir / "admin_auth.json",
                allowed_origin=os.environ.get("SMH_ORIGIN", "http://127.0.0.1:8765"))
            db_inst = db or MemoryDatabase(db_path=runtime / "data/sqlite/memory_hub.db",
                tombstone_path=runtime / "data/tombstones/purge_registry.jsonl",
                exports_dir=runtime / "exports", backups_dir=runtime / "backups")
            model_dir = checked_asset(os.environ.get("SMH_MODEL_DIR", str(PROJECT_ROOT / "models/Qwen3-Embedding-0.6B")))
            embed_inst = embedder or EmbeddingEngine(model_dir=model_dir, device=os.environ.get("SMH_DEVICE", "cuda"))
            qdrant_inst = qdrant or QdrantManager(base_url=os.environ.get("SMH_QDRANT_URL", "http://127.0.0.1:6333"),
                collection_prefix=os.environ.get("SMH_COLLECTION_PREFIX", "memories"))
            worker_inst = worker or SerialIndexWorker(db=db_inst, embedder=embed_inst, qdrant=qdrant_inst)
            worker_inst.purge_delete_callback = _delete_point_all_gens
            for name, obj in (("db", db_inst), ("auth", auth_inst), ("embedder", embed_inst), ("qdrant", qdrant_inst), ("worker", worker_inst)):
                setattr(app.state, name, obj)
            await run_in_threadpool(db_inst.verify_and_reconcile_purge_registry, qdrant_delete_cb=_delete_point_all_gens)
            await run_in_threadpool(db_inst.startup_recover_reindex_and_worker, qdrant_drop_collection_cb=qdrant_inst.drop_collection)
            try:
                await run_in_threadpool(qdrant_inst.ensure_collection, db_inst.get_meta("active_index_generation", DEFAULT_GENERATION))
            except Exception:
                pass  # Explicit degraded health, never an alternative vector store.
            if auto_start_worker:
                worker_inst.start_background()
            yield
        finally:
            if worker_inst and auto_start_worker:
                await run_in_threadpool(worker_inst.stop_background)
            if not qdrant and qdrant_inst:
                qdrant_inst.close()
            if acquire_process_lock:
                proc_lock.release()

    app = FastAPI(title="SharedMemoryHub", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(HubError)
    async def hub_error_handler(request: Request, exc: HubError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "error_code": exc.error_code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )

    def _resolve_caller(request: Request, require_csrf_if_admin_write: bool = True) -> CallerIdentity:
        auth_hdr = request.headers.get("Authorization")
        sess_cookie = request.cookies.get("smh_admin_session")
        sess_hdr = request.headers.get("X-Admin-Session")
        ident = auth_inst.authenticate_bearer_or_session(
            authorization_header=auth_hdr,
            session_cookie=sess_cookie,
            session_header=sess_hdr,
        )
        if ident.actor_role == "user_admin" and require_csrf_if_admin_write and request.method in ("POST", "PUT", "DELETE", "PATCH"):
            auth_inst.require_admin_session(
                method=request.method,
                authorization_header=auth_hdr,
                session_cookie=sess_cookie,
                session_header=sess_hdr,
                csrf_header=request.headers.get("X-CSRF-Token"),
                origin_header=request.headers.get("Origin"),
            )
        return ident

    def _require_admin(request: Request) -> CallerIdentity:
        return auth_inst.require_admin_session(
            method=request.method,
            authorization_header=request.headers.get("Authorization"),
            session_cookie=request.cookies.get("smh_admin_session"),
            session_header=request.headers.get("X-Admin-Session"),
            csrf_header=request.headers.get("X-CSRF-Token"),
            origin_header=request.headers.get("Origin"),
        )

    @app.middleware("http")
    async def protection_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @app.get("/ui.js")
    def serve_ui_script():
        return FileResponse(UI_HTML_PATH.with_suffix(".js"), media_type="application/javascript")

    @app.get('/favicon.ico')
    def favicon():
        return Response(status_code=204)

    @app.get("/ui", response_class=HTMLResponse)
    def serve_ui():
        return HTMLResponse(content=UI_HTML_PATH.read_text(encoding="utf-8"))

    @app.get("/v1/health")
    def get_health():
        con = db_inst.connect()
        try:
            pend = con.execute("SELECT COUNT(*) AS c FROM index_work WHERE status = 'pending'").fetchone()["c"]
            proc = con.execute("SELECT COUNT(*) AS c FROM index_work WHERE status = 'processing'").fetchone()["c"]
            fail = con.execute("SELECT COUNT(*) AS c FROM index_work WHERE status = 'failed'").fetchone()["c"]
            active_mems = con.execute("SELECT COUNT(*) AS c FROM current_memory WHERE state = 'active'").fetchone()["c"]
            pending_purges = con.execute('SELECT COUNT(*) FROM purge_tombstones WHERE NOT(sqlite_done AND vector_done AND exports_done AND compact_done)').fetchone()[0]
        finally:
            con.close()

        q_ok = qdrant_inst.is_healthy()
        return {
            "status": "ok" if (q_ok and not embed_inst.wedged) else "degraded",
            "version": "0.3.0",
            "sqlite": "ok",
            "qdrant": "ok" if q_ok else "unreachable",
            "embedding_model_loaded": embed_inst.loaded,
            "embedding_engine_wedged": embed_inst.wedged,
            "active_index_generation": db_inst.get_meta("active_index_generation", DEFAULT_GENERATION),
            "staging_index_generation": db_inst.get_meta("staging_index_generation", None),
            "maintenance_mode": db_inst.get_meta("maintenance_mode", "normal"),
            "max_purge_seq": int(db_inst.get_meta("max_purge_seq", 0)),
            "active_memories": active_mems,
            "pending_index_work": pend,
            "processing_index_work": proc,
            "failed_index_work": fail,
            "pending_purges": pending_purges,
            "vector_backend": "docker_qdrant_rest",
            "embedding_device": getattr(embed_inst, 'device', 'fixture'),
        }

    @app.post("/v1/memory/precheck")
    def memory_precheck(request: Request, body: Dict[str, Any]):
        ident = _resolve_caller(request, require_csrf_if_admin_write=False)
        return db_inst.search_memories(
            actor_role=ident.actor_role,
            actor_id=ident.actor_id,
            host_id=ident.host_id,
            query=body.get("query") or body.get("task_summary") or "",
            workspace_path=body.get("workspace_path"),
            subjects=body.get("subjects") or ["user", "project", "shared"],
            kinds=body.get("kinds") or ["preference", "decision", "fact", "experience", "handoff"],
            limit=int(body.get("limit", 8)),
            include_non_active=False,
            vector_search_fn=_vector_search_bridge,
            live_similarity_fn=embed_inst.cosine_similarity,
            event_type="precheck",
        )

    @app.post("/v1/memory/search")
    def memory_search(request: Request, body: Dict[str, Any]):
        ident = _resolve_caller(request, require_csrf_if_admin_write=False)
        return db_inst.search_memories(
            actor_role=ident.actor_role,
            actor_id=ident.actor_id,
            host_id=ident.host_id,
            query=body.get("query") or "",
            workspace_path=body.get("workspace_path"),
            subjects=body.get("subjects"),
            kinds=body.get("kinds"),
            limit=int(body.get("limit", 8)),
            include_non_active=bool(body.get("include_non_active", False)),
            vector_search_fn=_vector_search_bridge,
            live_similarity_fn=embed_inst.cosine_similarity,
            event_type="search",
        )

    @app.get("/v1/memory/{memory_id}")
    def memory_get(memory_id: str, request: Request, workspace_path: Optional[str] = None):
        ident = _resolve_caller(request, require_csrf_if_admin_write=False)
        return db_inst.get_memory(
            memory_id=memory_id,
            actor_role=ident.actor_role,
            actor_id=ident.actor_id,
            host_id=ident.host_id,
            workspace_path=workspace_path,
        )

    @app.post("/v1/memory/write")
    def memory_write(request: Request, body: Dict[str, Any]):
        ident = _resolve_caller(request, require_csrf_if_admin_write=True)
        return db_inst.write_memory(
            actor_role=ident.actor_role,
            actor_id=ident.actor_id,
            host_id=ident.host_id,
            payload=body,
        )

    @app.post("/v1/admin/login")
    def admin_login(body: Dict[str, Any], response: Response, request: Request):
        if request.headers.get("Origin") != auth_inst.allowed_origin:
            raise HubError(403, "csrf_origin_forbidden", "登录 Origin 不匹配。")
        sess = auth_inst.create_admin_session(passcode=str(body.get("passcode") or ""))
        response.set_cookie(
            key="smh_admin_session",
            value=sess["session_id"],
            httponly=True,
            samesite="strict",
            max_age=sess["expires_in"],
        )
        return {
            "status": "ok",
            "csrf_token": sess["csrf_token"],
            "expires_in": sess["expires_in"],
        }

    @app.post("/v1/admin/projects")
    def admin_bind_project(request: Request, body: Dict[str, Any]):
        _require_admin(request)
        pid = (body.get("project_id") or "").strip()
        wpath = (body.get("workspace_path") or "").strip()
        if not pid or not wpath:
            raise HubError(422, "invalid_project_binding", "project_id 与 workspace_path 均不能为空。")
        return db_inst.register_project_binding(project_id=pid, workspace_path=wpath)

    @app.get("/v1/admin/memories")
    def admin_list_memories(
        request: Request,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
        state: Optional[str] = None,
        subject: Optional[str] = None,
        has_conflict: Optional[bool] = None,
        query: Optional[str] = None,
        limit: int = 100,
    ):
        _require_admin(request)
        return db_inst.list_memories_for_admin(
            scope=scope,
            project_id=project_id,
            state=state,
            subject=subject,
            has_conflict=has_conflict,
            query=query,
            limit=limit,
        )

    @app.get("/v1/admin/memory/{memory_id}/detail")
    def admin_memory_detail(memory_id: str, request: Request):
        _require_admin(request)
        return db_inst.get_memory_detail_for_admin(memory_id=memory_id)

    @app.post("/v1/admin/memory/{memory_id}/undo")
    def admin_memory_undo(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        return db_inst.admin_undo(
            memory_id=memory_id,
            expected_revision=int(body["expected_revision"]),
            actor_id=ident.actor_id,
        )

    @app.post("/v1/admin/memory/{memory_id}/restore")
    def admin_memory_restore(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        return db_inst.admin_restore(
            memory_id=memory_id,
            target_revision=int(body["target_revision"]),
            expected_revision=int(body["expected_revision"]),
            actor_id=ident.actor_id,
        )

    @app.post("/v1/admin/memory/{memory_id}/lock")
    def admin_memory_lock(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        return db_inst.admin_set_lock(
            memory_id=memory_id,
            locked=bool(body["locked"]),
            expected_revision=int(body["expected_revision"]),
            actor_id=ident.actor_id,
        )

    @app.post("/v1/admin/memory/{memory_id}/state")
    def admin_memory_state(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        return db_inst.admin_set_state(
            memory_id=memory_id,
            new_state=str(body["state"]),
            expected_revision=int(body["expected_revision"]),
            actor_id=ident.actor_id,
        )

    @app.post("/v1/admin/memory/{memory_id}/resolve")
    def admin_memory_resolve(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        return db_inst.admin_resolve_conflict(
            memory_id=memory_id,
            proposal_id=str(body["proposal_id"]),
            resolution=str(body["resolution"]),
            expected_revision=int(body["expected_revision"]),
            final_title=body.get("final_title"),
            final_content=body.get("final_content"),
            actor_id=ident.actor_id,
        )

    @app.post("/v1/admin/memory/{memory_id}/purge")
    def admin_memory_purge(memory_id: str, request: Request, body: Dict[str, Any]):
        ident = _require_admin(request)
        result = db_inst.admin_purge(
            memory_id=memory_id,
            confirm_purge=body.get("confirm_purge"),
            expected_revision=body.get("expected_revision"),
            qdrant_delete_cb=_delete_point_all_gens,
            actor_id=ident.actor_id,
        )

        return JSONResponse(result, status_code=202 if result["status"] == "purge_pending" else 200)

    @app.post("/v1/admin/reindex")
    def admin_trigger_reindex(request: Request, body: Dict[str, Any]):
        _require_admin(request)
        target_gen = str(body.get("target_generation") or "")
        if hasattr(qdrant_inst, 'prefix') and target_gen.startswith(qdrant_inst.prefix + '_'):
            raise HubError(422, 'invalid_generation', '仅接受逻辑世代名，不能传入集合全名。')
        return db_inst.admin_reindex(
            target_generation=target_gen,
            build_and_verify_qdrant_fn=lambda tg, rows: qdrant_inst.build_and_verify_generation(tg, rows, embed_inst),
            drop_qdrant_collection_fn=qdrant_inst.drop_collection,
            pause_worker_fn=worker_inst.pause,
            resume_worker_fn=worker_inst.resume,
            simulate_mismatch=bool(body.get("simulate_mismatch", False)),
        )

    @app.post("/v1/admin/backup/export")
    def admin_export_backup(request: Request, body: Dict[str, Any]):
        _require_admin(request)
        return db_inst.admin_backup_export(backup_name=body.get("backup_name"))

    @app.post("/v1/admin/backup/restore")
    def admin_restore_backup(request: Request, body: Dict[str, Any]):
        _require_admin(request)
        return db_inst.admin_backup_restore(
            backup_db_path=str(body["backup_db_path"]),
            qdrant_delete_cb=_delete_point_all_gens,
        )

    @app.post("/v1/admin/worker/step")
    def admin_worker_step(request: Request):
        _require_admin(request)
        return worker_inst.run_once(ignore_retry_schedule=True)

    return app


app = create_hub_app()
