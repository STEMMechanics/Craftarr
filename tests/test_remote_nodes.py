import base64
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.database import Base, get_db
from app.instance_identity import get_node_id, server_reference
from app.main import app
from app.models import NodeAccessToken, RemoteNode, RemoteServer, Server, User
from app.node_gateway import (
    _permitted,
    _proxy_user_claim,
    _required_permissions,
    _rewrite_remote_html,
)
from app.node_security import generate_node_token, verify_node_token
from app.remote_nodes import RemoteNodeError, normalize_remote_url, sync_node_servers
from app.schemas import NodeServerOut
from app.web_users import _gateway_user


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _remote_server(server_id: int, name: str) -> NodeServerOut:
    return NodeServerOut(
        server_id=server_id,
        name=name,
        minecraft_version="1.21.1",
        paper_build="100",
        memory="2G",
        min_memory="1G",
        port=25565 + server_id,
        enabled=True,
    )


def test_regenerating_node_token_invalidates_the_previous_token(db):
    previous = generate_node_token(db)
    assert verify_node_token(db, previous)

    replacement = generate_node_token(db)

    assert replacement != previous
    assert verify_node_token(db, replacement)
    assert not verify_node_token(db, previous)
    assert db.query(NodeAccessToken).count() == 1


def test_node_inventory_api_requires_token_and_disables_caching(db):
    db.add(Server(
        name="Survival",
        directory="minecraft-servers/survival",
        service_name="craftarr-survival",
    ))
    db.commit()
    token = generate_node_token(db)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        assert client.get("/api/node/identity").status_code == 401

        identity = client.get(
            "/api/node/identity",
            headers={"Authorization": f"Bearer {token}"},
        )
        inventory = client.get(
            "/api/node/servers",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert identity.status_code == 200
        assert identity.headers["cache-control"] == "no-store"
        assert inventory.status_code == 200
        assert inventory.json()[0]["name"] == "Survival"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_gateway_user_is_limited_to_server_routes_and_asserted_access(db):
    server = Server(
        name="Survival",
        directory="minecraft-servers/survival",
        service_name="craftarr-survival",
    )
    db.add(server)
    db.commit()
    token = generate_node_token(db)
    node_id = str(uuid.uuid4())
    claim = base64.urlsafe_b64encode(json.dumps({
        "username": "alice",
        "permissions": ["servers.view", "servers.control"],
        "servers": [server.id],
        "hub_servers": [{
            "id": f"{node_id}:9",
            "name": "Other",
            "node_name": "Second node",
            "minecraft_version": "1.21.1",
            "memory": "2G",
        }],
    }).encode()).decode()

    def request_for(path: str) -> Request:
        return Request({
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"x-craftarr-user", claim.encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("craftarr.example.test", 443),
            "session": {},
        })

    gateway_user = _gateway_user(request_for("/api/web/servers/1/start"), db)
    assert gateway_user is not None
    assert gateway_user.username == "alice"
    assert gateway_user.can("servers.control")
    assert gateway_user.servers == [server]
    assert gateway_user.hub_servers[0].id == f"{node_id}:9"
    assert _gateway_user(request_for("/settings"), db) is None


def test_instance_identity_is_persistent_and_server_reference_is_namespaced(db):
    node_id = get_node_id(db)

    assert uuid.UUID(node_id)
    assert get_node_id(db) == node_id
    assert server_reference(node_id, 7) == f"{node_id}:7"


def test_inventory_refresh_keeps_assignments_for_stable_server_ids(db):
    node_id = str(uuid.uuid4())
    node = RemoteNode(
        node_id=node_id,
        name="Remote",
        base_url="https://remote.example.test",
        token_ciphertext="encrypted",
    )
    db.add(node)
    db.flush()
    sync_node_servers(db, node, [_remote_server(1, "Survival")])
    db.commit()

    user = User(username="alice", password_hash="hash", role="user", enabled=True)
    user.remote_servers = db.query(RemoteServer).filter_by(node_id=node_id).all()
    db.add(user)
    db.commit()

    sync_node_servers(db, node, [_remote_server(1, "Survival 2")])
    db.commit()
    db.expire_all()

    user = db.query(User).filter_by(username="alice").one()
    server = user.remote_servers[0]
    assert server.server_ref == f"{node_id}:1"
    assert server.name == "Survival 2"

    sync_node_servers(db, db.query(RemoteNode).filter_by(node_id=node_id).one(), [])
    db.commit()
    db.expire_all()
    user = db.query(User).filter_by(username="alice").one()
    assert user.remote_servers == []


def test_remote_url_requires_tls_except_for_loopback():
    assert normalize_remote_url("https://REMOTE.example.test/console/") == (
        "https://remote.example.test/console"
    )
    assert normalize_remote_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"

    with pytest.raises(RemoteNodeError, match="HTTPS"):
        normalize_remote_url("http://remote.example.test")
    with pytest.raises(RemoteNodeError, match="valid Craftarr URL"):
        normalize_remote_url("https://remote.example.test:notaport")


def test_gateway_maps_remote_operations_and_rewrites_server_refs():
    node_id = str(uuid.uuid4())
    path = f"/api/web/servers/{node_id}:42/start"

    assert _required_permissions(path, "POST") == {"servers.control"}
    assert _required_permissions(
        f"/api/web/servers/{node_id}:42/port-warning", "GET"
    ) == {"servers.properties"}
    assert _required_permissions(
        f"/api/web/servers/{node_id}:42/paper/update-status", "GET"
    ) == {"servers.view"}
    assert _required_permissions(f"/servers/{node_id}:42", "GET") == {"servers.view"}

    path = f"/api/web/servers/{node_id}:42/plugins/monitoring/global"
    required = _required_permissions(path, "POST")
    user = SimpleNamespace(
        enabled=True,
        role="user",
        access_role=None,
        can=lambda permission: permission in {"plugins.manage", "settings.manage"},
    )
    assert _permitted(user, required, path)
    user.can = lambda permission: permission == "plugins.manage"
    assert not _permitted(user, required, path)

    html = (
        b'<a href="/servers/42">Open</a> '
        b'data-server-id="42" active_server_id=42'
    )
    assert _rewrite_remote_html(html, node_id) == (
        f'<a href="/servers/{node_id}:42">Open</a> '
        f'data-server-id="{node_id}:42" active_server_id={node_id}:42'
    ).encode()


def test_proxy_claim_carries_only_the_hub_users_server_menu(db):
    hub_node_id = get_node_id(db)
    current_node_id = str(uuid.uuid4())
    other_node_id = str(uuid.uuid4())
    current_node = RemoteNode(
        node_id=current_node_id,
        name="Current remote",
        base_url="https://current.example.test",
        token_ciphertext="encrypted",
        servers=[RemoteServer(
            server_id=1,
            name="Current server",
            memory="2G",
            min_memory="1G",
            port=25566,
            enabled=True,
        )],
    )
    other_node = RemoteNode(
        node_id=other_node_id,
        name="Other remote",
        base_url="https://other.example.test",
        token_ciphertext="encrypted",
        servers=[RemoteServer(
            server_id=2,
            name="Other server",
            memory="2G",
            min_memory="1G",
            port=25567,
            enabled=True,
        )],
    )
    local_server = Server(
        name="Local server",
        directory="minecraft-servers/local",
        service_name="craftarr-local",
    )
    user = User(username="admin", password_hash="hash", role="admin", enabled=True)
    db.add_all([current_node, other_node, local_server, user])
    db.commit()
    db.refresh(user)

    claim = _proxy_user_claim(
        user,
        current_node_id,
        [1],
        hub_node_id,
        db,
    )
    payload = json.loads(base64.urlsafe_b64decode(claim))
    choices = {item["id"]: item["name"] for item in payload["hub_servers"]}

    assert payload["servers"] == [1]
    assert choices == {
        f"{hub_node_id}:{local_server.id}": "Local server",
        f"{other_node_id}:2": "Other server",
    }
