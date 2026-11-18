"""数据模型：流水线四个阶段的输入/输出契约。

为什么把契约写死成 pydantic 模型：
1. 它们同时是 JSON Schema 的来源 —— 校验失败的报错信息会原样回喂给 LLM，
   让"格式不合规"变成一次可修复的重试，而不是一条脏数据流进下游。
2. 引用完整性（SourceRef.chunk_id 必须来自检索结果）是实现"可追责生成"的最小抓手：
   模型只能引用给出的证据块，凭空造句会在校验阶段被拒绝。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal["normal", "exception", "boundary", "security", "compat", "performance"]
Priority = Literal["P0", "P1", "P2"]
CATEGORIES: tuple[str, ...] = ("normal", "exception", "boundary", "security", "compat", "performance")

Stage = Literal["s1_testmap", "gate1", "s2_testpoints", "s3_testcases", "s4_review", "done"]
RunStatus = Literal["running", "awaiting_human", "failed", "completed", "aborted"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SourceRef(BaseModel):
    """一条证据引用：哪个检索块、原文哪句话。"""

    chunk_id: str = Field(description="检索结果里的块 id，如 P-0042")
    quote: str = Field(default="", max_length=200, description="支撑该结论的原文片段")


class FeatureItem(BaseModel):
    """需求拆解产出的一个功能模块。"""

    id: str = Field(description="F-01 形式")
    name: str
    summary: str
    rules: list[str] = Field(default_factory=list, description="可验证的业务规则")
    risks: list[str] = Field(default_factory=list, description="预判的风险点")
    sources: list[SourceRef] = Field(default_factory=list)

    @field_validator("rules", "risks", "sources", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return [] if v is None else v


class TestMap(BaseModel):
    """Skill 1 输出：测试地图。也是人工确认节点的审核对象。"""

    prd_id: str
    features: list[FeatureItem]

    def feature_ids(self) -> set[str]:
        return {f.id for f in self.features}


class TestPoint(BaseModel):
    """Skill 2 输出：一个测试点（还不是用例，只有"测什么"和"为什么"）。"""

    id: str
    feature_id: str
    category: Category
    title: str = Field(min_length=6)
    rationale: str = ""
    priority: Priority = "P1"
    sources: list[SourceRef] = Field(default_factory=list)


class TestPointSet(BaseModel):
    test_points: list[TestPoint]


class Step(BaseModel):
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)


class TestCase(BaseModel):
    """Skill 3 输出：结构化用例。"""

    id: str
    test_point_id: str
    title: str = Field(min_length=6)
    category: Category
    priority: Priority = "P1"
    preconditions: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    test_data: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class TestCaseSet(BaseModel):
    cases: list[TestCase]


class Issue(BaseModel):
    type: Literal["coverage", "duplicate", "format", "vague", "hallucination", "traceability", "risk"]
    severity: Literal["blocker", "major", "minor"]
    message: str
    ref: str = ""


class ReviewReport(BaseModel):
    """Skill 4 输出：评审报告。gate=revise 时不允许直接导出。"""

    score: int = Field(ge=0, le=100)
    gate: Literal["pass", "revise"]
    coverage: dict[str, int] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class AdoptionVerdict(BaseModel):
    """人工逐条裁决 —— 采纳率指标的原始数据，必须由人填写。"""

    case_id: str
    verdict: Literal["usable", "needs_edit", "unusable"]
    note: str = ""


class LlmCall(BaseModel):
    """一次 LLM 调用的审计记录。评测报告里的 token / 延迟 / 重试次数都来自这里。"""

    task: str
    mode: str
    model: str
    ok: bool
    attempt: int
    latency_ms: int
    prompt_chars: int
    response_chars: int
    error: str = ""
    at: str = Field(default_factory=_now)


class StageRecord(BaseModel):
    stage: str
    started_at: str = Field(default_factory=_now)
    finished_at: str | None = None
    duration_ms: int = 0
    ok: bool = True
    error: str = ""
    # 该阶段的质量信号（只放标量，便于汇总图表）
    metrics: dict[str, float] = Field(default_factory=dict)
    # 结构化明细（如六类覆盖计数），不参与聚合
    details: dict[str, Any] = Field(default_factory=dict)


class RunState(BaseModel):
    run_id: str
    prd_id: str
    mode: Literal["bare", "rag", "rag_hitl"]
    status: RunStatus = "running"
    stage: Stage = "s1_testmap"
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    llm_mode: str = "mock"
    stages: list[StageRecord] = Field(default_factory=list)
    testmap_ai: TestMap | None = None
    testmap_final: TestMap | None = None
    hitl: dict = Field(default_factory=dict)
    testpoints: TestPointSet | None = None
    cases: TestCaseSet | None = None
    review: ReviewReport | None = None
    adoption: list[AdoptionVerdict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = _now()
