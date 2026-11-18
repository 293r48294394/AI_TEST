"""运行配置：全部来自环境变量，带安全默认值。

设计约束：核心链路零第三方依赖，因此这里不用 pydantic-settings / python-dotenv，
只用一个 12 行的 .env 解析器。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"


def load_dotenv(path: Path | None = None) -> None:
    """把 .env 里的键值加载进 os.environ（已存在的环境变量优先，不覆盖）。"""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    # ---- LLM ----
    llm_mode: str = "mock"  # deepseek | mock
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    timeout_s: float = 120.0
    max_retries: int = 3
    max_repair: int = 2  # JSON 校验失败后的重试轮数

    # ---- 流水线 ----
    require_human_review: bool = True
    max_cases_per_call: int = 8  # 用例生成分批，控制单次上下文体积
    retrieval_top_k: int = 6

    # ---- 目录 ----
    data_dir: Path = DEFAULT_DATA_DIR
    runs_dir: Path = DEFAULT_DATA_DIR / "runs"

    @property
    def prd_dir(self) -> Path:
        return self.data_dir / "prd"

    @property
    def golden_dir(self) -> Path:
        return self.data_dir / "golden"

    @property
    def live_llm(self) -> bool:
        """是否真的会发网络请求。"""
        return self.llm_mode == "deepseek" and bool(self.api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        data_dir = Path(os.environ.get("ATP_DATA_DIR", str(DEFAULT_DATA_DIR)))
        return cls(
            llm_mode=os.environ.get("ATP_LLM_MODE", "mock").lower(),
            base_url=os.environ.get("CHECKME_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
            temperature=float(os.environ.get("ATP_TEMPERATURE", "0.2")),
            timeout_s=float(os.environ.get("ATP_TIMEOUT_S", "120")),
            max_retries=int(os.environ.get("ATP_MAX_RETRIES", "3")),
            max_repair=int(os.environ.get("ATP_MAX_REPAIR", "2")),
            require_human_review=os.environ.get("ATP_REQUIRE_HUMAN", "1") not in {"0", "false", "False"},
            max_cases_per_call=int(os.environ.get("ATP_MAX_CASES_PER_CALL", "8")),
            retrieval_top_k=int(os.environ.get("ATP_TOP_K", "6")),
            data_dir=data_dir,
            runs_dir=Path(os.environ.get("ATP_RUNS_DIR", str(data_dir / "runs"))),
        )
