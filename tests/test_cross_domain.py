"""跨文档泛化：换一个业务域，验证流水线没有把"优惠券"写死。

这类测试的价值不在于数字，而在于"换一份 PRD 还能不能跑"。
本轮的对照组：coupon-center（营销） vs account-login（账号安全）。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGIN_PRD = ROOT / "data" / "prd" / "account-login-prd.md"
LOGIN_GOLDEN = ROOT / "data" / "golden" / "account-login-golden.json"


def test_login_domain_runs_end_to_end(orc):
    from atp.parsers.prd import parse_file

    prd = parse_file(LOGIN_PRD, prd_id="account-login-prd")
    state = orc.start(prd, mode="rag")
    assert state.status == "completed"

    features = [f.name for f in state.testmap_final.features]
    assert len(features) >= 3, f"功能拆解过少：{features}"
    assert any("登录" in f for f in features), features

    cats = {p.category for p in state.testpoints.test_points}
    assert cats == {"normal", "exception", "boundary", "security", "compat", "performance"}

    assert state.cases and len(state.cases.cases) >= 10
    assert state.review is not None
    # 基线很弱，评审可以不过（revise 是合法结果），但必须给出结论
    assert state.review.gate in {"pass", "revise"}


def test_login_golden_set_is_clean():
    from atp.eval.golden_lint import lint_files

    rep = lint_files(LOGIN_PRD, LOGIN_GOLDEN)
    assert rep.ok, [f"{i.gid}: {i.message}" for i in rep.errors]
    assert 10 <= rep.stats["total"] <= 50


def test_prompts_are_domain_neutral():
    """四个 Skill 的提示词里不应出现具体业务名词，否则换个域就失效。"""
    from atp.skills import s1_testmap, s2_testpoints, s3_testcases, s4_review

    prompts = {
        "s1": s1_testmap.SYSTEM,
        "s2": s2_testpoints.SYSTEM,
        "s3": s3_testcases.SYSTEM,
        "s4": s4_review.SYSTEM,
    }
    banned = ("优惠券", "满减", "折扣券", "运费券", "领券")
    for name, text in prompts.items():
        found = [w for w in banned if w in text]
        assert not found, f"{name} 提示词含业务域词 {found}，换领域会失效"


def test_parser_handles_both_documents():
    """两份文档的标题风格不同（`4.1 优惠券创建` vs `3.1 密码登录`），分段都应成立。"""
    from atp.parsers.prd import parse_file

    coupon = parse_file(ROOT / "data" / "prd" / "coupon-center-prd.md")
    login = parse_file(LOGIN_PRD)
    for prd in (coupon, login):
        assert len(prd.chunks) >= 8
        assert prd.stats["has_tables"] is True
        assert all(c.chunk_id.startswith("P-") for c in prd.chunks)
