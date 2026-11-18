"""JSON 契约层：解析 → pydantic 校验 → 自定义校验 → 失败回喂重试。

这是整个项目"可被测试"的核心机制：
- 模型输出不是字符串直通下游，而是必须先过 pydantic 模型；
- 除了字段类型，还跑业务校验（引用块是否真实存在、覆盖类目是否齐全、id 是否唯一）；
- 校验失败的报错原文会作为下一条 user 消息回喂给模型，最多 max_repair 轮；
- 每轮 attempts / first_try_ok 都记录，直接产出"格式合规率"指标。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .base import Conversation, LLMClient

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class SchemaRepairFailed(RuntimeError):
    def __init__(self, task: str, attempts: list[str], raw_last: str):
        self.task = task
        self.attempts = attempts
        self.raw_last = raw_last
        super().__init__(f"[{task}] JSON 契约校验在 {len(attempts)} 次尝试后仍失败：" + " | ".join(attempts[-2:]))


@dataclass
class RepairOutcome:
    value: BaseModel
    attempts: int
    first_try_ok: bool
    problems: list[str] = field(default_factory=list)
    raw: str = ""
    usage: dict = field(default_factory=dict)


def extract_json(text: str) -> str:
    """从模型输出里抠出 JSON：容忍 ```json 围栏、前后废话、BOM。"""
    text = (text or "").lstrip("\ufeff").strip()
    text = _FENCE.sub("", text)
    if text.startswith("{") or text.startswith("["):
        return text
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        return text
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end > start else text[start:]


def format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"- 字段 `{loc}`: {err.get('msg')} (输入={err.get('input')!r})")
    return "\n".join(lines)


def call_json(
    client: LLMClient,
    *,
    task: str,
    system: str,
    user: str,
    model_cls: type[T],
    validator: Callable[[T], list[str]] | None = None,
    max_repair: int = 2,
    extra_payload: dict | None = None,
    max_tokens: int = 4096,
) -> RepairOutcome:
    conv = Conversation(system)
    messages = conv.start(user)
    problems: list[str] = []
    raw = ""

    for attempt in range(1, max_repair + 2):
        result = client.chat(messages, task=task, json_mode=True, max_tokens=max_tokens, payload=extra_payload)
        raw = result.text
        try:
            data = json.loads(extract_json(raw))
            obj = model_cls.model_validate(data)
            issues = validator(obj) if validator else []
            if issues:
                raise _BusinessError(issues)
        except json.JSONDecodeError as exc:
            problems.append(f"JSON 解析失败：{exc}")
            messages = conv.add_repair(
                raw,
                f"你上一次的输出不是合法 JSON（{exc}）。请只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏。",
            )
            continue
        except _BusinessError as exc:
            problems.append("业务校验未通过：" + "；".join(exc.issues[:5]))
            messages = conv.add_repair(
                raw,
                "上一次输出未通过业务校验，问题如下：\n"
                + "\n".join(f"- {i}" for i in exc.issues[:8])
                + "\n请修正后重新输出完整 JSON（保持同样结构）。",
            )
            continue
        except ValidationError as exc:
            problems.append("字段校验未通过：" + format_errors(exc))
            messages = conv.add_repair(
                raw,
                "上一次输出不符合 JSON Schema，错误如下：\n"
                + format_errors(exc)
                + f"\n目标结构（pydantic）:\n{json.dumps(model_cls.model_json_schema(), ensure_ascii=False)[:2500]}"
                + "\n请输出修正后的完整 JSON。",
            )
            continue

        return RepairOutcome(
            value=obj,
            attempts=attempt,
            first_try_ok=(attempt == 1),
            problems=problems,
            raw=raw,
        )

    raise SchemaRepairFailed(task, problems, raw)


class _BusinessError(Exception):
    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


# ----------------------------------------------------------------- 业务校验器


def make_citation_validator(allowed_chunk_ids: set[str]) -> Callable[[Any], list[str]]:
    """引用完整性：任何 SourceRef.chunk_id 都必须是本次检索真实返回过的块。"""

    def _validate(obj: Any) -> list[str]:
        issues: list[str] = []
        for item in _iter_items(obj):
            for ref in getattr(item, "sources", []) or []:
                cid = getattr(ref, "chunk_id", "")
                if cid and cid not in allowed_chunk_ids:
                    issues.append(f"{item.id} 引用了不存在的证据块 `{cid}`，只允许引用 {sorted(allowed_chunk_ids)[:8]}")
        return issues

    return _validate


def _iter_items(obj: Any):
    for attr in ("features", "test_points", "cases"):
        seq = getattr(obj, attr, None)
        if seq:
            yield from seq


def uniqueness_validator(prefix: str = "") -> Callable[[Any], list[str]]:
    def _validate(obj: Any) -> list[str]:
        ids: list[str] = []
        for item in _iter_items(obj):
            ids.append(getattr(item, "id", ""))
        dupes = {i for i in ids if ids.count(i) > 1}
        empty = [i for i in ids if not i]
        issues = [f"id 重复：{sorted(dupes)}"] if dupes else []
        if empty:
            issues.append("存在空 id")
        if not ids:
            issues.append("输出为空列表，至少需要 1 条")
        return issues

    return _validate


def category_coverage_validator(required: dict[str, int]) -> Callable[[Any], list[str]]:
    """覆盖门槛：六类里若缺项，把缺口写成可执行的反馈让模型补。"""

    def _validate(obj: Any) -> list[str]:
        counts: dict[str, int] = {}
        for item in _iter_items(obj):
            cat = getattr(item, "category", "")
            counts[cat] = counts.get(cat, 0) + 1
        return [
            f"类别 `{cat}` 至少需要 {need} 条，当前 {counts.get(cat, 0)} 条"
            for cat, need in required.items()
            if counts.get(cat, 0) < need
        ]

    return _validate


def combine(*validators: Callable[[Any], list[str]]) -> Callable[[Any], list[str]]:
    def _validate(obj: Any) -> list[str]:
        issues: list[str] = []
        for v in validators:
            issues.extend(v(obj))
        return issues

    return _validate
