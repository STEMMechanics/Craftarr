"""Add single-role permission management.

Revision ID: 30c8d40e2b91
Revises: c41f1e9a7320
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "30c8d40e2b91"
down_revision: Union[str, Sequence[str], None] = "c41f1e9a7320"
branch_labels = None
depends_on = None


PERMISSIONS = {
    "servers.view": "View assigned servers",
    "servers.view_all": "View every server",
    "servers.create": "Create and import servers",
    "servers.delete": "Delete servers",
    "servers.control": "Start, stop, and restart servers",
    "servers.properties": "Edit server properties and versions",
    "console.view": "View server consoles",
    "console.command": "Send console commands",
    "players.view": "View players and bans",
    "players.manage": "Manage players, whitelist, and bans",
    "plugins.view": "View installed plugins",
    "plugins.manage": "Install, configure, and remove plugins",
    "files.view": "Browse and download server files",
    "files.manage": "Upload, edit, move, and delete files",
    "backups.view": "View and download backups",
    "backups.manage": "Create, restore, delete, and schedule backups",
    "automation.manage": "Manage schedules and automation",
    "system.view": "View system information",
    "system.manage": "Control services and application updates",
    "users.manage": "Create and manage users",
    "roles.manage": "Create and manage roles",
    "settings.manage": "Manage global console settings",
}

USER_PERMISSIONS = {
    "servers.view", "servers.control", "servers.properties", "console.view", "console.command",
    "players.view", "players.manage", "plugins.view", "files.view",
    "files.manage", "backups.view", "backups.manage",
}


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(80), nullable=False, unique=True),
        sa.Column("label", sa.String(160), nullable=False),
    )
    op.create_index("ix_permissions_key", "permissions", ["key"], unique=True)
    op.create_table(
        "access_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_access_roles_name", "access_roles", ["name"], unique=True)
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["access_roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("role_id", sa.Integer(), nullable=True))
        batch.create_index("ix_users_role_id", ["role_id"])
        batch.create_foreign_key("fk_users_role_id", "access_roles", ["role_id"], ["id"], ondelete="RESTRICT")

    connection = op.get_bind()
    permissions = sa.table("permissions", sa.column("id"), sa.column("key"), sa.column("label"))
    roles = sa.table("access_roles", sa.column("id"), sa.column("name"), sa.column("description"), sa.column("system"))
    links = sa.table("role_permissions", sa.column("role_id"), sa.column("permission_id"))
    users = sa.table("users", sa.column("role"), sa.column("role_id"))

    connection.execute(permissions.insert(), [{"key": key, "label": label} for key, label in PERMISSIONS.items()])
    connection.execute(roles.insert(), [
        {"name": "Administrator", "description": "Full access to Craftarr Console", "system": True},
        {"name": "User", "description": "Migrated pre-0.2.0 user permissions", "system": True},
    ])
    role_rows = {row.name: row.id for row in connection.execute(sa.select(roles.c.id, roles.c.name))}
    permission_rows = {row.key: row.id for row in connection.execute(sa.select(permissions.c.id, permissions.c.key))}
    connection.execute(links.insert(), [
        {"role_id": role_rows["Administrator"], "permission_id": permission_id}
        for permission_id in permission_rows.values()
    ] + [
        {"role_id": role_rows["User"], "permission_id": permission_rows[key]}
        for key in USER_PERMISSIONS
    ])
    connection.execute(users.update().where(users.c.role == "admin").values(role_id=role_rows["Administrator"]))
    connection.execute(users.update().where(users.c.role != "admin").values(role_id=role_rows["User"]))
    with op.batch_alter_table("users") as batch:
        batch.alter_column("role_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_role_id", type_="foreignkey")
        batch.drop_index("ix_users_role_id")
        batch.drop_column("role_id")
    op.drop_table("role_permissions")
    op.drop_index("ix_access_roles_name", table_name="access_roles")
    op.drop_table("access_roles")
    op.drop_index("ix_permissions_key", table_name="permissions")
    op.drop_table("permissions")
