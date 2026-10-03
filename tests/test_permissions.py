from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AccessRole, Permission, User
from app.permissions import has_permission


def test_user_has_only_direct_permissions_from_single_role():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        view = Permission(key="servers.view", label="View assigned servers")
        manage = Permission(key="servers.delete", label="Delete servers")
        viewer = AccessRole(name="Viewer", permissions=[view])
        db.add_all([viewer, manage])
        db.flush()
        user = User(
            username="viewer",
            password_hash="hash",
            role="user",
            role_id=viewer.id,
            enabled=True,
        )
        db.add(user)
        db.commit()

        assert has_permission(user, "servers.view") is True
        assert has_permission(user, "servers.delete") is False
        assert user.access_role is viewer
        assert not hasattr(viewer, "parent_role")
    finally:
        db.close()
        engine.dispose()


def test_disabled_user_has_no_permissions():
    user = User(username="disabled", password_hash="hash", role="admin", enabled=False)

    assert has_permission(user, "servers.view") is False


def test_administrator_role_implicitly_has_every_permission():
    administrator = AccessRole(name="Administrator", permissions=[])
    user = User(
        username="admin",
        password_hash="hash",
        role="admin",
        enabled=True,
        access_role=administrator,
    )

    assert has_permission(user, "servers.delete") is True
    assert has_permission(user, "roles.manage") is True
    assert has_permission(user, "a.future.permission") is True


def test_view_all_servers_also_grants_server_view():
    view_all = Permission(key="servers.view_all", label="View every server")
    role = AccessRole(name="Global viewer", permissions=[view_all])
    user = User(
        username="global-viewer",
        password_hash="hash",
        role="user",
        enabled=True,
        access_role=role,
    )

    assert has_permission(user, "servers.view") is True
