"""零依赖工具函数：JSON 读写、文本归一化、相似度、粗略 token 估算。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_CJK = re.compile(r"[\u4e00-\u9fff]")
_PUNCT = re.compile(r"[\s\u3000，。、；：？！,.;:?!\"'“”‘’()（）\[\]【】<>《》/\\|_\-—+*#`~]+")
_STOP = {
    "测试", "验证", "检查", "确认", "用例", "场景", "用户", "系统", "接口", "接口的",
    "能够", "可以", "是否", "相关", "对应", "进行", "出现", "可能", "情况",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    """原子写：先写临时文件再替换，避免中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def normalize(text: str) -> str:
    """归一化：全角转半角、去标点、小写。用于标题查重与匹配。"""
    text = unicodedata.normalize("NFKC", text or "")
    return _PUNCT.sub("", text).lower()


def tokens(text: str) -> list[str]:
    """中英混排的粗粒度切分：中文按 2-gram，英文按单词。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    words = re.findall(r"[a-z0-9_.]+", text)
    cjk = "".join(_CJK.findall(text))
    grams = [cjk[i : i + 2] for i in range(len(cjk) - 1)]
    return words + grams


def similarity(a: str, b: str) -> float:
    """0~1 的相似度：先算集合 Jaccard（抗语序），再与序列相似度取大。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(tokens(a)) - _STOP, set(tokens(b)) - _STOP
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    # 包含关系（A 的归一化文本被 B 包含）说明是同一场景的不同措辞
    contain = 1.0 if (na in nb or nb in na) and min(len(na), len(nb)) >= 8 else 0.0
    return round(max(jac, seq, contain), 4)


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 字 1 token，英文约 4 字符 1 token。够用来做预算告警。"""
    cjk = len(_CJK.findall(text))
    other = max(len(text) - cjk, 0)
    return int(cjk + other / 3.5) + 1


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def safe_slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", text).strip("-")
    return (s or "untitled")[:limit]
