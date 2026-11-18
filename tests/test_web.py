"""前端：模板必须能被 Vue 编译器编译（防"白屏但 HTTP 200"）。

依赖 Node 只是为了跑校验脚本；没有 Node 时跳过，不影响后端测试。
"""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_vue_template_compiles():
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 Node，跳过前端模板校验")
    result = subprocess.run(
        [node, str(ROOT / "scripts" / "check_web.js")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"模板编译失败:\n{result.stdout}\n{result.stderr}"


def test_single_file_app_has_no_cdn_dependency():
    """前端必须断网可用：页面里不应出现外部 CDN 地址。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html
    assert "cdnjs" not in html
    assert '/vendor/vue.global.prod.js' in html
    assert '/vendor/element-plus.full.min.js' in html


class _TagBalance(HTMLParser):
    """检查自定义组件是否显式闭合。

    这是被真实故障教育出来的测试：<el-input ... /> 这种自闭合写法在浏览器 HTML
    解析里会被忽略（非 void 元素），后续内容被吞进去，Vue 报 compiler error 30
    "Element is missing end tag"，页面白屏。Node 里的 Vue.compile 用的是 base 模式，
    认自闭合，所以查不出来 —— 必须按 HTML 规则自己查。
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.self_closed: list[str] = []
        self.unclosed: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        if tag.startswith("el-") or tag == "template":
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        if tag.startswith("el-"):
            self.self_closed.append(f"<{tag} /> (line {self.getpos()[0]})")

    def handle_endtag(self, tag):
        if not (tag.startswith("el-") or tag == "template"):
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            self.unclosed.append(f"</{tag}> 不匹配（line {self.getpos()[0]}）")

    def result(self) -> list[str]:
        problems = self.self_closed + self.unclosed
        problems += [f"<{t}> 未闭合（line {ln}）" for t, ln in self.stack]
        return problems


def test_custom_elements_are_explicitly_closed():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    checker = _TagBalance()
    checker.feed(html)
    assert checker.result() == [], "模板里有未闭合/自闭合的自定义组件：\n" + "\n".join(checker.result())
