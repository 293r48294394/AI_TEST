"""流水线编排：显式状态机 + 人工确认闸门 + 产物落盘。

为什么不用 LangGraph：
- 这道流水线只有 5 个节点、1 个人工闸门、1 个循环点，显式状态机的全部代码 200 行，
  每一步都能断点、能单测、能在 events.jsonl 里看到耗时与重试次数；
- 引入图框架会把它变成黑盒，而项目要证明的恰恰是"每一步都被验证过"；
- 后续真要换成 LangGraph，只要把 run_stage 换成 node 即可（Skill 层是纯函数，不依赖编排）。

状态流转：s1_testmap → gate1(人工) → s2_testpoints → s3_testcases → s4_review → done
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass

from ..config import Settings
from ..llm.base import LLMClient
from ..llm.deepseek import DeepSeekClient
from ..llm.mock import HeuristicClient
from ..models import (
    ReviewReport,
    RunState,
    StageRecord,
    TestCaseSet,
    TestMap,
    TestPointSet,
)
from ..parsers.prd import ParsedPRD, parse_file, parse_text
from ..skills import s1_testmap, s2_testpoints, s3_testcases, s4_review
from ..store import RunStore
from ..util import similarity

MODES = ("bare", "rag", "rag_hitl")


@dataclass
class PipelineConfig:
    mode: str = "rag_hitl"

    @property
    def use_rag(self) -> bool:
        return self.mode != "bare"

    @property
    def require_human(self) -> bool:
        return self.mode == "rag_hitl"


class Orchestrator:
    def __init__(self, settings: Settings, store: RunStore | None = None):
        self.settings = settings
        self.store = store or RunStore(settings)

    # ---------------------------------------------------------------- client
    def _make_client(self, run_id: str) -> LLMClient:
        if self.settings.live_llm:
            client: LLMClient = DeepSeekClient(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                model=self.settings.model,
                options=None,
            )
        else:
            client = HeuristicClient()

        def _record(row: dict) -> None:
            self.store.log_llm(run_id, row)

        client.on_call = _record  # type: ignore[attr-defined]
        return client

    # ------------------------------------------------------------------ prd
    def save_prd(self, text: str, prd_id: str) -> ParsedPRD:
        self.settings.prd_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.prd_dir / f"{prd_id}.md"
        path.write_text(text, encoding="utf-8")
        return parse_text(text, source=path.name, prd_id=prd_id)

    def load_prd(self, prd_id: str) -> ParsedPRD:
        for suffix in (".md", ".txt", ".docx", ".pdf"):
            path = self.settings.prd_dir / f"{prd_id}{suffix}"
            if path.exists():
                return parse_file(path, prd_id=prd_id)
        raise FileNotFoundError(f"找不到 PRD：{prd_id}（{self.settings.prd_dir}）")

    def prd_from_artifacts(self, run_id: str, prd_id: str) -> ParsedPRD:
        """从运行产物重建 PRD 视图。

        这样即使源文件被删改，历史运行仍可复现（resume 不依赖外部文件状态），
        也是"每次运行自包含、可审计"的一部分。
        """
        from ..parsers.prd import Chunk

        rows = self.store.load_artifact(run_id, "00_prd_chunks.json") or []
        chunks = [Chunk(**row) for row in rows]
        text = "\n\n".join(c.text for c in chunks)
        return ParsedPRD(prd_id=prd_id, source=f"artifacts:{run_id}", text=text, sections=[], chunks=chunks)

    def list_prds(self) -> list[dict]:
        out = []
        for path in sorted(self.settings.prd_dir.glob("*")):
            if path.suffix.lower() in {".md", ".txt", ".docx", ".pdf"}:
                out.append({"prd_id": path.stem, "file": path.name, "size": path.stat().st_size})
        return out

    # ---------------------------------------------------------------- start
    def start(self, prd: ParsedPRD, mode: str = "rag_hitl") -> RunState:
        cfg = PipelineConfig(mode=mode)
        run_id = self.store.new_run_id()
        state = RunState(run_id=run_id, prd_id=prd.prd_id, mode=mode, llm_mode=self.settings.llm_mode)  # type: ignore[arg-type]
        self.store.save(state)
        self.store.log_event(run_id, {"event": "run_start", "prd_id": prd.prd_id, "mode": mode, "source": prd.source})
        self.store.save_artifact(run_id, "00_prd_chunks.json", [c.as_dict() for c in prd.chunks])
        self.store.save_artifact(run_id, "00_prd_stats.json", prd.stats)

        client = self._make_client(run_id)
        try:
            self._stage_s1(state, client, prd, cfg)
        except Exception as exc:
            # 阶段失败不抛异常：把失败写进状态并返回，调用方才拿得到 run_id 与审计日志
            self._fail(state, "s1_testmap", exc)
            return state
        self.store.save(state)

        if cfg.require_human:
            state.status = "awaiting_human"
            state.stage = "gate1"
            self.store.log_event(run_id, {"event": "gate_await", "gate": "testmap", "features": len(state.testmap_ai.features)})  # type: ignore[union-attr]
            self.store.save(state)
            return state

        self._finish(state, prd, cfg)
        return state

    # --------------------------------------------------------------- resume
    def resume(self, run_id: str, testmap_override: TestMap | None = None) -> RunState:
        state = self.store.load(run_id)
        if state.stage != "gate1":
            raise ValueError(f"运行 {run_id} 当前阶段为 {state.stage}，不在人工确认闸门")
        prd = self.prd_from_artifacts(run_id, state.prd_id)
        cfg = PipelineConfig(mode=state.mode)

        final = testmap_override or state.testmap_ai
        assert final is not None
        diff = diff_testmaps(state.testmap_ai, final) if state.testmap_ai else {}
        state.testmap_final = final
        state.hitl = {
            "confirmed": True,
            "changed": bool(diff.get("added") or diff.get("removed") or diff.get("edited")),
            "diff": diff,
        }
        self.store.save_artifact(run_id, "02_testmap_final.json", final)
        self.store.save_artifact(run_id, "hitl_diff.json", diff)
        self.store.log_event(
            run_id,
            {
                "event": "gate_confirm",
                "changed": state.hitl["changed"],
                "added": len(diff.get("added", [])),
                "removed": len(diff.get("removed", [])),
                "edited": len(diff.get("edited", [])),
            },
        )
        state.status = "running"
        state.stage = "s2_testpoints"
        self.store.save(state)
        self._finish(state, prd, cfg)
        return state

    def confirm_ai_draft(self, run_id: str) -> RunState:
        """一键确认 AI 草稿（演示用）。语义上等价于"人看过并接受"。"""
        return self.resume(run_id, None)

    # --------------------------------------------------------------- stages
    def _stage_s1(self, state: RunState, client: LLMClient, prd: ParsedPRD, cfg: PipelineConfig) -> None:
        rec = StageRecord(stage="s1_testmap")
        started = time.perf_counter()
        result = s1_testmap.run(client, self.settings, prd, use_rag=cfg.use_rag, prd_id=state.prd_id)
        tm: TestMap = result.value  # type: ignore[assignment]
        state.testmap_ai = tm
        state.testmap_final = tm
        self.store.save_artifact(state.run_id, "01_testmap_ai.json", tm)
        rec.finished_at = None
        rec.duration_ms = int((time.perf_counter() - started) * 1000)
        rec.metrics = {
            "features": len(tm.features),
            "rules": sum(len(f.rules) for f in tm.features),
            "cited_chunks": len({s.chunk_id for f in tm.features for s in f.sources}),
            "attempts": result.attempts,
            "first_try_ok": 1.0 if result.first_try_ok else 0.0,
        }
        state.stages.append(rec)
        self.store.log_event(state.run_id, {"event": "stage_done", **rec.model_dump(mode="json")})

    def _finish(self, state: RunState, prd: ParsedPRD, cfg: PipelineConfig) -> None:
        client = self._make_client(state.run_id)
        try:
            self._stage_s2(state, client, prd, cfg)
            self._stage_s3(state, client, prd, cfg)
            self._stage_s4(state, client, prd)
        except Exception as exc:
            self._fail(state, state.stage, exc)
            return  # 失败也返回，不抛：状态里已记录原因与已产生的产物
        state.stage = "done"
        state.status = "completed"
        state.touch()
        self.store.save(state)
        self.store.log_event(
            state.run_id,
            {
                "event": "run_done",
                "testpoints": len(state.testpoints.test_points) if state.testpoints else 0,
                "cases": len(state.cases.cases) if state.cases else 0,
                "score": state.review.score if state.review else None,
                "gate": state.review.gate if state.review else None,
            },
        )

    def _stage_s2(self, state: RunState, client: LLMClient, prd: ParsedPRD, cfg: PipelineConfig) -> None:
        rec = StageRecord(stage="s2_testpoints")
        started = time.perf_counter()
        assert state.testmap_final is not None
        result = s2_testpoints.run(client, self.settings, state.testmap_final, prd.chunks, use_rag=cfg.use_rag)
        tps: TestPointSet = result.value  # type: ignore[assignment]
        state.testpoints = tps
        self.store.save_artifact(state.run_id, "03_testpoints.json", tps)
        rec.duration_ms = int((time.perf_counter() - started) * 1000)
        rec.metrics = {
            "testpoints": len(tps.test_points),
            "attempts": result.attempts,
            "first_try_ok": 1.0 if result.first_try_ok else 0.0,
        }
        rec.details = {"coverage": dict(s2_testpoints.coverage_counts(tps.test_points))}
        state.stages.append(rec)
        self.store.log_event(state.run_id, {"event": "stage_done", **rec.model_dump(mode="json")})

    def _stage_s3(self, state: RunState, client: LLMClient, prd: ParsedPRD, cfg: PipelineConfig) -> None:
        rec = StageRecord(stage="s3_testcases")
        started = time.perf_counter()
        assert state.testmap_final is not None and state.testpoints is not None
        result = s3_testcases.run(
            client, self.settings, state.testmap_final, state.testpoints.test_points, prd.chunks, use_rag=cfg.use_rag
        )
        state.cases = TestCaseSet(cases=result.cases)
        self.store.save_artifact(state.run_id, "04_cases.json", state.cases)
        rec.duration_ms = int((time.perf_counter() - started) * 1000)
        rec.metrics = {
            "cases": len(result.cases),
            "batches": result.batches,
            "failed_batches": len(result.failed_batches),
            "repair_attempts": result.repair_attempts,
            "first_try_ok_batches": result.first_try_ok_batches,
        }
        rec.ok = not result.failed_batches
        rec.error = "; ".join(result.failed_batches[:2])
        state.stages.append(rec)
        self.store.log_event(state.run_id, {"event": "stage_done", **rec.model_dump(mode="json")})

    def _stage_s4(self, state: RunState, client: LLMClient, prd: ParsedPRD) -> None:
        rec = StageRecord(stage="s4_review")
        started = time.perf_counter()
        assert state.testmap_final is not None and state.testpoints is not None and state.cases is not None
        report: ReviewReport = s4_review.run(
            client,
            self.settings,
            state.testmap_final,
            state.testpoints.test_points,
            state.cases.cases,
            chunks=prd.chunks,
        )
        state.review = report
        self.store.save_artifact(state.run_id, "05_review.json", report)
        rec.duration_ms = int((time.perf_counter() - started) * 1000)
        rec.metrics = {"score": report.score, "issues": len(report.issues), "blockers": sum(1 for i in report.issues if i.severity == "blocker")}
        state.stages.append(rec)
        self.store.log_event(state.run_id, {"event": "stage_done", **rec.model_dump(mode="json")})

    # ---------------------------------------------------------------- error
    def _fail(self, state: RunState, stage: str, exc: Exception) -> None:
        state.status = "failed"
        state.errors.append(f"{stage}: {type(exc).__name__}: {exc}")
        self.store.log_event(
            state.run_id,
            {"event": "stage_failed", "stage": stage, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]},
        )
        self.store.save(state)


# ------------------------------------------------------------------ diff


def diff_testmaps(ai: TestMap | None, human: TestMap) -> dict:
    """记录人工改了什么 —— 这是 human-in-the-loop 的量化证据，也是评测模式三的输入。"""
    if ai is None:
        return {"added": [], "removed": [], "edited": [], "summary": "无 AI 草稿"}
    ai_by_id = {f.id: f for f in ai.features}
    hu_by_id = {f.id: f for f in human.features}
    added = [f.id for f in human.features if f.id not in ai_by_id]
    removed = [f.id for f in ai.features if f.id not in hu_by_id]
    edited = []
    for fid, hf in hu_by_id.items():
        af = ai_by_id.get(fid)
        if not af:
            continue
        changes = []
        if similarity(af.name, hf.name) < 0.95:
            changes.append("name")
        if set(af.rules) != set(hf.rules):
            changes.append("rules")
        if similarity(af.summary, hf.summary) < 0.9:
            changes.append("summary")
        if changes:
            edited.append({"id": fid, "fields": changes, "rules_added": sorted(set(hf.rules) - set(af.rules))[:5], "rules_removed": sorted(set(af.rules) - set(hf.rules))[:5]})
    return {
        "added": added,
        "removed": removed,
        "edited": edited,
        "summary": f"+{len(added)} / -{len(removed)} / ~{len(edited)}",
    }
