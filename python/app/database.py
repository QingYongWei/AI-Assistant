from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import home

class Base(DeclarativeBase): pass

def db_path() -> Path:
    override = __import__('os').getenv('PERSONZIT_DB')
    return Path(override) if override else home() / "data" / "personzit.db"

def make_engine():
    p=db_path(); p.parent.mkdir(parents=True,exist_ok=True)
    engine=create_engine(f"sqlite:///{p.as_posix()}",connect_args={"check_same_thread":False,"timeout":30})
    @event.listens_for(engine,"connect")
    def pragmas(conn,_):
        cur=conn.cursor(); cur.execute("PRAGMA journal_mode=WAL"); cur.execute("PRAGMA foreign_keys=ON"); cur.execute("PRAGMA busy_timeout=30000"); cur.close()
    return engine

engine=make_engine(); SessionLocal=sessionmaker(engine,expire_on_commit=False)
def init_db():
    from . import models
    from sqlalchemy import inspect, text
    Base.metadata.create_all(engine)
    with engine.begin() as conn:  # 轻量迁移：为存量库补充新列
        cols={c["name"] for c in inspect(engine).get_columns("subtasks")}
        if "key" not in cols: conn.execute(text('ALTER TABLE subtasks ADD COLUMN "key" VARCHAR(40)'))
def get_db():
    with SessionLocal() as db: yield db

