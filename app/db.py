"""SQLite engine and session helper."""
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.models import Category

DEFAULT_CATEGORIES = ["Рецепти", "Новини", "Музика", "Інше"]

engine = None


def init_db(url: str | None = None) -> None:
    global engine
    config.ensure_dirs()
    url = url or f"sqlite:///{config.DB_PATH}"
    kwargs = {"connect_args": {"check_same_thread": False}}
    if url == "sqlite://":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if not s.exec(select(Category)).first():
            s.add_all(Category(name=n) for n in DEFAULT_CATEGORIES)
            s.commit()


def get_session() -> Session:
    return Session(engine, expire_on_commit=False)
