from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from .database import get_db
from .instance_identity import get_node_id
from .models import Server
from .node_security import verify_node_token
from .schemas import NodeIdentityOut, NodeServerOut
from .version import APP_VERSION


router = APIRouter(prefix="/api/node", tags=["Node connection"])


def require_node_token(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not verify_node_token(db, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid node token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/identity", response_model=NodeIdentityOut)
def node_identity(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return {"node_id": get_node_id(db), "app_version": APP_VERSION}


@router.get("/servers", response_model=list[NodeServerOut])
def node_servers(
    response: Response,
    _authorized: None = Depends(require_node_token),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "server_id": server.id,
            "name": server.name,
            "minecraft_version": server.minecraft_version,
            "paper_build": server.paper_build,
            "memory": server.memory,
            "min_memory": server.min_memory,
            "port": server.port,
            "enabled": server.enabled,
        }
        for server in db.query(Server).order_by(Server.name).all()
    ]
