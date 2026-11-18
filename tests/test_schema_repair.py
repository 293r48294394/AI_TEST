"""JSON 契约层：解析容错、字段校验、引用校验、修复重试、失败收敛。"""

from __future__ import annotations

import json

import pytest

from atp.llm.base import CallResult
from atp.llm.schema import (
    SchemaRepairFailed,
    call_json,
    category_coverage_validator,
    combine,
    extract_json,
    make_citation_validator,
    uniqueness_validator,
)
from atp.models import TestMap, TestPointSet


class FakeClient:
    mode = "fake"
    model = "fake"

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.seen: list[list] = []

    def chat(self, messages, *, task, json_mode=True, max_tokens=4096, payload=None):
        self.seen.append(messages)
        if not self.outputs:
            raise AssertionError("FakeClient 输出已耗尽")
        return CallResult(self.outputs.pop(0))


VALID_MAP = {
    "prd_id": "t",
    "features": [
        {"id": "F-01", "name": "领取", "summary": "s", "rules": ["限领 3 张"], "risks": [], "sources": [{"chunk_id": "P-0001"}]},
        {"id": "F-02", "name": "使用", "summary": "s", "rules": [], "risks": [], "sources": [{"chunk_id": "P-0002"}]},
    ],
}


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a":1}\n```') == '{"a":1}'
    assert extract_json('好的，结果如下：{"a":1} 以上') == '{"a":1}'
    assert extract_json('{"a":1}') == '{"a":1}'


def test_first_try_success():
    client = FakeClient([json.dumps(VALID_MAP, ensure_ascii=False)])
    out = call_json(client, task="t", system="s", user="u", model_cls=TestMap)
    assert out.first_try_ok is True
    assert out.attempts == 1
    assert isinstance(out.value, TestMap)


def test_repairs_invalid_json():
    client = FakeClient(["这不是 JSON", json.dumps(VALID_MAP, ensure_ascii=False)])
    out = call_json(client, task="t", system="s", user="u", model_cls=TestMap, max_repair=2)
    assert out.attempts == 2
    assert out.first_try_ok is False
    # 第二次调用必须把错误反馈带给模型
    feedback = client.seen[1][-1].content
    assert "JSON" in feedback


def test_repairs_missing_field():
    broken = {"prd_id": "t", "features": [{"id": "F-01", "name": "领取"}]}  # 缺 summary
    client = FakeClient([json.dumps(broken, ensure_ascii=False), json.dumps(VALID_MAP, ensure_ascii=False)])
    out = call_json(client, task="t", system="s", user="u", model_cls=TestMap, max_repair=2)
    assert out.attempts == 2
    assert "summary" in client.seen[1][-1].content


def test_citation_validator_rejects_unknown_chunk():
    bad = json.loads(json.dumps(VALID_MAP))
    bad["features"][0]["sources"] = [{"chunk_id": "P-9999"}]
    client = FakeClient([json.dumps(bad, ensure_ascii=False), json.dumps(VALID_MAP, ensure_ascii=False)])
    out = call_json(
        client,
        task="t",
        system="s",
        user="u",
        model_cls=TestMap,
        validator=make_citation_validator({"P-0001", "P-0002"}),
        max_repair=2,
    )
    assert out.attempts == 2
    assert "P-9999" in client.seen[1][-1].content


def test_exhausting_repairs_raises():
    client = FakeClient(["bad"] * 3)
    with pytest.raises(SchemaRepairFailed) as exc:
        call_json(client, task="t", system="s", user="u", model_cls=TestMap, max_repair=2)
    assert len(exc.value.attempts) == 3


def test_uniqueness_and_coverage_validators():
    points = TestPointSet(
        test_points=[
            {"id": "TP-001", "feature_id": "F-01", "category": "normal", "title": "正常领取成功并扣减库存", "priority": "P0"},
            {"id": "TP-001", "feature_id": "F-01", "category": "normal", "title": "重复 id 的用例", "priority": "P1"},
        ]
    )
    issues = combine(uniqueness_validator(), category_coverage_validator({"boundary": 1}))(points)
    assert any("重复" in i for i in issues)
    assert any("boundary" in i for i in issues)
