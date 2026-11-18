"""运行产物持久化。

目录结构（每次运行一个目录，天然可审计、可回放）：

data/runs/<run_id>/
  state.json          运行状态（可 resume）
  events.jsonl        阶段事件流（时间、耗时、错误）
  llm_calls.jsonl     每次 LLM 调用审计（token / 延迟 / 重试）
  artifacts/
    01_testmap_ai.json         AI 草稿
    02_testmap_final.json      人工确认后的版本
    03_testpoints.json
    04_cases.json
    05_review.json
    hitl_diff.json             人工改了什么
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .config import Settings
from .models import RunState
from .util import append_jsonl, now_iso, read_json, read_jsonl, write_json


class RunStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.runs_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def new_run_id(self) -> str:
        return now_iso()[:10].replace("-", "") + "-" + uuid.uuid4().hex[:6]

    def dir(self, run_id: str) -> Path:
        return self.root / run_id

    def artifacts_dir(self, run_id: str) -> Path:
        p = self.dir(run_id) / "artifacts"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---- state ----
    def save(self, state: RunState) -> None:
        state.touch()
        write_json(self.dir(state.run_id) / "state.json", state.model_dump(mode="json"))

    def load(self, run_id: str) -> RunState:
        return RunState.model_validate(read_json(self.dir(run_id) / "state.json"))

    def exists(self, run_id: str) -> bool:
        return (self.dir(run_id) / "state.json").exists()

    def list_runs(self) -> list[dict]:
        rows = []
        for p in sorted(self.root.glob("*/state.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                s = read_json(p)
            except Exception:
                continue
            rows.append(
                {
                    "run_id": s.get("run_id"),
                    "prd_id": s.get("prd_id"),
                    "mode": s.get("mode"),
                    "status": s.get("status"),
                    "stage": s.get("stage"),
                    "created_at": s.get("created_at"),
                    "n_testpoints": len(((s.get("testpoints") or {}).get("test_points")) or []),
                    "n_cases": len(((s.get("cases") or {}).get("cases")) or []),
                    "review_score": (s.get("review") or {}).get("score"),
                }
            )
        return rows

    # ---- artifacts ----
    def save_artifact(self, run_id: str, name: str, payload) -> Path:
        path = self.artifacts_dir(run_id) / name
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        write_json(path, payload)
        return path

    def load_artifact(self, run_id: str, name: str):
        path = self.artifacts_dir(run_id) / name
        return read_json(path) if path.exists() else None

    # ---- logs ----
    def log_event(self, run_id: str, event: dict) -> None:
        append_jsonl(self.dir(run_id) / "events.jsonl", {"at": now_iso(), **event})

    def log_llm(self, run_id: str, call: dict) -> None:
        append_jsonl(self.dir(run_id) / "llm_calls.jsonl", call)

    def llm_calls(self, run_id: str) -> list[dict]:
        return read_jsonl(self.dir(run_id) / "llm_calls.jsonl")

    def events(self, run_id: str) -> list[dict]:
        return read_jsonl(self.dir(run_id) / "events.jsonl")
