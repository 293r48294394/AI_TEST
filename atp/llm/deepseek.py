"""DeepSeek（OpenAI 兼容）客户端，仅用 stdlib urllib。

为什么要自己写而不是装 openai SDK：
- 本机 pip 安装受限（见 README 的"环境约束"），而 httpx/openai 是纯第三方依赖；
- 需求只有"发 POST、取 choices[0].message.content"，120 行足够，还能精确控制重试与审计字段。
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request

from ..util import est_tokens
from .base import CallResult, ClientOptions, LLMClient, Message

_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """不可重试或重试耗尽的调用失败。"""


class DeepSeekClient(LLMClient):
    mode = "deepseek"

    def __init__(self, api_key: str, base_url: str, model: str, options: ClientOptions | None = None):
        if not api_key:
            raise LLMError("缺少 LLM_API_KEY：请在 .env 里配置，或把 ATP_LLM_MODE 设为 mock")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.opt = options or ClientOptions()
        self.on_call = None  # 由编排器绑定：fn(dict) -> None

    # ------------------------------------------------------------------ public
    def chat(
        self,
        messages: list[Message],
        *,
        task: str,
        json_mode: bool = True,
        max_tokens: int = 4096,
        payload: dict | None = None,
    ) -> CallResult:
        body: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.opt.temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        prompt_chars = sum(len(m.content) for m in messages)
        last_err = ""
        for attempt in range(1, self.opt.max_retries + 1):
            started = time.perf_counter()
            try:
                raw = self._post(body)
                latency = int((time.perf_counter() - started) * 1000)
                self._emit(
                    {
                        "mode": self.mode,
                        "model": self.model,
                        "ok": True,
                        "attempt": attempt,
                        "latency_ms": latency,
                        "prompt_chars": prompt_chars,
                        "response_chars": len(raw),
                        "est_prompt_tokens": est_tokens("".join(m.content for m in messages)),
                    }
                )
                return CallResult(raw, attempt=attempt, latency_ms=latency, prompt_chars=prompt_chars)
            except _Retryable as exc:
                latency = int((time.perf_counter() - started) * 1000)
                last_err = str(exc)
                self._emit(
                    {
                        "mode": self.mode,
                        "model": self.model,
                        "ok": False,
                        "attempt": attempt,
                        "latency_ms": latency,
                        "prompt_chars": prompt_chars,
                        "response_chars": 0,
                        "error": last_err,
                    }
                )
                if attempt >= self.opt.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1) + random.random() * 0.3, 8))
            except _Fatal as exc:
                self._emit(
                    {
                        "mode": self.mode,
                        "model": self.model,
                        "ok": False,
                        "attempt": attempt,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "prompt_chars": prompt_chars,
                        "response_chars": 0,
                        "error": str(exc),
                    }
                )
                raise LLMError(str(exc)) from exc
        raise LLMError(f"LLM 调用失败（已重试 {self.opt.max_retries} 次）：{last_err}")

    # ----------------------------------------------------------------- private
    def _post(self, body: dict) -> str:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.opt.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            if exc.code in _RETRY_STATUS:
                raise _Retryable(f"HTTP {exc.code}: {detail}") from exc
            raise _Fatal(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise _Retryable(f"网络错误: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise _Retryable(f"响应不是 JSON: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise _Retryable(f"响应结构异常: {str(data)[:200]}") from exc
        if not content.strip():
            raise _Retryable("模型返回空内容")
        return content

    def _emit(self, row: dict) -> None:
        if self.on_call:
            self.on_call(row)


class _Retryable(Exception):
    pass


class _Fatal(Exception):
    pass
