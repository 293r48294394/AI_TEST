"""pytest 公共夹具：全部测试强制离线模式，不产生网络请求、不写真实 data/runs。

注意：这里不用 pytest 的 tmp_path —— 本机受限环境下 tempfile.mkdtemp 生成的
0o700 目录无法写入（见 README「环境约束」）。改用工作区内自建目录，行为可预测。
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atp.config import Settings  # noqa: E402
from atp.parsers.prd import parse_text  # noqa: E402
from atp.pipeline.orchestrator import Orchestrator  # noqa: E402
from atp.store import RunStore  # noqa: E402

SAMPLE_PRD = """# 优惠券中心 PRD v1.2

## 1 背景与目标
营销活动需要统一的发券能力，一次活动平均发放 3 万张券。

## 2.1 优惠券创建
运营可创建满减券与折扣券。单张券面额不得超过 500 元，折扣不得低于 5 折。
创建时需填写有效期，结束时间必须晚于开始时间。

## 2.2 用户领取
用户在活动页领取优惠券。单用户最多领取 3 张①②。
库存扣减必须使用原子操作，超卖为 P0 缺陷。

## 2.3 下单使用
结算页按抵扣金额降序展示可用券。折扣券与满减券不可叠加，但可与运费券叠加③。
门槛按商品实付金额计算，不含运费。

## 2.4 退款返还
整单退款时返还优惠券并恢复可用状态。部分退款按实付金额比例返还④。
退款后券的过期时间不变。

## 3 业务规则
| 编号 | 规则 |
| R1 | 领券接口必须幂等 |
| R2 | 券核销后不可撤销 |

① 单用户限领 3 张，含跨活动累计，不含客服补偿券。
② 库存预占失败时不记入限领次数。
③ 折扣券与满减券互斥，运费券可同时使用。
④ 返还金额最小 0.01 元，舍去不足部分。

## 4 待定问题
Q1：券过期后是否可恢复？A：不可恢复，仅支持重新发放。
"""


@pytest.fixture
def prd_text() -> str:
    return SAMPLE_PRD


@pytest.fixture
def workdir():
    base = Path(__file__).resolve().parent / "_work"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def settings(workdir) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        llm_mode="mock",
        api_key="",
        runs_dir=workdir / "runs",
        data_dir=ROOT / "data",
        require_human_review=True,
        max_repair=1,
        max_cases_per_call=4,
        retrieval_top_k=4,
    )


@pytest.fixture
def parsed(prd_text):
    return parse_text(prd_text, source="sample.md", prd_id="sample")


@pytest.fixture
def orc(settings):
    return Orchestrator(settings, RunStore(settings))


@pytest.fixture
def state(orc, parsed):
    """跑完 s1、停在人工确认闸门的运行状态。"""
    return orc.start(parsed, mode="rag_hitl")
