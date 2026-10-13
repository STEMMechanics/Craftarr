from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Server, User
from app import web_automation, web_servers


def test_server_home_and_performance_pages_render_storage_panel(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = session_factory()
    user = User(username="maker", password_hash="test", role="admin", enabled=True)
    server = Server(name="Build Lab", directory=str(tmp_path / "server"), service_name="build-lab")
    db.add_all([user, server])
    db.commit()

    app = FastAPI()
    app.include_router(web_automation.router)
    app.include_router(web_servers.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(web_automation, "get_accessible_server", lambda *args: (user, server))
    monkeypatch.setattr(web_servers, "get_accessible_server", lambda *args: (user, server))
    client = TestClient(app)
    try:
        home = client.get(f"/servers/{server.id}")
        assert home.status_code == 200
        assert 'class="server-overview"' in home.text
        assert 'data-server-storage' in home.text

        url = f"/servers/{server.id}/performance"
        full_page = client.get(url)
        assert full_page.status_code == 200
        assert 'class="server-performance-page"' in full_page.text
        assert 'id="metric-cpu"' in full_page.text
        assert 'data-server-storage' in full_page.text
        assert "This shows the whole volume" in full_page.text
        assert "Craftarr" in full_page.text
        assert 'id="notifications-toggle"' in full_page.text
        assert 'id="account-menu-toggle"' in full_page.text

        partial_page = client.get(url, headers={"HX-Request": "true"})
        assert partial_page.status_code == 200
        assert "<!DOCTYPE html>" not in partial_page.text
        assert 'data-server-id="1"' in partial_page.text

        monkeypatch.setattr(
            web_automation,
            "collect_server_storage",
            lambda directory: {
                "used_bytes": 70,
                "free_bytes": 30,
                "total_bytes": 100,
                "percent": 70.0,
            },
        )
        storage = client.get(f"/api/web/servers/{server.id}/storage")
        assert storage.status_code == 200
        assert storage.json() == {
            "used_bytes": 70,
            "free_bytes": 30,
            "total_bytes": 100,
            "percent": 70.0,
        }
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
