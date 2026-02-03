import json
from dataclasses import dataclass, asdict
from typing import Optional, List


@dataclass
class Output:
    type: str = "output"
    content: str = ""


@dataclass
class Status:
    type: str = "status"
    connected: bool = False
    session: Optional[str] = None
    is_busy: bool = False


@dataclass
class Sessions:
    type: str = "sessions"
    sessions: List[str] = None
    active: Optional[str] = None

    def __post_init__(self):
        if self.sessions is None:
            self.sessions = []


@dataclass
class Pong:
    type: str = "pong"


@dataclass
class Input:
    type: str = "input"
    content: str = ""
    key: Optional[str] = None


@dataclass
class Command:
    type: str = "command"
    action: str = ""
    session: Optional[str] = None
    command: Optional[str] = None


def to_json(msg) -> str:
    return json.dumps(asdict(msg))


def from_json(data: str):
    obj = json.loads(data)
    msg_type = obj.get("type")

    type_map = {
        "output": Output,
        "status": Status,
        "sessions": Sessions,
        "pong": Pong,
        "input": Input,
        "command": Command,
    }

    cls = type_map.get(msg_type)
    if cls is None:
        raise ValueError(f"Unknown message type: {msg_type}")

    return cls(**obj)
