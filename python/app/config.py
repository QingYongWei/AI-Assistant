from pathlib import Path
import os, yaml

def home() -> Path:
    return Path(os.getenv("PERSONZIT_HOME", Path.home() / ".personzit"))

def load_config() -> dict:
    path = home() / "config.yaml"
    if not path.exists(): return {"server":{"host":"127.0.0.1","port":8765},"agents":{"default":"mock","mock":{"enabled":True}}}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def workspace_root() -> Path:
    cfg=load_config().get('workspace',{}) or {}
    root=Path(str(cfg.get('root') or Path.home()/'.personzit'/'workspace')).expanduser().resolve()
    return root


def workspace_tasks() -> Path:
    cfg=load_config().get('workspace',{}) or {}
    root=workspace_root()
    return Path(str(cfg.get('tasks') or root/'tasks')).expanduser().resolve()


def workspace_documents() -> Path:
    cfg=load_config().get('workspace',{}) or {}
    root=workspace_root()
    return Path(str(cfg.get('documents') or root/'documents')).expanduser().resolve()
