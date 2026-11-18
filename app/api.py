"""FastAPI 后端：把四个 Skill 的状态机暴露成 HTTP 接口。

接口设计对应流水线的真实节点，而不是 CRUD 拼盘：
  POST /api/prds/upload      提测：上传 PRD（md/txt/docx）
  POST /api/runs             启动运行（会停在人工确认闸门）
  GET  /api/runs/{id}        查看状态与全部中间产物
  POST /api/runs/{id}/resume 提交人工修改后的测试地图，继续下游
  POST /api/runs/{id}/adoption 逐条裁决（采纳率数据来源）
  GET  /api/runs/{id}/export.xlsx 导出（评审不过则 409）
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from atp.config import Settings
from atp.export.xlsx import ExportBlocked, export_xlsx
from atp.models import AdoptionVerdict, FeatureItem, SourceRef, TestMap
from atp.parsers.prd import ParseError
from atp.pipeline.orchestrator import Orchestrator
from atp.store import RunStore

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

settings = Settings.from_env()
store = RunStore(settings)
orchestrator = Orchestrator(settings, store)

app = FastAPI(title="AI 测试用例生成平台", version="0.1.0")

# 前端依赖本地化（不依赖 CDN）：断网也能演示
_VENDOR = WEB_DIR / "vendor"
if _VENDOR.exists():
    app.mount("/vendor", StaticFiles(directory=str(_VENDOR)), name="vendor")


class PrdInput(BaseModel):
    prd_id: str = Field(min_length=1, max_length=60, pattern=r"^[\w\-\u4e00-\u9fff]+$")
    text: str = Field(min_length=50)


class RunInput(BaseModel):
    prd_id: str
    mode: str = "rag_hitl"


class TestMapInput(BaseModel):
    features: list[dict]


class AdoptionInput(BaseModel):
    case_id: str
    verdict: str
    note: str = ""


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "llm_mode": settings.llm_mode,
        "live_llm": settings.live_llm,
        "model": settings.model,
        "require_human_review": settings.require_human_review,
        "runs": len(store.list_runs()),
    }


# ------------------------------------------------------------------- PRD


@app.get("/api/prds")
def list_prds() -> dict:
    items = orchestrator.list_prds()
    for it in items:
        try:
            parsed = orchestrator.load_prd(it["prd_id"])
            it["stats"] = parsed.stats
        except Exception as exc:
            it["error"] = str(exc)
    return {"items": items}


@app.post("/api/prds")
def create_prd(payload: PrdInput) -> dict:
    try:
        parsed = orchestrator.save_prd(payload.text, payload.prd_id)
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prd_id": parsed.prd_id, "stats": parsed.stats, "sections": [s.heading for s in parsed.sections][:30]}


@app.post("/api/prds/upload")
async def upload_prd(request: Request, filename: str, prd_id: str | None = None) -> dict:
    """裸 body 上传（不依赖 python-multipart）。前端把 File 直接作为 body 发过来。"""
    body = await request.body()
    if len(body) < 50:
        raise HTTPException(status_code=400, detail="文件内容为空或过小")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".md", ".txt", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 .md / .txt / .docx")
    stem = prd_id or Path(filename).stem
    settings.prd_dir.mkdir(parents=True, exist_ok=True)
    target = settings.prd_dir / f"{stem}{suffix}"
    target.write_bytes(body)
    try:
        parsed = orchestrator.load_prd(stem)
    except ParseError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prd_id": stem, "stats": parsed.stats, "sections": [s.heading for s in parsed.sections][:30]}


# ------------------------------------------------------------------ runs


@app.post("/api/runs")
def start_run(payload: RunInput) -> dict:
    if payload.mode not in {"bare", "rag", "rag_hitl"}:
        raise HTTPException(status_code=400, detail="mode 只能是 bare / rag / rag_hitl")
    try:
        prd = orchestrator.load_prd(payload.prd_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        state = orchestrator.start(prd, mode=payload.mode)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _state_payload(state)


@app.get("/api/runs")
def list_runs() -> dict:
    return {"items": store.list_runs()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _state_payload(_load(run_id))


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, payload: TestMapInput | None = None) -> dict:
    state = _load(run_id)
    override = None
    if payload and payload.features:
        try:
            override = TestMap(prd_id=state.prd_id, features=[FeatureItem.model_validate(f) for f in payload.features])
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"测试地图格式错误：{exc}") from exc
    try:
        return _state_payload(orchestrator.resume(run_id, override))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/api/runs/{run_id}/confirm")
def confirm_run(run_id: str) -> dict:
    try:
        return _state_payload(orchestrator.confirm_ai_draft(run_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/adoption")
def record_adoption(run_id: str, payload: AdoptionInput) -> dict:
    state = _load(run_id)
    if payload.verdict not in {"usable", "needs_edit", "unusable"}:
        raise HTTPException(status_code=400, detail="verdict 只能是 usable / needs_edit / unusable")
    case_ids = {c.id for c in (state.cases.cases if state.cases else [])}
    if payload.case_id not in case_ids:
        raise HTTPException(status_code=404, detail=f"用例 {payload.case_id} 不存在")
    state.adoption = [a for a in state.adoption if a.case_id != payload.case_id]
    state.adoption.append(AdoptionVerdict(case_id=payload.case_id, verdict=payload.verdict, note=payload.note))
    store.save(state)
    return {"adoption": [a.model_dump() for a in state.adoption], "rate": _adoption_rate(state)}


@app.get("/api/runs/{run_id}/events")
def get_events(run_id: str) -> dict:
    _load(run_id)
    return {"items": store.events(run_id), "llm_calls": store.llm_calls(run_id)}


@app.get("/api/runs/{run_id}/export.xlsx")
def export_run(run_id: str, draft: int = 0) -> Response:
    state = _load(run_id)
    try:
        path = export_xlsx(state, settings.runs_dir / run_id / "artifacts", force_draft=bool(draft))
    except ExportBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ------------------------------------------------------------------ utils


def _load(run_id: str):
    if not store.exists(run_id):
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    return store.load(run_id)


def _adoption_rate(state) -> dict:
    total = len(state.adoption)
    if not total:
        return {"total": 0, "usable": 0, "needs_edit": 0, "unusable": 0, "rate": None}
    usable = sum(1 for a in state.adoption if a.verdict == "usable")
    return {
        "total": total,
        "usable": usable,
        "needs_edit": sum(1 for a in state.adoption if a.verdict == "needs_edit"),
        "unusable": sum(1 for a in state.adoption if a.verdict == "unusable"),
        "rate": round(usable / total, 4),
    }


def _state_payload(state) -> dict:
    data = state.model_dump(mode="json")
    data["adoption_rate"] = _adoption_rate(state)
    data["export_allowed"] = not (state.review and state.review.gate == "revise")
    return data


# -------------------------------------------------------------------- web


@app.get("/")
def index() -> FileResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail="web/index.html 缺失")
    return FileResponse(page, media_type="text/html; charset=utf-8")
