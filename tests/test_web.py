"""前端：模板必须能被 Vue 编译器编译（防"白屏但 HTTP 200"）。

依赖 Node 只是为了跑校验脚本；没有 Node 时跳过，不影响后端测试。
"""

from __future__ import annotations

import shutil
import subprocess
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
