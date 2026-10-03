from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .auth import get_current_user
from .database import get_db
from .instance_identity import get_node_id
from .models import User
from .schemas import InstanceOut


router = APIRouter(tags=["Instance"])


@router.get("/api/instance", response_model=InstanceOut)
def get_instance_identity(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return {"node_id": get_node_id(db)}
