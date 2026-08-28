from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import UNIT_ID


def test_health_ready_metrics_search_and_report(settings) -> None:
    app = create_app(settings.model_copy(update={"compatibility_api_key": "test-key"}))
    with TestClient(app) as client:
        client.headers["x-api-key"] = "test-key"
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 200
        assert "sales" in client.get("/api/metrics").json()
        search = client.get("/api/documentation/search", params={"q": "выручка"})
        assert search.status_code == 200
        response = client.post(
            "/api/reports",
            json={
                "query": (
                    "Покажи выручку и количество заказов по дням с 1 по 10 мая 2026 "
                    f"по юниту {UNIT_ID}"
                ),
                "output_format": "table",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ready"
        assert len(data["rows"]) == 10
        assert data["totals"]["sales"] == 111250.0
        assert data["execution"]["dodo_requests_count"] == 1


def test_clarification_unsupported_and_downloads(settings) -> None:
    app = create_app(settings.model_copy(update={"compatibility_api_key": "test-key"}))
    with TestClient(app) as client:
        client.headers["x-api-key"] = "test-key"
        clarification = client.post(
            "/api/reports",
            json={"query": f"Покажи выручку по юниту {UNIT_ID}"},
        ).json()
        assert clarification["status"] == "needs_clarification"
        unsupported = client.post(
            "/api/reports",
            json={"query": f"Покажи погоду за май 2026 по юниту {UNIT_ID}"},
        ).json()
        assert unsupported["status"] == "unsupported"
        for file_format in ("csv", "xlsx"):
            report = client.post(
                "/api/reports",
                json={
                    "query": f"Покажи выручку за май 2026 по юниту {UNIT_ID}",
                    "output_format": file_format,
                },
            ).json()
            download = client.get(report["download"]["url"])
            assert download.status_code == 200
            assert download.content
        assert client.get("/api/reports/not-a-valid-id/download").status_code == 404
        assert client.get("/api/reports/..%2Fsecret/download").status_code in {404, 422}


def test_compatibility_api_requires_key(settings) -> None:
    protected = create_app(settings.model_copy(update={"compatibility_api_key": "test-key"}))
    with TestClient(protected) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/metrics").status_code == 401
        assert client.get("/api/metrics", headers={"x-api-key": "wrong"}).status_code == 401
        assert client.get("/api/metrics", headers={"x-api-key": "test-key"}).status_code == 200

    disabled = create_app(settings.model_copy(update={"compatibility_api_key": ""}))
    with TestClient(disabled) as client:
        assert client.get("/api/metrics").status_code == 503


def test_home_page_directs_to_telegram_and_privacy(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Telegram" in response.text
        assert "/privacy" in response.text

        privacy = client.get("/privacy")
        assert privacy.status_code == 200
        assert "конфиденциальности" in privacy.text.lower()
