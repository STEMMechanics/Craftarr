from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Server, User
from app import web_automation


def test_performance_page_renders_full_and_htmx_views(monkeypatch, tmp_path):
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
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(web_automation, "get_accessible_server", lambda *args: (user, server))
    client = TestClient(app)
    try:
        url = f"/servers/{server.id}/performance"
        full_page = client.get(url)
        assert full_page.status_code == 200
        assert 'class="server-performance-page"' in full_page.text
        assert 'id="metric-cpu"' in full_page.text
        assert "STEMMechanics" in full_page.text
        assert 'id="notifications-toggle"' in full_page.text
        assert 'id="account-menu-toggle"' in full_page.text

        partial_page = client.get(url, headers={"HX-Request": "true"})
        assert partial_page.status_code == 200
        assert "<!DOCTYPE html>" not in partial_page.text
        assert 'data-server-id="1"' in partial_page.text
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
