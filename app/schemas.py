from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .processes import normalize_memory


ProcessBackend = Literal["subprocess", "systemd"]


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class InstanceOut(BaseModel):
    node_id: str


class NodeServerOut(BaseModel):
    server_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=100)
    minecraft_version: str | None = Field(default=None, max_length=40)
    paper_build: str | None = Field(default=None, max_length=40)
    memory: str = Field(max_length=20)
    min_memory: str = Field(max_length=20)
    port: int = Field(ge=1, le=65535)
    enabled: bool


class NodeIdentityOut(BaseModel):
    node_id: UUID
    app_version: str | None = None


class UserCreate(BaseModel):
    username: str = Field(
        min_length=3,
        max_length=64,
    )

    password: str = Field(
        min_length=8,
        max_length=128,
    )

    role_id: int


class UserUpdate(BaseModel):
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
    )

    role_id: int | None = None

    enabled: bool | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(
        from_attributes=True
    )

    id: int
    username: str
    role_id: int
    role_name: str
    enabled: bool


class ServerCreate(BaseModel):
    name: str

    directory: str

    service_name: str

    minecraft_version: str | None = None

    paper_build: str | None = None

    memory: str = "2G"
    min_memory: str | None = None
    jar_name: str = Field(default="paper.jar", pattern=r"^[^/\\]+\.jar$")
    java_args: str = Field(default="", max_length=1000)
    stop_commands: str = Field(default="", max_length=4000)
    java_path: str = "java"
    process_backend: ProcessBackend = "systemd"

    port: int = Field(
        default=25565,
        ge=1,
        le=65535,
    )

    @model_validator(mode="after")
    def default_initial_memory(self):
        if self.min_memory is None:
            self.min_memory = self.memory
        return self

    @field_validator("memory", "min_memory", mode="before")
    @classmethod
    def normalize_memory_units(cls, value):
        return None if value is None else normalize_memory(value)


class ServerUpdate(BaseModel):
    name: str | None = None

    directory: str | None = None

    service_name: str | None = None

    minecraft_version: str | None = None

    paper_build: str | None = None

    memory: str | None = None
    min_memory: str | None = None
    jar_name: str | None = Field(default=None, pattern=r"^[^/\\]+\.jar$")
    java_args: str | None = Field(default=None, max_length=1000)
    stop_commands: str | None = Field(default=None, max_length=4000)
    process_backend: ProcessBackend | None = None

    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
    )

    enabled: bool | None = None

    @field_validator("memory", "min_memory", mode="before")
    @classmethod
    def normalize_memory_units(cls, value):
        return None if value is None else normalize_memory(value)


class ServerOut(BaseModel):
    model_config = ConfigDict(
        from_attributes=True
    )

    id: int
    node_id: str
    server_ref: str
    name: str
    directory: str
    service_name: str

    minecraft_version: str | None
    paper_build: str | None

    memory: str
    min_memory: str
    jar_name: str
    java_args: str
    stop_commands: str
    java_path: str
    process_backend: ProcessBackend

    port: int
    enabled: bool


class ServerAccessRequest(BaseModel):
    user_id: int
    server_id: int

class PaperInstallRequest(BaseModel):
    minecraft_version: str


class ServerStartRequest(BaseModel):
    memory: str = "2G"

    @field_validator("memory", mode="before")
    @classmethod
    def normalize_memory_units(cls, value):
        return normalize_memory(value)


class ConsoleCommandRequest(BaseModel):
    command: str = Field(
        min_length=1,
        max_length=500,
    )
