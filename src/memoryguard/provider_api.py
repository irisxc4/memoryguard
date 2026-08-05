"""Provider API 注入:通过外部 LLM API 实现真模型能力。

零第三方依赖:纯标准库 urllib + json。
支持三种 provider 类型:
- openai_compatible: OpenAI 官方 + 所有兼容 /v1/chat/completions 的服务(Ollama/vLLM/LM Studio)
- anthropic: Anthropic Claude /v1/messages
- 自定义:用户实现 ProviderBackend Protocol

配置方式(优先级从高到低):
1. 代码注入: set_provider(backend)
2. 环境变量: MEMORYGUARD_PROVIDER_TYPE / MEMORYGUARD_PROVIDER_API_KEY / ...
3. config.local.json: .memoryguard/config.local.json 的 "provider" 字段

安全:
- API key 只从环境变量或 config.local.json 读取,永不写入 config.json
- 请求失败回退 heuristic,不阻塞主流程
- 所有请求带 30s 超时
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# ===========================================================================
# Provider 配置
# ===========================================================================


@dataclass
class ProviderConfig:
    """Provider API 配置。"""
    provider_type: str = ""  # openai_compatible | anthropic | ""
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    embedding_model: str = ""
    timeout: int = 30
    # 额外 header
    extra_headers: dict[str, str] = field(default_factory=dict)

    def is_configured(self) -> bool:
        return bool(self.provider_type and self.api_base and self.model)

    def to_safe_dict(self) -> dict[str, Any]:
        """安全序列化(隐藏 api_key)。"""
        d = {
            "provider_type": self.provider_type,
            "api_base": self.api_base,
            "model": self.model,
            "embedding_model": self.embedding_model,
            "timeout": self.timeout,
        }
        d["api_key_configured"] = bool(self.api_key)
        return d


def load_provider_config(workspace: str | Path | None = None) -> ProviderConfig:
    """从环境变量或 config.local.json 加载 provider 配置。

    优先级:环境变量 > config.local.json。
    """
    # 1. 环境变量
    cfg = ProviderConfig(
        provider_type=os.environ.get("MEMORYGUARD_PROVIDER_TYPE", ""),
        api_base=os.environ.get("MEMORYGUARD_PROVIDER_API_BASE", ""),
        api_key=os.environ.get("MEMORYGUARD_PROVIDER_API_KEY", ""),
        model=os.environ.get("MEMORYGUARD_PROVIDER_MODEL", ""),
        embedding_model=os.environ.get("MEMORYGUARD_PROVIDER_EMBEDDING_MODEL", ""),
        timeout=int(os.environ.get("MEMORYGUARD_PROVIDER_TIMEOUT", "30")),
    )
    if cfg.is_configured():
        return cfg

    # 2. config.local.json
    if workspace:
        local_config = Path(workspace) / ".memoryguard" / "config.local.json"
        if local_config.exists():
            try:
                data = json.loads(local_config.read_text(encoding="utf-8"))
                prov = data.get("provider", {})
                if isinstance(prov, dict):
                    return ProviderConfig(
                        provider_type=prov.get("provider_type", cfg.provider_type),
                        api_base=prov.get("api_base", cfg.api_base),
                        api_key=prov.get("api_key", cfg.api_key),
                        model=prov.get("model", cfg.model),
                        embedding_model=prov.get("embedding_model", cfg.embedding_model),
                        timeout=int(prov.get("timeout", cfg.timeout)),
                        extra_headers=prov.get("extra_headers", {}),
                    )
            except (OSError, json.JSONDecodeError, ValueError):
                pass
    return cfg


# ===========================================================================
# HTTP 工具(纯标准库)
# ===========================================================================


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    """POST JSON,返回响应 JSON。失败抛异常。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={**{"Content-Type": "application/json"}, **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ===========================================================================
# ProviderBackend Protocol
# ===========================================================================


class ProviderBackend(Protocol):
    """Provider 后端接口:chat + embed。

    实现者封装具体 API 调用,返回结构化结果。
    """

    def chat(self, system: str, user: str, max_tokens: int = 500) -> str:
        """对话补全,返回助手回复文本。"""
        ...

    def embed(self, text: str) -> list[float]:
        """生成文本 embedding 向量。"""
        ...

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量生成 embedding（KB2）。默认实现循环调用 embed。"""
        ...


# ===========================================================================
# OpenAICompatibleBackend:覆盖 OpenAI / Ollama / vLLM / LM Studio
# ===========================================================================


class OpenAICompatibleBackend:
    """OpenAI 兼容 API 后端。

    覆盖:
    - OpenAI 官方: https://api.openai.com/v1
    - Ollama: http://localhost:11434/v1
    - vLLM: http://localhost:8000/v1
    - LM Studio: http://localhost:1234/v1
    """

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.base = config.api_base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {config.api_key}"}
        self.headers.update(config.extra_headers)

    def chat(self, system: str, user: str, max_tokens: int = 500) -> str:
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        resp = _http_post_json(
            f"{self.base}/chat/completions", body, self.headers, self.config.timeout,
        )
        return resp["choices"][0]["message"]["content"]

    def embed(self, text: str) -> list[float]:
        model = self.config.embedding_model or self.config.model
        body = {"model": model, "input": text}
        resp = _http_post_json(
            f"{self.base}/embeddings", body, self.headers, self.config.timeout,
        )
        return resp["data"][0]["embedding"]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding（KB2）。OpenAI /embeddings 原生支持 input 为 list。"""
        if not texts:
            return []
        model = self.config.embedding_model or self.config.model
        body = {"model": model, "input": texts}
        resp = _http_post_json(
            f"{self.base}/embeddings", body, self.headers, self.config.timeout,
        )
        # 按 index 排序确保顺序与输入一致
        data = sorted(resp["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]


# ===========================================================================
# AnthropicBackend:Anthropic Claude
# ===========================================================================


class AnthropicBackend:
    """Anthropic Claude API 后端。

    API: https://api.anthropic.com/v1/messages
    认证: x-api-key header
    """

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.base = config.api_base.rstrip("/")
        self.headers = {
            "x-api-key": config.api_key,
            "anthropic-version": "2023-06-01",
        }
        self.headers.update(config.extra_headers)

    def chat(self, system: str, user: str, max_tokens: int = 500) -> str:
        body = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        resp = _http_post_json(
            f"{self.base}/messages", body, self.headers, self.config.timeout,
        )
        return resp["content"][0]["text"]

    def embed(self, text: str) -> list[float]:
        # Anthropic 暂无 embedding API,回退 HashBackend
        from .semantic_dedup import HashBackend
        return HashBackend().embed_text(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding（KB2）：Anthropic 无 embedding API，逐个回退 HashBackend。"""
        from .semantic_dedup import HashBackend
        backend = HashBackend()
        return [backend.embed_text(t) for t in texts]


# ===========================================================================
# 工厂 + 自动注入
# ===========================================================================


_provider_backend: ProviderBackend | None = None
_provider_config: ProviderConfig | None = None


def set_provider(backend: ProviderBackend | None) -> None:
    """注入 provider backend。传 None 清除。"""
    global _provider_backend
    _provider_backend = backend


def get_provider(workspace: str | Path | None = None) -> ProviderBackend | None:
    """获取 provider backend。

    优先级:
    1. set_provider 注入的
    2. 从配置自动构建
    """
    global _provider_backend, _provider_config
    if _provider_backend is not None:
        return _provider_backend

    # 自动从配置构建
    if _provider_config is None:
        _provider_config = load_provider_config(workspace)
    cfg = _provider_config
    if not cfg.is_configured():
        return None

    try:
        if cfg.provider_type == "openai_compatible":
            _provider_backend = OpenAICompatibleBackend(cfg)
        elif cfg.provider_type == "anthropic":
            _provider_backend = AnthropicBackend(cfg)
        else:
            return None
    except Exception:
        return None
    return _provider_backend


def clear_provider() -> None:
    """清除 provider(测试用)。"""
    global _provider_backend, _provider_config
    _provider_backend = None
    _provider_config = None
