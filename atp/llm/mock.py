"""离线启发式基线（HeuristicClient）。

定位要诚实：这**不是**在用 AI 生成用例，而是一个规则基线。
它的价值有两个：
1. 没有 API key / 断网时，整条流水线（含人工确认、导出、评测）依然能跑通并进 CI；
2. 评测时它是"下限参照"——如果真实模型打不过这个规则基线，说明提示词或检索出了问题。

实现方式：不解析提示词文本（那太脆），而是由 Skill 通过 payload 传入结构化输入，
按与模型完全相同的输出契约合成 JSON。这样两条路径的差异只在上游"谁在做判断"。
"""

from __future__ import annotations

import json
import re
import time

from .base import CallResult, LLMClient, Message

_NUM_RULE = re.compile(r"\d+(?:\.\d+)?\s*(?:张|次|元|天|小时|分钟|秒|%|个|条)")
_RULE_HINT = re.compile(r"(必须|不得|不可|不能|最多|至少|仅|只允许|上限|不得超过|需|应|禁止|支持)")
_SECTION = re.compile(r"^(#{1,6})\s*(.+)$")
_FOOTNOTE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")


class HeuristicClient(LLMClient):
    mode = "mock"

    def __init__(self, model: str = "heuristic-baseline-v1"):
        self.model = model
        self.on_call = None

    def chat(
        self,
        messages: list[Message],
        *,
        task: str,
        json_mode: bool = True,
        max_tokens: int = 4096,
        payload: dict | None = None,
    ) -> CallResult:
        started = time.perf_counter()
        payload = payload or {}
        handler = getattr(self, f"_task_{task}", None)
        if handler is None:
            data = {"error": f"启发式基线不支持任务 {task}"}
        else:
            data = handler(payload)
        raw = json.dumps(data, ensure_ascii=False)
        latency = int((time.perf_counter() - started) * 1000)
        if self.on_call:
            self.on_call(
                {
                    "mode": self.mode,
                    "model": self.model,
                    "ok": True,
                    "attempt": 1,
                    "latency_ms": latency,
                    "prompt_chars": sum(len(m.content) for m in messages),
                    "response_chars": len(raw),
                    "task": task,
                }
            )
        return CallResult(raw, attempt=1, latency_ms=latency)

    # ------------------------------------------------------------------ tasks
    def _task_s1_testmap(self, payload: dict) -> dict:
        prd_id = payload.get("prd_id", "prd")
        chunks = payload.get("chunks", [])
        features: list[dict] = []
        seen: set[str] = set()
        for chunk in chunks:
            heading = (chunk.get("heading") or "").strip()
            text = chunk.get("text") or ""
            name = self._feature_name(heading, text)
            if not name or name in seen or len(text) < 20:
                continue
            rules = [
                line.strip()
                for line in re.split(r"[\n。；;]", text)
                if _RULE_HINT.search(line) and 6 <= len(line.strip()) <= 120
            ][:4]
            seen.add(name)
            features.append(
                {
                    "id": f"F-{len(features) + 1:02d}",
                    "name": name,
                    "summary": self._summary(text),
                    "rules": rules,
                    "risks": [],
                    "sources": [{"chunk_id": chunk.get("chunk_id", ""), "quote": self._quote(text)}],
                }
            )
            if len(features) >= 8:
                break
        if len(features) < 2:
            # 标题不含功能关键词（如"背景""附录"）时，用章节标题兜底，保证下游有可挂载的 feature
            for chunk in chunks:
                heading = re.sub(r"^[0-9.\s]+", "", (chunk.get("heading") or "").strip())
                if not heading or heading in seen or len(heading) > 24:
                    continue
                seen.add(heading)
                text = chunk.get("text") or ""
                features.append(
                    {
                        "id": f"F-{len(features) + 1:02d}",
                        "name": heading,
                        "summary": self._summary(text),
                        "rules": [
                            line.strip()
                            for line in re.split(r"[\n。；;]", text)
                            if _RULE_HINT.search(line) and 6 <= len(line.strip()) <= 120
                        ][:3],
                        "risks": [],
                        "sources": [{"chunk_id": chunk.get("chunk_id", ""), "quote": self._quote(text)}],
                    }
                )
                if len(features) >= 3:
                    break
        return {"prd_id": prd_id, "features": features}

    def _task_s2_testpoints(self, payload: dict) -> dict:
        points: list[dict] = []
        for feature in payload.get("features", []):
            fid = feature.get("id")
            name = feature.get("name") or "功能"
            src = feature.get("sources") or []
            rules = feature.get("rules") or []
            noun = self._noun(name)

            def add(category: str, title: str, rationale: str, priority: str = "P1") -> None:
                points.append(
                    {
                        "id": f"TP-{len(points) + 1:03d}",
                        "feature_id": fid,
                        "category": category,
                        "title": title,
                        "rationale": rationale,
                        "priority": priority,
                        "sources": src[:1],
                    }
                )

            add("normal", f"{noun}在合法输入下成功执行并返回预期结果", f"覆盖 {name} 主流程", "P0")
            numeric = [r for r in rules if _NUM_RULE.search(r)]
            if numeric:
                add("boundary", f"{noun}在阈值边界（{_NUM_RULE.search(numeric[0]).group(0)}）处表现正确", "规则含数值阈值，需验证临界值", "P1")
            else:
                add("boundary", f"{noun}在输入为空或极值时不崩溃并给出明确提示", "缺少显式阈值，按通用边界处理", "P1")
            add("exception", f"{noun}在参数非法（缺失/类型错误/超长）时返回错误码且不落脏数据", "负向路径", "P1")
            for rule in rules[:2]:
                add("normal", f"{noun}满足规则：{rule[:40]}", "逐条规则验证", "P1")
        if points and not any(p["category"] == "security" for p in points):
            points.append(
                {
                    "id": f"TP-{len(points) + 1:03d}",
                    "feature_id": points[0]["feature_id"],
                    "category": "security",
                    "title": "越权访问：普通用户调用管理侧接口被拒绝",
                    "rationale": "接口鉴权基线检查",
                    "priority": "P0",
                    "sources": points[0]["sources"],
                }
            )
        # 规则基线也要满足六类覆盖门槛，否则它连"可被评估的基线"都算不上
        for cat, title in (
            ("compat", "在 Chrome/Android 主流版本上核心链路的展示与操作一致"),
            ("performance", "领取峰值并发下 P99 响应时间不超过 200ms 且无超卖"),
            ("security", "重复提交同一领取请求仅生效一次（幂等）"),
        ):
            if points and not any(p["category"] == cat for p in points):
                points.append(
                    {
                        "id": f"TP-{len(points) + 1:03d}",
                        "feature_id": points[0]["feature_id"],
                        "category": cat,
                        "title": title,
                        "rationale": "通用质量属性基线",
                        "priority": "P1",
                        "sources": points[0]["sources"],
                    }
                )
        return {"test_points": points}

    def _task_s3_testcases(self, payload: dict) -> dict:
        cases: list[dict] = []
        for point in payload.get("points", []):
            title = point.get("title") or "场景"
            category = point.get("category", "normal")
            cases.append(
                {
                    "id": f"TC-{len(cases) + 1:03d}",
                    "test_point_id": point.get("id"),
                    "title": title if len(title) >= 6 else f"验证{title}",
                    "category": category,
                    "priority": point.get("priority", "P1"),
                    "preconditions": ["测试环境可用", "已准备满足前置条件的数据"],
                    "steps": [
                        {"action": "构造前置数据并登录具备权限的账号", "expected": "进入可操作状态"},
                        {"action": f"执行场景：{title}", "expected": "接口返回 200 且响应体结构合法"},
                        {"action": "查询数据库/缓存核对状态变化", "expected": "数据状态与业务规则一致"},
                    ],
                    "test_data": {},
                    "tags": [category],
                }
            )
        return {"cases": cases}

    def _task_s4_llm_review(self, payload: dict) -> dict:
        cases = payload.get("cases", [])
        suggestions = []
        if any(not c.get("test_data") for c in cases):
            suggestions.append("部分用例的 test_data 为空，建议补齐具体输入数据以提升可执行性")
        if len(cases) < 10:
            suggestions.append("用例总量偏少，建议补充异常与安全类场景")
        return {"suggestions": suggestions[:3], "semantic_issues": []}

    def _task_s5_fidelity_judge(self, payload: dict) -> dict:
        """离线基线不做语义判断：统一给 3 分，避免把"猜"包装成"评"。"""
        pairs = payload.get("pairs", [])
        return {
            "verdicts": [
                {"pair_index": i, "score": 3, "reason": "离线基线不提供语义判断"}
                for i in range(len(pairs))
            ]
        }

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _feature_name(heading: str, text: str) -> str:
        heading = re.sub(r"^[0-9.\s]+", "", heading).strip()
        if not heading or len(heading) > 30:
            return ""
        if re.search(r"(功能|需求|领取|创建|使用|核销|退款|过期|库存|权限|优惠券|下单|支付)", heading):
            return heading
        return ""

    @staticmethod
    def _summary(text: str) -> str:
        first = re.split(r"[\n。]", text.strip())[0]
        return first[:120]

    @staticmethod
    def _quote(text: str) -> str:
        m = _FOOTNOTE.search(text)
        if m:
            start = max(m.start() - 30, 0)
            return text[start : m.start() + 60].replace("\n", " ")[:120]
        return text.strip().replace("\n", " ")[:80]

    @staticmethod
    def _noun(name: str) -> str:
        return re.sub(r"^[0-9.\s]+", "", name).strip() or "功能"
