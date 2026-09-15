from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from dq.demo.server import DemoApplication


@dataclass
class FakeService:
    loaded_periods: list[str | None] = field(default_factory=list)
    run_periods: list[str] = field(default_factory=list)

    def load(self, period: str | None = None) -> dict[str, object]:
        self.loaded_periods.append(period)
        return {"period": period or "2026-09", "status": "ready"}

    def run(self, period: str) -> dict[str, object]:
        self.run_periods.append(period)
        return {"period": period, "status": "COMPLETED"}

    def health(self) -> dict[str, object]:
        return {"status": "healthy", "database": "PostgreSQL"}


def test_get_state_passes_canonical_period_to_service() -> None:
    service = FakeService()
    app = DemoApplication(service)

    response = app.dispatch("GET", "/api/nightly-load?period=2026-09", {}, b"")

    assert response.status == 200
    assert json.loads(response.body) == {"period": "2026-09", "status": "ready"}
    assert service.loaded_periods == ["2026-09"]


def test_run_requires_same_origin_action_header() -> None:
    service = FakeService()
    app = DemoApplication(service)

    response = app.dispatch(
        "POST",
        "/api/nightly-load/run",
        {"content-type": "application/json"},
        b'{"period":"2026-09"}',
    )

    assert response.status == 403
    assert service.run_periods == []


def test_run_accepts_only_period_payload_and_fixed_action() -> None:
    service = FakeService()
    app = DemoApplication(service)

    response = app.dispatch(
        "POST",
        "/api/nightly-load/run",
        {
            "content-type": "application/json",
            "x-dq-demo-action": "run-nightly-load",
        },
        b'{"period":"2026-09"}',
    )

    assert response.status == 200
    assert json.loads(response.body)["status"] == "COMPLETED"
    assert service.run_periods == ["2026-09"]


def test_run_rejects_extra_payload_fields() -> None:
    service = FakeService()
    app = DemoApplication(service)

    response = app.dispatch(
        "POST",
        "/api/nightly-load/run",
        {
            "content-type": "application/json",
            "x-dq-demo-action": "run-nightly-load",
        },
        b'{"period":"2026-09","sql":"drop table finding"}',
    )

    assert response.status == 400
    assert service.run_periods == []


def test_unknown_api_route_is_not_exposed() -> None:
    response = DemoApplication(FakeService()).dispatch("GET", "/api/admin", {}, b"")

    assert response.status == 404


def test_static_surface_serves_only_allowlisted_assets(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>demo</h1>", encoding="utf-8")
    (tmp_path / "responsive.css").write_text(".demo {}", encoding="utf-8")
    app = DemoApplication(FakeService(), tmp_path)

    home = app.dispatch("GET", "/", {}, b"")
    responsive = app.dispatch("GET", "/responsive.css", {}, b"")
    traversal = app.dispatch("GET", "/../.env", {}, b"")

    assert home.status == 200
    assert home.body == b"<h1>demo</h1>"
    assert responsive.status == 200
    assert responsive.content_type == "text/css; charset=utf-8"
    assert traversal.status == 404
