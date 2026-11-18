"""批量并发：一次跑多份 PRD，量化吞吐与失败率。

为什么值得单独做：面试官问"这东西能上生产吗"时，真正的答案是"我不知道，我只测过一份文档"。
批量跑至少能把回答变成："4 并发跑 N 份，平均 X 秒/份，失败率 Y%，失败原因是 Z"。

并发安全依赖三点：
1. 每次运行独立目录（run_id = 日期 + 随机串），互不覆盖；
2. Settings 是 frozen dataclass，只读共享；
3. 每个 worker 自己 new 一个 Orchestrator（不共享客户端状态），
   只有 RunStore 的文件写入是共享的——写的是不同文件，不触发竞争。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..config import Settings
from ..pipeline.orchestrator import Orchestrator
from ..store import RunStore


@dataclass
class BatchItem:
    prd_id: str
    run_id: str = ""
    status: str = ""
    ok: bool = False
    error: str = ""
    duration_ms: int = 0
    points: int = 0
    cases: int = 0
    score: int | None = None
    gate: str = ""


@dataclass
class BatchResult:
    items: list[BatchItem] = field(default_factory=list)
    workers: int = 1
    wall_ms: int = 0

    @property
    def success(self) -> int:
        return sum(1 for i in self.items if i.ok)

    @property
    def failed(self) -> int:
        return len(self.items) - self.success

    @property
    def throughput_per_sec(self) -> float:
        return round(len(self.items) / (self.wall_ms / 1000), 3) if self.wall_ms else 0.0

    def summary(self) -> dict:
        return {
            "documents": len(self.items),
            "success": self.success,
            "failed": self.failed,
            "failure_rate": round(self.failed / len(self.items), 4) if self.items else 0.0,
            "workers": self.workers,
            "wall_ms": self.wall_ms,
            "avg_ms": round(self.wall_ms / len(self.items), 1) if self.items else 0.0,
            "throughput_per_sec": self.throughput_per_sec,
            "gates": {g: sum(1 for i in self.items if i.gate == g) for g in ("pass", "revise", "")},
        }


def run_batch(
    settings: Settings,
    prd_ids: list[str],
    *,
    mode: str = "rag",
    workers: int = 3,
    auto_confirm: bool = True,
) -> BatchResult:
    store = RunStore(settings)
    result = BatchResult(workers=max(1, workers))
    started = time.perf_counter()

    def _one(prd_id: str) -> BatchItem:
        item = BatchItem(prd_id=prd_id)
        t0 = time.perf_counter()
        try:
            orc = Orchestrator(settings, RunStore(settings))
            prd = orc.load_prd(prd_id)
            state = orc.start(prd, mode=mode)
            if state.status == "awaiting_human" and auto_confirm:
                state = orc.confirm_ai_draft(state.run_id)
            item.run_id = state.run_id
            item.status = state.status
            item.ok = state.status == "completed"
            item.points = len(state.testpoints.test_points) if state.testpoints else 0
            item.cases = len(state.cases.cases) if state.cases else 0
            item.score = state.review.score if state.review else None
            item.gate = state.review.gate if state.review else ""
        except Exception as exc:
            item.error = f"{type(exc).__name__}: {exc}"
            item.status = "failed"
        item.duration_ms = int((time.perf_counter() - t0) * 1000)
        return item

    with ThreadPoolExecutor(max_workers=result.workers) as pool:
        futures = {pool.submit(_one, pid): pid for pid in prd_ids}
        for fut in as_completed(futures):
            result.items.append(fut.result())

    result.items.sort(key=lambda i: i.prd_id)
    result.wall_ms = int((time.perf_counter() - started) * 1000)
    return result
