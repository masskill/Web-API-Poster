"""SQLite engine and session helper."""
from sqlalchemy import inspect, text
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
    add_missing_columns()
    with Session(engine) as s:
        if not s.exec(select(Category)).first():
            s.add_all(Category(name=n) for n in DEFAULT_CATEGORIES)
            s.commit()


def add_missing_columns() -> None:
    """Tiny migration: create_all() makes new tables but not new columns of existing ones."""
    existing = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not existing.has_table(table.name):
                continue
            have = {c["name"] for c in existing.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if default is not None:
                    value = int(default) if isinstance(default, bool) else default
                    ddl += f" DEFAULT {value!r}" if isinstance(value, str) else f" DEFAULT {value}"
                conn.execute(text(ddl))


def get_session() -> Session:
    return Session(engine, expire_on_commit=False)
