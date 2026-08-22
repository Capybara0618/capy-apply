from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from capybot import server
from capybot.server import app
from capybot.webui import apply_api


def test_standalone_fastapi_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "capybot-apply",
    }


def test_apply_websocket_connects_without_generic_chat_gateway() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/apply") as websocket:
            assert websocket.receive_json() == {"event": "apply_connected"}
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"


def test_opportunity_evidence_accepts_canonical_evidence_refs(monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        server.apply_api,
        "apply_opportunity_evidence",
        lambda query_string: captured.append(query_string) or {"messages": [{"message_id": "m1"}]},
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/apply/opportunities/opp-1/evidence",
            params={"evidence_refs": "boss_message:m1,boss_job_snapshot:j1"},
        )

    assert response.status_code == 200
    assert response.json()["messages"][0]["message_id"] == "m1"
    assert captured == [
        "evidence_refs=boss_message%3Am1%2Cboss_job_snapshot%3Aj1",
    ]


def test_opportunity_research_delegates_to_apply_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        server.apply_api,
        "apply_research_opportunity",
        lambda opportunity_id: {"job_id": f"research:{opportunity_id}"},
    )

    with TestClient(app) as client:
        response = client.post("/api/apply/opportunities/opp-1/research")

    assert response.status_code == 200
    assert response.json() == {"job_id": "research:opp-1"}


def test_opportunity_boss_refresh_delegates_to_apply_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        server.apply_api,
        "apply_refresh_boss_opportunity",
        lambda opportunity_id: {"job_id": f"boss-refresh:{opportunity_id}"},
    )

    with TestClient(app) as client:
        response = client.post("/api/apply/opportunities/opp-1/refresh-boss")

    assert response.status_code == 200
    assert response.json() == {"job_id": "boss-refresh:opp-1"}


def test_reanalyze_all_fit_delegates_with_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        server.apply_api,
        "apply_fit_reanalyze_all",
        lambda limit: {"limit": limit},
    )

    with TestClient(app) as client:
        response = client.post("/api/apply/fit/reanalyze/all?limit=40")

    assert response.status_code == 200
    assert response.json() == {"limit": 40}


def test_demo_uses_post_and_delegates_to_isolated_loader(monkeypatch) -> None:
    monkeypatch.setattr(
        server.apply_api,
        "apply_demo_start",
        lambda: {"ok": True, "account_id": "capybot_demo"},
    )

    with TestClient(app) as client:
        post_response = client.post("/api/apply/demo")

    assert set(app.openapi()["paths"]["/api/apply/demo"]) == {"post"}
    assert post_response.status_code == 200
    assert post_response.json()["account_id"] == "capybot_demo"


def test_failed_job_fit_can_be_retried(monkeypatch) -> None:
    monkeypatch.setattr(apply_api, "_require_postgres", lambda: None)
    jobs = type(
        "Jobs",
        (),
        {
            "get": staticmethod(
                lambda _job_id: {
                    "job_type": "analyze_job_fit",
                    "target_id": "opp-1",
                    "payload": {"opportunity_id": "opp-1"},
                }
            )
        },
    )()
    monkeypatch.setattr(apply_api, "ApplyJobStore", lambda: jobs)
    monkeypatch.setattr(
        apply_api,
        "_enqueue_job_fit",
        lambda opportunity_id, retry_of=None: {
            "opportunity_id": opportunity_id,
            "retry_of": retry_of,
        },
    )

    result = apply_api.apply_job_retry("job-failed")

    assert result == {"opportunity_id": "opp-1", "retry_of": "job-failed"}


def test_overview_does_not_disguise_transient_read_failure_as_empty_account(
    monkeypatch,
) -> None:
    failing_store = type(
        "FailingStore",
        (),
        {"overview": staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("busy")))},
    )()
    monkeypatch.setattr(apply_api, "_schedule_apply_maintenance", lambda: None)
    monkeypatch.setattr(apply_api, "_store", lambda: failing_store)
    monkeypatch.setattr(
        apply_api,
        "apply_health_payload",
        lambda force=False: {"postgres": {"ok": True}},
    )

    with pytest.raises(apply_api.ApplyAPIError) as exc_info:
        apply_api.apply_overview()

    assert exc_info.value.status == 503
    assert "稍后重试" in exc_info.value.message


def test_demo_import_progress_uses_completed_analysis_job(monkeypatch) -> None:
    analysis_job = {
        "id": "job-demo",
        "job_type": "trigger_import_analysis",
        "status": "ok",
        "progress_current": 6,
        "progress_total": 6,
        "progress_percent": 100,
        "message": "DecisionRouter 已完成增量分流。",
    }

    class Jobs:
        @staticmethod
        def latest_by_type(job_type: str):
            return analysis_job if job_type == "trigger_import_analysis" else None

    store = type(
        "DemoStore",
        (),
        {"current_account": staticmethod(lambda: {"source": "demo_fixture"})},
    )()
    monkeypatch.setattr(apply_api, "ApplyJobStore", Jobs)
    monkeypatch.setattr(apply_api, "_store", lambda: store)

    progress = apply_api.apply_import_progress()

    assert progress["status"] == "ok"
    assert progress["phase"] == "trigger_import_analysis"
    assert progress["percent"] == 100
    assert progress["current"] == progress["total"] == 6
