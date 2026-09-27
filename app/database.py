from pathlib import Path
from .env import getenv

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


# Load Craftarr configuration.
load_dotenv(
    getenv(
        "CRAFTARR_CONSOLE_ENV",
        ".env",
    )
)


# Database location can be overridden through .env.
#
# Development default:
#   ./craftarr.db
#
# Production:
#   /var/lib/craftarr/craftarr.db

DATABASE_PATH = Path(
    getenv(
        "CRAFTARR_CONSOLE_DATABASE",
        "craftarr.db",
    )
).expanduser()


# Ensure the database directory exists when an
# absolute or nested path has been configured.

if DATABASE_PATH.parent != Path("."):

    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


DATABASE_URL = (
    f"sqlite:///{DATABASE_PATH}"
)


engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
    },
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


Base = declarative_base()


def get_db():

    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()