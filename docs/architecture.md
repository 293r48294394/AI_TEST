# 架构与关键设计

## 1. 数据流

```
PRD 文件
  │  parsers/prd.py：按标题分段 → 段内切块（≤700 字）→ chunk_id = P-0001…
  ▼
chunks[] ──► retrieval/bm25.py：BM25 + 符号加权 + 多查询 RRF ──► hits[]（带 chunk_id 与分数）
  │
  ▼
① s1_testmap ──► TestMap {features[{id,name,summary,rules[],risks[],sources[]}]}
  │                     │
  │                     └── 校验：≥2 个功能 / id 唯一 / sources ⊆ hits 的 chunk_id
  ▼
⏸ gate1 人工确认 ──► testmap_final（与草稿做 diff，落盘 hitl_diff.json）
  │
  ▼
② s2_testpoints ──► TestPointSet {test_points[{id,feature_id,category,title,rationale,priority,sources[]}]}
  │                     └── 校验：六类覆盖门槛 / feature_id ∈ 测试地图
  ▼
③ s3_testcases ──► 按 8 条一批 ──► TestCaseSet {cases[{id,test_point_id,title,preconditions[],steps[],test_data,tags[]}]}
  │                     └── 校验：每点恰 1 条 / 步骤≥2 / 本批全覆盖 / id 唯一
  ▼
④ s4_review ──► ReviewReport {score,gate,coverage,issues[],suggestions[]}
  │                 ├── 规则层：覆盖/查重/可追溯/空话（确定性）
  │                 └── 语义层：LLM 找盲区/不可执行/幻觉
  ▼
export/xlsx.py ──► gate=revise 时抛 ExportBlocked（409），否则导出三表
```

## 2. 状态机

```
s1_testmap ──► [gate1] ──► s2_testpoints ──► s3_testcases ──► s4_review ──► done
                  │
                  ├─ 模式 bare / rag：不停留，直接进入 s2
                  └─ 模式 rag_hitl：status=awaiting_human，等 POST /resume
                       └─ resume 携带人工修改版 TestMap → 记录 diff → 继续
```

状态字段：`RunState.status ∈ {running, awaiting_human, failed, completed, aborted}`，
`stage ∈ {s1_testmap, gate1, s2_testpoints, s3_testcases, s4_review, done}`。

**为什么 resume 走 `prd_from_artifacts` 而不是重读源文件**：历史运行必须能复现，
所以每个 run 目录自带 `artifacts/00_prd_chunks.json`，源文件被改被删都不影响回放。

## 3. 每次运行的落盘结构

```
data/runs/<run_id>/
  state.json                可 resume 的完整状态
  events.jsonl              阶段事件（开始/结束/失败/闸门/人工确认）
  llm_calls.jsonl           每次调用的 task/尝试/延迟/字符数/错误
  artifacts/
    00_prd_chunks.json      PRD 切块（运行自包含）
    00_prd_stats.json       字数/段数/块数/是否有脚注表格
    01_testmap_ai.json      AI 草稿
    02_testmap_final.json   人工确认版
    03_testpoints.json
    04_cases.json
    05_review.json
    hitl_diff.json          +新增 / -删除 / ~修改 的明细
```

这套结构的作用：**任何一次运行的每个中间产物都能单独打开核对**，
面试时可以直接说"第 3 次运行的 s2 在第一次输出时漏了 performance 类，
校验器回喂后第二次补齐，logs 里 attempts=2"。

## 4. 校验器清单（防幻觉的全部手段）

| 位置 | 校验 | 失败后果 |
|---|---|---|
| `llm/schema.py` | JSON 可解析（容错 ```json 围栏与前后废话） | 回喂"请只输出 JSON" |
| | pydantic 字段校验（类型/长度/枚举） | 回喂具体字段错误 + 目标 schema 摘要 |
| | `make_citation_validator` | 回喂"只允许引用 P-0001/P-0002…" |
| | `uniqueness_validator` | 回喂 id 重复清单 |
| | `category_coverage_validator` | 回喂"类别 X 至少 N 条，当前 M 条" |
| `coverage/rules.py` | 六类覆盖 / 功能零覆盖 / 用例孤儿 / 标题查重 / 空话检测 / test_data 缺失 | 形成 issue，严重级别决定评审分与闸门 |

重试上限 `ATP_MAX_REPAIR`（默认 2，即最多 3 次尝试），耗尽抛 `SchemaRepairFailed`，
阶段标记 `ok=false` 并记录错误，**不产生半成品**。

## 5. 评审打分

```
score = max(0, 100 - Σ 权重(issue))
权重：blocker=20, major=8, minor=3
gate  = pass 当且仅当 无 blocker 且 score ≥ 75
```

分数构成可逐条解释（哪个 issue 扣了多少），不是模型给的玄学分。
语义层的问题也会进 issue 列表，但严重级别由模型给、人可复核。

## 6. 可替换点

| 想换什么 | 改哪里 | 影响面 |
|---|---|---|
| 换模型/厂商 | `llm/deepseek.py` 或新增实现 `LLMClient` | 无（Skills 只依赖协议） |
| 换向量检索 | 实现 `Retriever.retrieve()` | 无（评测脚本不动） |
| 换编排框架（LangGraph） | 把 `Orchestrator._stage_*` 换成 node | Skill 层不动 |
| 换导出格式（Xray/TestLink/CSV） | 加 `export/` 实现 | 只影响导出接口 |
| 换被测文档类型 | `parsers/prd.py` 加分支 | 只影响 PRD 入口 |
| 换 Fidelity 判定 | `eval/judge.py`（1–5 分）或 `eval/metrics.fidelity_heuristic`（代理） | 报告里的 `fidelity_judge` 字段自动标注 |
| 换匹配算法 | `eval/metrics.match_points` | 阈值敏感性 `sweep_threshold` 会一并变化 |

## 7. 前端

单文件 `web/index.html`：Vue 3 + Element Plus（`web/vendor/` 本地化，断网可用）。
页面职责只有四个：看中间产物、在闸门处改测试地图、逐条裁决、导出。
不做登录/权限/多租户——那是工程脚手架，不是这个项目的论点。

模板正确性由 `scripts/check_web.js` 保证：用 Vue 编译器在 Node 里编译模板并收集报错。
没有浏览器也能拦住"页面白屏但 HTTP 200"这类问题（本仓库开发时确实靠它发现并修掉过
`document is not defined` 的编译环境问题）。
