"""LLM 抽象层：只暴露 chat()，其余（重试、JSON 修复、审计）在 schema.py 与具体实现里。

两个实现共享同一接口：
- DeepSeekClient：真实调用 OpenAI 兼容的 /chat/completions
- HeuristicClient：离线启发式基线。它不是"假 AI"，而是一个诚实的规则基线，
  用来保证链路在没有 API key / 没有网络时依然可跑，也顺带成为评测里的下限参照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class CallResult:
    text: str
    attempt: int = 1
    latency_ms: int = 0
    prompt_chars: int = 0
    error: str = ""


@dataclass
class CallHook:
    """每次调用后回调，编排器用它写审计日志。"""

    fn: Callable[[dict], None] | None = None
    task: str = ""

    def emit(self, payload: dict) -> None:
        if self.fn:
            self.fn({**payload, "task": self.task})


@runtime_checkable
class LLMClient(Protocol):
    mode: str
    model: str

    def chat(
        self,
        messages: list[Message],
        *,
        task: str,
        json_mode: bool = True,
        max_tokens: int = 4096,
        payload: dict | None = None,
    ) -> CallResult: ...


@dataclass
class ClientOptions:
    temperature: float = 0.2
    timeout_s: float = 120.0
    max_retries: int = 3


def build_messages(system: str, user: str) -> list[Message]:
    return [Message("system", system), Message("user", user)]


@dataclass
class Conversation:
    """把多轮修复对话累积起来，避免每次重建列表。"""

    system: str
    turns: list[Message] = field(default_factory=list)

    def start(self, user: str) -> list[Message]:
        self.turns = [Message("system", self.system), Message("user", user)]
        return list(self.turns)

    def add_repair(self, assistant_text: str, feedback: str) -> list[Message]:
        self.turns.append(Message("assistant", assistant_text))
        self.turns.append(Message("user", feedback))
        return list(self.turns)
