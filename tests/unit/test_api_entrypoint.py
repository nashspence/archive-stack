from typing import Any

from riverhog_api import app as api_app


def test_api_entrypoint_listens_on_the_container_network(monkeypatch: Any) -> None:
    invocation: dict[str, Any] = {}

    def fake_run(application: str, **options: Any) -> None:
        invocation.update({"application": application, **options})

    monkeypatch.setattr(api_app.uvicorn, "run", fake_run)

    api_app.main()

    assert invocation == {
        "application": "riverhog_api.app:create_app",
        "factory": True,
        "host": "0.0.0.0",
        "port": 8000,
        "reload": False,
    }
