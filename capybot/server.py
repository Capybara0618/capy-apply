"""Standalone FastAPI service for Capybot Apply."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import redis.asyncio as redis
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from capybot.apply.postgres import apply_redis_url
from capybot.webui import apply_api


class ApplyEventHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self.clients)
        stale: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:
                stale.append(client)
        if stale:
            async with self._lock:
                for client in stale:
                    self.clients.discard(client)


event_hub = ApplyEventHub()


async def _relay_redis_events() -> None:
    while True:
        client = redis.from_url(apply_redis_url(), decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("apply.events")
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(str(message.get("data") or "{}"))
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await event_hub.broadcast(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(2)
        finally:
            with suppress(Exception):
                await pubsub.aclose()
            with suppress(Exception):
                await client.aclose()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    relay = asyncio.create_task(_relay_redis_events())
    try:
        yield
    finally:
        relay.cancel()
        with suppress(asyncio.CancelledError):
            await relay


app = FastAPI(
    title="Capybot Apply",
    version="0.3.0",
    lifespan=lifespan,
)


@app.exception_handler(apply_api.ApplyAPIError)
async def apply_error_handler(_request, exc: apply_api.ApplyAPIError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.message},
    )


@app.get("/health")
def root_health() -> dict[str, str]:
    return {"status": "ok", "service": "capybot-apply"}


@app.get("/api/apply/health")
def health(force: bool = False) -> dict[str, Any]:
    return apply_api.apply_health_payload(force=force)


@app.get("/api/apply/jobs")
def jobs() -> dict[str, Any]:
    return apply_api.apply_jobs_payload()


@app.get("/api/apply/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    return apply_api.apply_jobs_payload(job_id)


@app.post("/api/apply/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    return apply_api.apply_job_retry(job_id)


@app.get("/api/apply/overview")
def overview() -> dict[str, Any]:
    return apply_api.apply_overview()


@app.get("/api/apply/opportunities")
def opportunities() -> dict[str, Any]:
    return apply_api.apply_opportunities()


@app.get("/api/apply/opportunities/{opportunity_id}")
def opportunity(opportunity_id: str) -> dict[str, Any]:
    return apply_api.apply_opportunity_detail(opportunity_id)


@app.get("/api/apply/opportunities/{opportunity_id}/evidence")
def opportunity_evidence(
    opportunity_id: str,
    evidence_refs: str = "",
    message_ids: str = "",
) -> dict[str, Any]:
    del opportunity_id
    refs = evidence_refs or message_ids
    return apply_api.apply_opportunity_evidence(urlencode({"evidence_refs": refs}))


@app.post("/api/apply/opportunities/{opportunity_id}/reanalyze")
def reanalyze(opportunity_id: str) -> dict[str, Any]:
    return apply_api.apply_reanalyze(opportunity_id)


@app.post("/api/apply/opportunities/{opportunity_id}/fit/reanalyze")
def reanalyze_fit(opportunity_id: str) -> dict[str, Any]:
    return apply_api.apply_fit_reanalyze(opportunity_id)


@app.post("/api/apply/fit/reanalyze/all")
def reanalyze_all_fit(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
    return apply_api.apply_fit_reanalyze_all(limit)


@app.post("/api/apply/opportunities/{opportunity_id}/research")
def research_opportunity(opportunity_id: str) -> dict[str, Any]:
    return apply_api.apply_research_opportunity(opportunity_id)


@app.post("/api/apply/opportunities/{opportunity_id}/refresh-boss")
def refresh_boss_opportunity(opportunity_id: str) -> dict[str, Any]:
    return apply_api.apply_refresh_boss_opportunity(opportunity_id)


@app.get("/api/apply/tasks")
def tasks() -> dict[str, Any]:
    return apply_api.apply_tasks()


@app.get("/api/apply/agent-runs")
def agent_runs() -> dict[str, Any]:
    return apply_api.apply_agent_runs()


@app.get("/api/apply/agent-runs/{run_id}")
def agent_run(run_id: str) -> dict[str, Any]:
    return apply_api.apply_agent_runs(run_id)


@app.get("/api/apply/profile")
def profile() -> dict[str, Any]:
    return apply_api.apply_profile()


@app.post("/api/apply/profile")
def update_profile(payload: dict[str, Any]) -> dict[str, Any]:
    return apply_api.apply_profile_update(
        urlencode({"payload": json.dumps(payload, ensure_ascii=False)})
    )


@app.post("/api/apply/profile/pdf")
def upload_profile_pdf(payload: dict[str, Any]) -> dict[str, Any]:
    return apply_api.apply_profile_upload_pdf(payload)


@app.get("/api/apply/import/report")
def import_report() -> dict[str, Any]:
    return apply_api.apply_import_report()


@app.get("/api/apply/import/progress")
def import_progress() -> dict[str, Any]:
    return apply_api.apply_import_progress()


@app.post("/api/apply/import")
def start_import(days: int = Query(default=30, ge=1, le=90)) -> dict[str, Any]:
    return apply_api.apply_import_start(days)


@app.post("/api/apply/demo")
def start_demo() -> dict[str, Any]:
    return apply_api.apply_demo_start()


@app.get("/api/apply/boss/status")
def boss_status(force: bool = False) -> dict[str, Any]:
    return apply_api.apply_login_status(force=force)


@app.post("/api/apply/boss/login")
async def boss_login() -> dict[str, Any]:
    return await apply_api.apply_begin_login_async()


@app.get("/api/apply/rebuild/status")
def rebuild_status() -> dict[str, Any]:
    return apply_api.apply_rebuild_status()


@app.post("/api/apply/rebuild/start")
def rebuild_start(limit: int = Query(default=500, ge=1, le=2000)) -> dict[str, Any]:
    return apply_api.apply_rebuild_start(limit)


@app.delete("/api/apply/derived/clear")
def clear_derived() -> dict[str, Any]:
    return apply_api.apply_clear_derived()


@app.delete("/api/apply/clear")
async def clear(include_login: bool = False) -> dict[str, Any]:
    return await apply_api.apply_clear_async(include_login)


@app.post("/api/apply/suggestions/{suggestion_id}")
def update_suggestion(
    suggestion_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    status = str(payload.pop("status", ""))
    return apply_api.apply_suggestion_update(suggestion_id, status, payload)


@app.post("/api/apply/reanalyze/all")
def reanalyze_all(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    return apply_api.apply_reanalyze_all(limit)


@app.websocket("/ws/apply")
async def apply_events(websocket: WebSocket) -> None:
    await event_hub.connect(websocket)
    try:
        await websocket.send_json({"event": "apply_connected"})
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await event_hub.disconnect(websocket)


_dist = Path(__file__).resolve().parent / "web" / "dist"
if _dist.exists():
    assets = _dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        candidate = (_dist / path).resolve()
        if path and candidate.is_file() and _dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "capybot.server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


if __name__ == "__main__":
    main()
