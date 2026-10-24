from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Server
from app import web_files, web_players, web_plugins


def make_app(db, user, server):
    app = FastAPI()
    app.include_router(web_files.router)
    app.include_router(web_players.router)
    app.include_router(web_plugins.router)
    app.dependency_overrides[get_db] = lambda: db
    for module in (web_files, web_players, web_plugins):
        module.get_accessible_server = lambda *args, _user=user, _server=server: (_user, _server)
    return app


def test_files_breadcrumb_and_topbar_player_link_render(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    db = session_factory()
    server = Server(name="Build Lab", directory=str(tmp_path / "server"), service_name="build-lab")
    (tmp_path / "server" / "world" / "region").mkdir(parents=True)
    db.add(server)
    db.commit()
    user = SimpleNamespace(
        id=1,
        username="maker",
        role="admin",
        role_name="Administrator",
        enabled=True,
        access_role=None,
        servers=[server],
        remote_servers=[],
        hub_servers=[],
        can=lambda _permission: True,
    )
    monkeypatch.setattr(web_players, "get_online_players", lambda _server_id: {"Alex", "Bea"})
    app = make_app(db, user, server)
    client = TestClient(app)

    try:
        response = client.get(f"/servers/{server.id}/files?path=world/region")
        assert response.status_code == 200
        assert 'class="file-path-root"' in response.text
        assert 'href="/servers/1/files?path=world"' in response.text
        assert 'href="/servers/1/files?path=world/region"' in response.text
        assert 'id="topbar-player-count-link"' in response.text
        assert 'href="/servers/1/players"' in response.text

        online_count = client.get(f"/api/web/servers/{server.id}/players/online-count")
        assert online_count.status_code == 200
        assert online_count.json() == {"online_count": 2}

        players = client.get(f"/servers/{server.id}/players")
        assert players.status_code == 200
        assert 'value="last-online-desc" selected' in players.text
    finally:
        db.close()
        engine.dispose()
