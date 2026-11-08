"""v0.3.0 追加真机验收；真实 CUDA、HTTP 服务与 Docker Qdrant。

故障由本机 HTTP 代理注入；不替换编码器、向量后端或数据库。
只接受 receipts 内的新实例、独立端口与 smh030_ 集合前缀。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hub.paths import checked_path

for key, suffix in {"TEMP": "tmp", "TMP": "tmp", "HF_HOME": "hf", "TORCH_HOME": "torch", "XDG_CACHE_HOME": "xdg"}.items():
    directory = checked_path(ROOT / "cache" / suffix)
    directory.mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(directory)
os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")

import httpx
from hub.auth import AuthManager
from hub.db import MemoryDatabase
from hub.embedder import memory_id_to_point_uuid
from scripts.runtime_control import identity, port_free


def wait_for(check, seconds=90, label="condition"):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()  # Callers here only observe state; never replay writes.
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException) as error:
            last = type(error).__name__
            time.sleep(.2)
            continue
        if last:
            return last
        time.sleep(0.2)
    raise AssertionError(f"deadline: {label}; last={last!r}")


class FaultProxy:
    def __init__(self, port, backend, prefix):
        self.backend, self.prefix = backend, prefix
        self.fail_deletes = False
        self.fail_generation = None
        self.fail_drop_generation = None
        self.hold_generation = None
        self.entered, self.release = threading.Event(), threading.Event()
        self.held_once = False
        self.events = []
        self.guard = threading.Lock()
        self.upstream = httpx.Client(base_url=backend, trust_env=False, timeout=10,
                                     limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log authentication headers or fixture content.

            def do_GET(self): self.forward()
            def do_POST(self): self.forward()
            def do_PUT(self): self.forward()
            def do_DELETE(self): self.forward()

            def forward(self):
                path = self.path
                if path != "/collections" and not path.startswith("/collections/" + owner.prefix + "_"):
                    self.reply(403, b'{"error":"outside_test_prefix"}')
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                points_write = self.command == "PUT" and "/points?" in path
                injected = (owner.fail_deletes and self.command == "POST" and "/points/delete" in path)
                injected |= bool(owner.fail_generation and points_write and path.startswith("/collections/" + owner.prefix + "_" + owner.fail_generation + "/"))
                injected |= bool(owner.fail_drop_generation and self.command == "DELETE" and path == "/collections/" + owner.prefix + "_" + owner.fail_drop_generation)
                if injected:
                    owner.events.append({"method": self.command, "path": path, "fault": "HTTP 503"})
                    self.reply(503, b'{"error":"controlled_transport_fault"}')
                    return
                try:
                    response = owner.upstream.request(self.command, path, content=body,
                                                       headers={"Content-Type": "application/json"})
                    hold = False
                    with owner.guard:
                        if (points_write and owner.hold_generation and not owner.held_once
                                and path.startswith("/collections/" + owner.prefix + "_" + owner.hold_generation + "/")):
                            owner.held_once = True
                            hold = True
                    if hold:
                        owner.events.append({"method": self.command, "path": path, "fault": "response gate after real upsert"})
                        owner.entered.set()
                        owner.release.wait(12)
                    self.reply(response.status_code, response.content)
                except Exception:
                    self.reply(502, b'{"error":"backend_unreachable"}')

            def reply(self, code, body):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def hold(self, generation):
        self.hold_generation = generation
        self.held_once = False
        self.entered.clear()
        self.release.clear()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.upstream.close()


class Acceptance:
    def __init__(self, args):
        self.args = args
        evidence = checked_path(args.evidence_root)
        if not evidence.is_relative_to(ROOT / "receipts"):
            raise ValueError("receipts_root_required")
        self.runtime = checked_path(evidence / ("vector-live-" + uuid.uuid4().hex[:12]))
        self.runtime.mkdir(parents=True, exist_ok=False)
        if len({args.port, args.proxy_port, args.qdrant_port}) != 3 or any(p in (6333, 6334, 8765) for p in (args.port, args.proxy_port, args.qdrant_port)):
            raise ValueError("isolated_ports_required")
        if not port_free(args.port) or not port_free(args.proxy_port):
            raise ValueError("fixture_port_busy")
        self.prefix = "smh030_" + uuid.uuid4().hex[:16]
        self.origin = f"http://127.0.0.1:{args.port}"
        self.backend = f"http://127.0.0.1:{args.qdrant_port}"
        self.fixture_pass = "Synthetic-fixture-030"
        auth = AuthManager(self.runtime / "config/client_tokens.json", self.runtime / "config/admin_auth.json",
                           initial_passcode=self.fixture_pass, allowed_origin=self.origin)
        self.tokens = auth.get_client_tokens()  # This isolated fixture only; never serialize tokens.
        db = MemoryDatabase(self.runtime / "data/sqlite/memory_hub.db", self.runtime / "data/tombstones/purge_registry.jsonl",
                            self.runtime / "exports", self.runtime / "backups")
        self.workspace = self.runtime / "workspace"
        self.workspace.mkdir()
        db.register_project_binding("synthetic_vector", self.workspace)
        self.proxy = FaultProxy(args.proxy_port, self.backend, self.prefix)
        self.client = httpx.Client(base_url=self.origin, trust_env=False, timeout=20)
        self.raw = httpx.Client(base_url=self.backend, trust_env=False, timeout=10)
        self.headers = {}
        self.started = False
        self.receipt = {"version": "0.3.0+phase1a.followup2", "phase": "1A", "status": "部分验证",
                        "coverage": "connectivity_only" if args.connectivity_only else "full_vector_suite",
                        "evidence_level": "controlled-trial", "runtime": str(self.runtime), "prefix": self.prefix,
                        "ports": {"gateway": args.port, "proxy": args.proxy_port, "qdrant": args.qdrant_port},
                        "fault_method": "External HTTP fault proxy over real Docker Qdrant; no model/backend substitutions",
                        "steps": [], "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.save()

    def save(self):
        (self.runtime / "vector-live-receipt.json").write_text(json.dumps(self.receipt, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark(self, name, detail):
        self.receipt["steps"].append({"name": name, "result": "passed", "detail": detail})
        self.save()
        print(json.dumps({"runtime": str(self.runtime), "passed": name}, ensure_ascii=False), flush=True)

    def control(self, action):
        command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/runtime_control.py"), action,
                   "--runtime", str(self.runtime), "--port", str(self.args.port), "--qdrant-port", str(self.args.proxy_port),
                   "--prefix", self.prefix, "--device", "cuda"]
        response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=57)
        if response.returncode:
            raise RuntimeError(response.stdout[-500:] + response.stderr[-1800:])
        self.started = action == "start"
        return json.loads(response.stdout)

    def login(self):
        response = self.client.post("/v1/admin/login", headers={"Origin": self.origin}, json={"passcode": self.fixture_pass})
        response.raise_for_status()
        self.headers = {"Origin": self.origin, "X-CSRF-Token": response.json()["csrf_token"]}

    def admin(self, path, body, expected=200):
        response = self.client.post(path, headers=self.headers, json=body)
        assert response.status_code == expected, (path, response.status_code, response.text[:800])
        return response.json()

    def agent(self, path, body, who="CODEX", expected=200):
        response = self.client.post(path, headers={"Authorization": "Bearer " + self.tokens[who + "_CLIENT_TOKEN"]}, json=body)
        assert response.status_code == expected, (path, response.status_code, response.text[:800])
        return response.json()

    def payload(self, title, content, **kw):
        return {"request_key": uuid.uuid4().hex, "operation": "create", "workspace_path": str(self.workspace),
                "kind": "preference", "title": title, "content": content,
                "source": {"origin_type": "user_explicit", "session_id": "synthetic-vector", "evidence_ref": "合成用户明确原话"}, **kw}

    def create(self, title, content, **kw):
        return self.agent("/v1/memory/write", self.payload(title, content, **kw))

    def search(self, query, expected_id=None, hybrid=True):
        result = self.agent("/v1/memory/search", {"query": query, "workspace_path": str(self.workspace), "limit": 3})
        if hybrid:
            assert result.get("degraded_mode") != "keyword_only", result
        if expected_id:
            assert expected_id in [item["memory_id"] for item in result["items"]], (query, result)
        return result

    def sql(self, query, parameters=()):
        with sqlite3.connect(str(self.runtime / "data/sqlite/memory_hub.db"), timeout=2) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(query, parameters)]

    def synchronized(self):
        rows = self.sql("SELECT revision,indexed_revision,index_generation FROM current_memory WHERE state='active'")
        return bool(rows) and all(row["indexed_revision"] == row["revision"] for row in rows)

    def raw_points(self, gen, mid, with_vector=False):
        response = self.raw.post(f"/collections/{self.prefix}_{gen}/points",
                                 json={"ids": [memory_id_to_point_uuid(mid)], "with_payload": True, "with_vector": with_vector})
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return response.json()["result"]

    def crash(self):
        info = json.loads((self.runtime / "service.json").read_text(encoding="utf-8"))
        assert identity(info["pid"]) == info["identity"], "fixture process identity changed"
        os.kill(info["pid"], signal.SIGTERM)  # Windows TerminateProcess: deliberate fixture crash.
        wait_for(lambda: identity(info["pid"]) is None and port_free(self.args.port), 10, "fixture crash exit")
        self.started = False
        self.client.cookies.clear()

    def docker(self, action):
        command = ["docker", action]
        if action == "stop":
            command += ["--time", "10"]
        command.append(self.args.container)
        result = subprocess.run(command, capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stderr

    def connectivity(self, expected_id, query):
        self.docker("stop")
        wait_for(lambda: port_free(self.args.qdrant_port), 10, "Qdrant port release")
        offline = self.search(query, expected_id, hybrid=False)
        assert offline["degraded_mode"] == "keyword_only"
        self.docker("start")
        wait_for(lambda: self.raw.get("/healthz").status_code == 200, 20, "Qdrant restart")
        self.search(query, expected_id)
        self.mark("real Docker offline keyword degradation and persisted restart", {"offline_mode": offline["degraded_mode"]})

    def run(self):
        try:
            response = self.raw.get("/healthz")
            response.raise_for_status()
            self.receipt["startup"] = self.control("start")
            self.login()
            cli_ids = []
            for who in ("antigravity", "codex"):
                key = uuid.uuid4().hex
                command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "clients/memory_cli.py"), "--client", who,
                           "--tokens-path", str(self.runtime / "config/client_tokens.json"), "--base-url", self.origin,
                           "write", "--op", "create", "--request-key", key, "--workspace", str(self.workspace),
                           "--kind", "preference", "--title", who + "合成偏好", "--content", "合成偏好：使用简体中文，引用可核验证据。",
                           "--origin-type", "user_explicit", "--session-id", "synthetic-vector", "--evidence-ref", "合成用户明确原话"]
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=12)
                assert result.returncode == 0, result.stdout + result.stderr
                cli_ids.append(json.loads(result.stdout)["memory_id"])
            latencies = []
            deadline = time.monotonic() + 90
            while not self.synchronized():
                start = time.monotonic()
                health = self.client.get("/v1/health", timeout=3)
                health.raise_for_status()
                latencies.append(time.monotonic() - start)
                assert time.monotonic() < deadline, "automatic CUDA worker deadline"
                time.sleep(.25)
            health = self.client.get("/v1/health").json()
            assert health["embedding_model_loaded"] and health["embedding_device"] == "cuda" and health["qdrant"] == "ok", health
            for mid in cli_ids:
                vector = self.raw_points("v1", mid, True)[0]["vector"]
                assert len(vector) == 1024 and all(abs(float(value)) < 2 for value in vector)
                assert abs(sum(float(value) ** 2 for value in vector) - 1) < .01
                self.search("简体中文", mid)
            cli_recalls = {}
            for who in ("antigravity", "codex"):
                command = [sys.executable, "-X", "utf8", "-B", str(ROOT / "clients/memory_cli.py"), "--client", who,
                           "--tokens-path", str(self.runtime / "config/client_tokens.json"), "--base-url", self.origin,
                           "search", "--workspace", str(self.workspace), "--query", "简体中文"]
                response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=12)
                assert response.returncode == 0, response.stdout + response.stderr
                recalled = json.loads(response.stdout)
                returned_ids = [item["memory_id"] for item in recalled["items"]]
                assert set(cli_ids) <= set(returned_ids) and recalled.get("degraded_mode") != "keyword_only", recalled
                cli_recalls[who] = {"ids": returned_ids, "degraded_mode": recalled.get("degraded_mode")}
            self.mark("CUDA automatic worker and two CLI identities", {"health": health, "point_ids": cli_ids,
                      "cli_recalls": cli_recalls, "health_during_inference_max_seconds": max(latencies, default=0), "dimensions": 1024})

            if self.args.connectivity_only:
                self.connectivity(cli_ids[0], "简体中文")
                self.receipt["status"] = "已验证"
                return

            benchmark = json.loads((ROOT / "tests/search_benchmark.json").read_text(encoding="utf-8"))
            ids = {key: self.create(title, content, kind="fact")["memory_id"] for key, title, content in benchmark["documents"]}
            wait_for(self.synchronized, 90, "benchmark automatic indexing")
            matches = []
            for query, expected in benchmark["queries"]:
                result = self.search(query, ids[expected])
                matches.append({"query": query, "expected": ids[expected], "top3": [item["memory_id"] for item in result["items"]], "degraded_mode": result.get("degraded_mode")})
            self.mark("real hybrid benchmark 20/20", matches)

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                self.proxy.hold("v2")
                future = pool.submit(self.admin, "/v1/admin/reindex", {"target_generation": "v2"})
                assert self.proxy.entered.wait(30), "reindex never reached real upsert gate"
                try:
                    before = self.client.get("/v1/health", timeout=2).json()
                    assert before["maintenance_mode"] == "reindexing" and before["active_index_generation"] == "v1", before
                    recall = self.search("网关", ids["bm_01"])
                    denied = self.agent("/v1/memory/write", self.payload("冻结测试", "冻结期间不能写入"), expected=503)
                    assert denied["error_code"] == "reindex_in_progress", denied
                finally:
                    self.proxy.release.set()
                switched = future.result(90)
                assert switched["status"] == "switched" and switched["expected_manifest_sha256"] == switched["actual_manifest_sha256"]
            self.mark("real reindex old reads, write freeze and manifest switch", {"during": before, "read_ids": [x["memory_id"] for x in recall["items"]], "switch": switched})

            self.proxy.fail_generation = "v2"
            lagging = self.create("未完成任务恢复", "合成恢复目标：未完成的旧世代索引任务必须保留。", kind="handoff")["memory_id"]
            wait_for(lambda: self.sql("SELECT retry_count FROM index_work WHERE memory_id=? AND status='pending' AND retry_count>0", (lagging,)), 15, "persisted old generation retry")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                self.proxy.hold("crash_v3")
                future = pool.submit(self.admin, "/v1/admin/reindex", {"target_generation": "crash_v3"})
                assert self.proxy.entered.wait(30)
                self.crash()
                self.proxy.release.set()
                try: future.result(10)
                except (httpx.HTTPError, AssertionError): pass
            persisted = self.sql("SELECT status,retry_count,generation FROM index_work WHERE memory_id=?", (lagging,))
            assert any(row["status"] == "pending" and row["generation"] == "v2" for row in persisted), persisted
            self.proxy.fail_drop_generation = "crash_v3"
            self.control("start"); self.login()
            recovered = self.client.get("/v1/health").json()
            assert recovered["maintenance_mode"] == "normal" and recovered["active_index_generation"] == "v2" and recovered["staging_index_generation"] == "crash_v3", recovered
            denied = self.admin("/v1/admin/reindex", {"target_generation": "blocked_v4"}, expected=503)
            assert denied["error_code"] == "staging_cleanup_pending", denied
            self.proxy.fail_drop_generation = None
            self.proxy.fail_generation = None
            self.control("stop"); self.control("start"); self.login()
            wait_for(self.synchronized, 90, "old pending work after process crash")
            recovered_clean = self.client.get("/v1/health").json()
            assert recovered_clean["staging_index_generation"] is None
            assert self.raw.get(f"/collections/{self.prefix}_crash_v3").status_code == 404
            assert self.raw_points("v2", lagging)
            self.mark("process crash, failed staging cleanup and old pending work recovery", {"persisted_work": persisted, "cleanup_pending": recovered, "clean": recovered_clean})

            canary = "CANARY_" + uuid.uuid4().hex
            purged = self.create("永久清除合成目标", canary, kind="handoff")["memory_id"]
            wait_for(self.synchronized, 30, "purge fixture indexing")
            backup = self.admin("/v1/admin/backup/export", {"backup_name": "before-purge"})
            point = self.raw_points("v2", purged, True)[0]
            for generation in ("v1", "orphan_after_backup"):
                path = f"/collections/{self.prefix}_{generation}"
                response = self.raw.get(path)
                if response.status_code == 404:
                    self.raw.put(path, json={"vectors": {"size": 1024, "distance": "Cosine"}}).raise_for_status()
                clone = {"id": point["id"], "vector": point["vector"], "payload": {**point["payload"], "generation": generation}}
                self.raw.put(path + "/points?wait=true", json={"points": [clone]}).raise_for_status()
            self.proxy.fail_deletes = True
            pending = self.admin(f"/v1/admin/memory/{purged}/purge", {"expected_revision": 1, "confirm_purge": f"PURGE {purged} r1"}, expected=202)
            assert pending["status"] == "purge_pending" and not pending["purge_receipt"]["vector_done"], pending
            assert all(self.raw_points(gen, purged) for gen in ("v1", "v2", "orphan_after_backup"))
            response = self.client.get(f"/v1/memory/{purged}", headers={"Authorization": "Bearer " + self.tokens["CODEX_CLIENT_TOKEN"]}, params={"workspace_path": str(self.workspace)})
            assert response.status_code in (404, 410), response.text
            assert purged not in [x["memory_id"] for x in self.search(canary)["items"]]
            self.control("stop"); self.control("start"); self.login()
            assert self.client.get("/v1/health").json()["pending_purges"] == 1
            restored = self.admin("/v1/admin/backup/restore", {"backup_db_path": backup["backup_db_path"]})
            assert restored["status"] == "restored_and_reconciled" and not self.sql("SELECT 1 FROM current_memory WHERE memory_id=?", (purged,))
            self.proxy.fail_deletes = False
            wait_for(lambda: self.client.get("/v1/health").json()["pending_purges"] == 0, 30, "automatic persistent purge retry")
            complete = self.admin(f"/v1/admin/memory/{purged}/purge", {"expected_revision": 1, "confirm_purge": f"PURGE {purged} r1"})
            assert complete["status"] == "purged"
            assert not any(self.raw_points(gen, purged) for gen in ("v1", "v2", "orphan_after_backup"))
            for path in (self.runtime / "data/sqlite/memory_hub.db", self.runtime / "data/sqlite/memory_hub.db-wal", self.runtime / "exports/active_memories.jsonl"):
                if path.exists(): assert canary.encode() not in path.read_bytes(), str(path)
            self.mark("real vector delete failure, pending restart, old backup restore and cross-generation purge", {"pending": pending, "restored": restored, "complete": complete, "generations_verified_empty": ["v1", "v2", "orphan_after_backup"]})

            self.connectivity(ids["bm_01"], "网关")
            self.receipt["status"] = "已验证"
        except Exception as error:
            self.receipt["error"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            self.proxy.release.set()
            if (self.runtime / "service.json").exists():
                try:
                    self.receipt["final_stop"] = self.control("stop")
                except Exception as error:
                    self.receipt["stop_error"] = str(error)
                    self.receipt["status"] = "部分验证"
            self.receipt["fault_events"] = self.proxy.events
            self.proxy.close()
            self.client.close(); self.raw.close()
            self.receipt["gateway_port_released"] = port_free(self.args.port)
            self.receipt["proxy_port_released"] = port_free(self.args.proxy_port)
            self.save()
            print(json.dumps({"runtime": str(self.runtime), "status": self.receipt["status"], "error": self.receipt.get("error")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--port", type=int, default=18779)
    parser.add_argument("--proxy-port", type=int, default=16369)
    parser.add_argument("--qdrant-port", type=int, default=16349)
    parser.add_argument("--connectivity-only", action="store_true", help="Only recheck CUDA/two CLI recalls and Docker offline recovery")
    options = parser.parse_args()
    if not options.container.startswith("smh-030-followup-"):
        parser.error("isolated followup container required")
    Acceptance(options).run()
