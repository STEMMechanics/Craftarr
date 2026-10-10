from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base


user_server_access = Table(
    "user_server_access",
    Base.metadata,

    Column(
        "user_id",
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),

    Column(
        "server_id",
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("access_roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


user_remote_server_access = Table(
    "user_remote_server_access",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("remote_server_id", Integer, ForeignKey("remote_servers.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(Base):
    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False, index=True)
    label = Column(String(160), nullable=False)


class AccessRole(Base):
    __tablename__ = "access_roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)
    system = Column(Boolean, nullable=False, default=False)
    permissions = relationship("Permission", secondary=role_permissions, lazy="selectin")
    users = relationship("User", back_populates="access_role")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)

    username = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )

    password_hash = Column(
        String(255),
        nullable=False,
    )

    # admin or user
    role = Column(
        String(16),
        nullable=False,
        default="user",
    )

    role_id = Column(
        Integer,
        ForeignKey("access_roles.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    access_role = relationship("AccessRole", back_populates="users")

    def can(self, permission: str) -> bool:
        if self.access_role is None:
            return self.role == "admin"
        if self.access_role.name == "Administrator":
            return True
        return any(item.key == permission for item in self.access_role.permissions)

    @property
    def role_name(self) -> str:
        return self.access_role.name if self.access_role else self.role.capitalize()

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
    )

    email = Column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )

    totp_secret = Column(
        String(64),
        nullable=True,
    )

    totp_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    must_change_password = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    servers = relationship(
        "Server",
        secondary=user_server_access,
        back_populates="users",
    )
    remote_servers = relationship(
        "RemoteServer",
        secondary=user_remote_server_access,
        back_populates="users",
    )


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)

    name = Column(
        String(100),
        unique=True,
        nullable=False,
    )

    directory = Column(
        String(500),
        unique=True,
        nullable=False,
    )

    service_name = Column(
        String(150),
        unique=True,
        nullable=False,
    )

    minecraft_version = Column(
        String(40),
        nullable=True,
    )

    paper_build = Column(
        String(40),
        nullable=True,
    )

    port = Column(
        Integer,
        nullable=False,
        default=25565,
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
    )

    memory = Column(
        String(20),
        nullable=False,
        default="2G",
    )

    min_memory = Column(
        String(20),
        nullable=False,
        default="2G",
    )

    jar_name = Column(
        String(255), nullable=False, default="paper.jar"
    )

    java_args = Column(
        String(1000), nullable=False, default=""
    )

    stop_commands = Column(
        Text, nullable=False, default=""
    )

    java_path = Column(
        String(500), nullable=False, default="java"
    )

    process_backend = Column(
        String(20), nullable=False, default="subprocess"
    )

    plugins_dirty = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    plugin_session_pid = Column(
        Integer,
        nullable=True,
    )

    users = relationship(
        "User",
        secondary=user_server_access,
        back_populates="servers",
    )

class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)

    key = Column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    value = Column(
        Text,
        nullable=True,
    )


class CraftarrInstance(Base):
    """Persistent identity for this Craftarr installation."""

    __tablename__ = "craftarr_instance"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_craftarr_instance_singleton"),
    )

    id = Column(Integer, primary_key=True)
    node_id = Column(String(36), unique=True, nullable=False)


class NodeAccessToken(Base):
    """The one active secret accepted from a trusted management console."""

    __tablename__ = "node_access_tokens"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_node_access_tokens_singleton"),
    )

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False)
    token_ciphertext = Column(Text, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


class RemoteNode(Base):
    """A Craftarr instance managed through this console."""

    __tablename__ = "remote_nodes"

    id = Column(Integer, primary_key=True)
    node_id = Column(String(36), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False, unique=True)
    base_url = Column(String(1000), nullable=False, unique=True)
    token_ciphertext = Column(Text, nullable=False)
    app_version = Column(String(40), nullable=True)
    last_connected_at = Column(DateTime, nullable=True)
    last_error = Column(String(255), nullable=True)
    outage_started_at = Column(DateTime, nullable=True)
    offline_alert_sent = Column(Boolean, nullable=False, default=False)
    servers = relationship(
        "RemoteServer",
        back_populates="node",
        cascade="all, delete-orphan",
    )


class RemoteServer(Base):
    """Cached inventory for a server hosted by a linked Craftarr node."""

    __tablename__ = "remote_servers"
    __table_args__ = (
        UniqueConstraint("node_id", "server_id", name="uq_remote_servers_node_server"),
    )

    record_id = Column("id", Integer, primary_key=True)
    node_id = Column(String(36), ForeignKey("remote_nodes.node_id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = Column(Integer, nullable=False)
    name = Column(String(100), nullable=False)
    minecraft_version = Column(String(40), nullable=True)
    paper_build = Column(String(40), nullable=True)
    memory = Column(String(20), nullable=False, default="2G")
    min_memory = Column(String(20), nullable=False, default="2G")
    port = Column(Integer, nullable=False, default=25565)
    enabled = Column(Boolean, nullable=False, default=True)

    node = relationship("RemoteNode", back_populates="servers")
    users = relationship(
        "User",
        secondary=user_remote_server_access,
        back_populates="remote_servers",
    )

    @property
    def id(self) -> str:
        return f"{self.node_id}:{self.server_id}"

    @property
    def server_ref(self) -> str:
        return self.id

    @property
    def node_name(self) -> str:
        return self.node.name if self.node else "Remote"


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)

    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    token_hash = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )

    expires_at = Column(
        DateTime,
        nullable=False,
    )

    used_at = Column(
        DateTime,
        nullable=True,
    )

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id = Column(
        Integer,
        primary_key=True,
    )

    user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    code_hash = Column(
        String(64),
        nullable=False,
    )

    used_at = Column(
        DateTime,
        nullable=True,
    )

class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id = Column(Integer, primary_key=True)

    server_id = Column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    filename = Column(
        String(255),
        nullable=True,
    )

    label = Column(
        String(255),
        nullable=True,
    )

    status = Column(
        String(32),
        nullable=False,
        default="queued",
    )

    progress = Column(
        Integer,
        nullable=False,
        default=0,
    )

    message = Column(
        String(255),
        nullable=True,
    )

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    started_at = Column(
        DateTime,
        nullable=True,
    )

    finished_at = Column(
        DateTime,
        nullable=True,
    )


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type = Column(String(20), nullable=False)
    name = Column(String(100), nullable=False)
    command = Column(String(500), nullable=True)
    interval_minutes = Column(Integer, nullable=False)
    frequency = Column(String(20), nullable=True)
    run_hour = Column(Integer, nullable=True)
    run_weekday = Column(Integer, nullable=True)
    cron_expression = Column(String(100), nullable=True)
    schedule_timezone = Column(String(100), nullable=True)
    remote_destination = Column(String(500), nullable=True)
    remote_retention_count = Column(Integer, nullable=True)
    retention_count = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    next_run_at = Column(DateTime, nullable=False, index=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PendingIdleRestart(Base):
    """A durable restart request that waits for the server to become empty."""

    __tablename__ = "pending_idle_restarts"

    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    requested_by_username = Column(String(64), nullable=False)
    reason = Column(String(255), nullable=False, default="Plugin update")
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    empty_since = Column(DateTime, nullable=True)
    last_error = Column(String(255), nullable=True)


class PendingAutomaticUpdate(Base):
    """A scheduled update waiting for the server to become player-free."""

    __tablename__ = "pending_automatic_updates"

    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    task_id = Column(Integer, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    empty_since = Column(DateTime, nullable=True)
    restart_after_update = Column(Boolean, nullable=False, default=True)
    last_error = Column(String(255), nullable=True)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    detail = Column(String(1000), nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)


class ServerMetric(Base):
    __tablename__ = "server_metrics"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("servers.id", ondelete="CASCADE"), nullable=False, index=True)
    recorded_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    running = Column(Boolean, nullable=False)
    cpu_percent = Column(Integer, nullable=False, default=0)
    memory_bytes = Column(Integer, nullable=False, default=0)
    player_count = Column(Integer, nullable=True)
    uptime_seconds = Column(Integer, nullable=True)


class UpstreamUpdateCache(Base):
    __tablename__ = 'upstream_update_cache'
    key = Column(String(200), primary_key=True)
    payload = Column(Text, nullable=False)
    checked_at = Column(DateTime, nullable=False)
    error = Column(String(255), nullable=True)


class ServerUpdateCheck(Base):
    __tablename__ = 'server_update_checks'
    server_id = Column(Integer, ForeignKey('servers.id', ondelete='CASCADE'), primary_key=True)
    component = Column(String(255), primary_key=True)
    payload = Column(Text, nullable=False)


class UpdateNotification(Base):
    __tablename__ = 'update_notifications'
    server_id = Column(Integer, ForeignKey('servers.id', ondelete='CASCADE'), primary_key=True)
    component = Column(String(200), primary_key=True)
    recipient = Column(String(255), primary_key=True)
    version = Column(String(200), primary_key=True)
    sent_at = Column(DateTime, nullable=False)


class UpdateMonitorLease(Base):
    __tablename__ = 'update_monitor_lease'
    id = Column(Integer, primary_key=True)
    expires_at = Column(DateTime, nullable=False)
    last_scheduled_at = Column(DateTime, nullable=True)


class PluginMonitoringSetting(Base):
    __tablename__ = 'plugin_monitoring_settings'
    server_id = Column(Integer, ForeignKey('servers.id', ondelete='CASCADE'), primary_key=True)
    plugin_name = Column(String(200), primary_key=True)
    mode = Column(String(20), nullable=False)
    provider = Column(String(30), nullable=False, default='')
    project = Column(Text, nullable=False, default='')
    version_pattern = Column(Text, nullable=False, default='')
    download_url = Column(Text, nullable=False, default='')
    download_rename = Column(String(255), nullable=False, default='')
    installed_pattern = Column(Text, nullable=False, default='')
    asset_pattern = Column(Text, nullable=False, default='')
    installed_detection = Column(String(32), nullable=False, default='auto')

    @property
    def link_pattern(self):
        """Compatibility alias for older integrations using the former name."""
        return self.download_url

    @link_pattern.setter
    def link_pattern(self, value):
        self.download_url = value


class ServerAuditEvent(Base):
    __tablename__ = "server_audit_events"

    id = Column(Integer, primary_key=True)
    # Keep audit history after a server or user is removed. Names are snapshots.
    server_id = Column(Integer, nullable=False, index=True)
    server_name = Column(String(100), nullable=False)
    actor_user_id = Column(Integer, nullable=True, index=True)
    actor_username = Column(String(64), nullable=False)
    action = Column(String(255), nullable=False)
    details = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
