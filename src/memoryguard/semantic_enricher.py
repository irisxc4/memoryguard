"""可选语义增强层：分类 + 本地化（启发式，不调外部模型）。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .policies import _VALID_KINDS, classify_kind
from .runtime_v2.text_native import looks_english_text
from .schema_v3 import MemoryKind


@dataclass
class EnrichmentResult:
    kind: str
    title: str
    body: str
    display_language: str
    localization_mode: str  # none|heuristic|model
    confidence: float
    rationale: str
    provider_id: str
    enrichment_mode: str  # passthrough|heuristic|model


class SemanticEnricher(Protocol):
    def enrich(
        self,
        *,
        title: str,
        body: str,
        kind_hint: str = "",
        metadata: dict | None = None,
    ) -> EnrichmentResult: ...


def _resolve_kind(title: str, body: str, kind_hint: str) -> str:
    hint = (kind_hint or "").strip().lower()
    if hint in _VALID_KINDS:
        return hint
    return classify_kind(f"{title} {body}")


def _heuristic_confidence(content: str, kind: MemoryKind) -> float:
    text = content.strip()
    if not text:
        return 0.1
    score = 0.50
    if len(text) >= 12:
        score += 0.10
    if kind in (MemoryKind.PREFERENCE, MemoryKind.PROCEDURE, MemoryKind.PROJECT, MemoryKind.CORRECTION):
        score += 0.12
    if any(k in text.lower() for k in ["可能", "大概", "maybe", "probably", "不确定"]):
        score -= 0.20
    tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fff]", text.lower()))
    if len(tokens) < 4:
        score -= 0.18
    return max(0.1, min(0.95, score))


_KIND_LABELS = {
    MemoryKind.FACT: "事实",
    MemoryKind.PREFERENCE: "偏好",
    MemoryKind.PROJECT: "项目",
    MemoryKind.EPISODE: "事件",
    MemoryKind.PROCEDURE: "流程",
    MemoryKind.CORRECTION: "纠错",
}


def _compact_english_snippet(text: str, limit: int) -> str:
    replacements = {
        "memory": "记忆", "project": "项目", "preference": "偏好",
        "rule": "规则", "workflow": "流程", "procedure": "流程",
        "constraint": "约束", "fact": "事实", "use": "使用",
        "should": "应", "must": "必须", "avoid": "避免", "file": "文件",
        "files": "文件", "folder": "文件夹", "source": "来源", "truth": "事实依据",
        "agent": "智能体", "global": "全局", "local": "本地", "compact": "简洁",
    }
    words = " ".join(str(text or "").replace("\n", " ").split())[:limit].split()
    return " ".join(
        replacements.get(word.strip(".,:;()[]{}\"'").lower(), word)
        for word in words[:36]
    ).strip()


def _localize_memory_text(
    title: str, body: str, kind: MemoryKind,
) -> tuple[str, str, str, str, str, str]:
    if not looks_english_text(title + " " + body):
        return title, body, title, body, "zh", "none"
    label = _KIND_LABELS.get(kind, "记忆")
    return (
        f"{label}：{_compact_english_snippet(title or body, 72)}",
        _compact_english_snippet(body or title, 420),
        title,
        body,
        "mixed",
        "heuristic",
    )


class HeuristicEnricher:
    """默认：classify + localize_memory_text；不调外部模型。"""

    provider_id = "community"

    def enrich(
        self,
        *,
        title: str,
        body: str,
        kind_hint: str = "",
        metadata: dict | None = None,
    ) -> EnrichmentResult:
        del metadata
        kind_str = _resolve_kind(title, body, kind_hint)
        kind = MemoryKind(kind_str)
        zh_title, zh_body, _, _, disp_lang, loc_mode = _localize_memory_text(title, body, kind)
        content = body or title
        return EnrichmentResult(
            kind=kind_str,
            title=zh_title,
            body=zh_body,
            display_language=disp_lang,
            localization_mode=loc_mode,
            confidence=_heuristic_confidence(content, kind),
            rationale="heuristic classify + localize",
            provider_id=self.provider_id,
            enrichment_mode="heuristic",
        )


class PassthroughEnricher:
    """enrichment 关闭时：几乎原样返回，kind 用 classify 或 hint。"""

    provider_id = "passthrough"

    def enrich(
        self,
        *,
        title: str,
        body: str,
        kind_hint: str = "",
        metadata: dict | None = None,
    ) -> EnrichmentResult:
        del metadata
        kind_str = _resolve_kind(title, body, kind_hint)
        combined = f"{title} {body}".strip()
        disp_lang = "mixed" if looks_english_text(combined) else "zh"
        return EnrichmentResult(
            kind=kind_str,
            title=title,
            body=body,
            display_language=disp_lang,
            localization_mode="none",
            confidence=0.5,
            rationale="enrichment disabled",
            provider_id=self.provider_id,
            enrichment_mode="passthrough",
        )


class _ModelFallbackEnricher(HeuristicEnricher):
    """model 模式占位：回退启发式并在 rationale 标明 model_unavailable。"""

    provider_id = "model_fallback"

    def enrich(
        self,
        *,
        title: str,
        body: str,
        kind_hint: str = "",
        metadata: dict | None = None,
    ) -> EnrichmentResult:
        result = super().enrich(
            title=title, body=body, kind_hint=kind_hint, metadata=metadata,
        )
        return EnrichmentResult(
            kind=result.kind,
            title=result.title,
            body=result.body,
            display_language=result.display_language,
            localization_mode=result.localization_mode,
            confidence=result.confidence,
            rationale=f"{result.rationale}; model_unavailable",
            provider_id=self.provider_id,
            enrichment_mode="heuristic",
        )


class ModelBackend(Protocol):
    """真模型后端接口(duck typing)。

    外部实现(OpenAI / 本地模型)只需实现 classify + translate,
    通过 set_model_backend 注入即可让 get_enricher("model") 使用真模型。
    """

    def classify(self, title: str, body: str,
                 kind_hint: str = "") -> tuple[str, float]:
        """分类,返回 (kind, confidence)。kind 必须是 _VALID_KINDS 之一。"""
        ...

    def translate(self, text: str, target_lang: str = "zh") -> str:
        """翻译文本到目标语言。"""
        ...


# 模块级模型后端单例(通过 set_model_backend 注入)
_model_backend: ModelBackend | None = None


def set_model_backend(backend: ModelBackend | None) -> None:
    """注入真模型后端。传 None 清除,回退 heuristic。"""
    global _model_backend
    _model_backend = backend


class ModelEnricher:
    """注入真模型后端的 enricher。无 backend 时回退 heuristic。

    有 backend 时:用模型分类 + 翻译,heuristic 作 fallback。
    无 backend 时:等价于 HeuristicEnricher。
    """

    provider_id = "model"

    def __init__(self, backend: ModelBackend | None = None):
        self._backend = backend if backend is not None else _model_backend
        self._fallback = HeuristicEnricher()

    def enrich(
        self,
        *,
        title: str,
        body: str,
        kind_hint: str = "",
        metadata: dict | None = None,
    ) -> EnrichmentResult:
        del metadata
        # 无 backend:回退 heuristic
        if self._backend is None:
            result = self._fallback.enrich(
                title=title, body=body, kind_hint=kind_hint,
            )
            return EnrichmentResult(
                kind=result.kind, title=result.title, body=result.body,
                display_language=result.display_language,
                localization_mode=result.localization_mode,
                confidence=result.confidence,
                rationale="model backend not configured; heuristic fallback",
                provider_id="model_fallback",
                enrichment_mode="heuristic",
            )

        # 有 backend:用模型分类
        try:
            kind_str, confidence = self._backend.classify(title, body, kind_hint)
            if kind_str not in _VALID_KINDS:
                kind_str = _resolve_kind(title, body, kind_hint)
                confidence = min(confidence, 0.5)
        except Exception:
            kind_str = _resolve_kind(title, body, kind_hint)
            confidence = 0.5

        kind = MemoryKind(kind_str)
        combined = f"{title} {body}".strip()

        # 有 backend 且内容是英文:用模型翻译
        if looks_english_text(combined):
            try:
                zh_title_raw = self._backend.translate(title, "zh")
                zh_body_raw = self._backend.translate(body, "zh")
                # P1.2: 校验 translate 返回类型必须是 str
                zh_title = zh_title_raw if isinstance(zh_title_raw, str) and zh_title_raw else title
                zh_body = zh_body_raw if isinstance(zh_body_raw, str) and zh_body_raw else body
                loc_mode = "model"
            except Exception:
                zh_title, zh_body, _, _, disp_lang, loc_mode = _localize_memory_text(
                    title, body, kind)
                return EnrichmentResult(
                    kind=kind_str, title=zh_title, body=zh_body,
                    display_language=disp_lang, localization_mode=loc_mode,
                    confidence=confidence,
                    rationale="model translate failed; heuristic fallback",
                    provider_id=self.provider_id,
                    enrichment_mode="heuristic",
                )
        else:
            zh_title, zh_body, _, _, disp_lang, loc_mode = _localize_memory_text(
                title, body, kind)

        return EnrichmentResult(
            kind=kind_str, title=zh_title, body=zh_body,
            display_language="zh", localization_mode=loc_mode,
            confidence=confidence,
            rationale="model classify + translate",
            provider_id=self.provider_id,
            enrichment_mode="model",
        )


class ProviderModelBackend:
    """通过 provider_api 自动注入的真模型 ModelBackend。

    用 provider API 做 classify + translate,失败回退 heuristic。
    需要先配置 provider(环境变量或 config.local.json)。
    """

    provider_id = "provider_api"

    def __init__(self):
        from .provider_api import get_provider
        self._provider = get_provider()
        self._fallback = HeuristicEnricher()

    def classify(self, title: str, body: str,
                 kind_hint: str = "") -> tuple[str, float]:
        if self._provider is None:
            # 无 provider:回退 heuristic
            return self._fallback_classify(title, body, kind_hint)
        try:
            system = (
                "你是记忆分类器。根据用户输入,返回记忆类型和置信度。\n"
                "类型只能是:preference|fact|project|procedure|episode|correction\n"
                "输出格式:严格 JSON {\"kind\":\"...\",\"confidence\":0.0-1.0}"
            )
            user = f"title: {title}\nbody: {body}"
            if kind_hint:
                user += f"\nhint: {kind_hint}"
            resp = self._provider.chat(system, user, max_tokens=100)
            data = json.loads(resp)
            kind = str(data.get("kind", "fact"))
            conf = float(data.get("confidence", 0.5))
            if kind not in _VALID_KINDS:
                return self._fallback_classify(title, body, kind_hint)
            return (kind, conf)
        except Exception:
            return self._fallback_classify(title, body, kind_hint)

    def translate(self, text: str, target_lang: str = "zh") -> str:
        if self._provider is None:
            return text
        try:
            system = f"翻译到{target_lang}语言,只返回译文,不加解释。"
            return self._provider.chat(system, text, max_tokens=500)
        except Exception:
            return text

    def _fallback_classify(self, title, body, kind_hint):
        """heuristic 回退。"""
        kind_str = _resolve_kind(title, body, kind_hint)
        return (kind_str, 0.5)


def get_enricher(mode: str | None = None,
                 workspace: str | Path | None = None) -> SemanticEnricher:
    """mode: off|heuristic|model|host；默认 heuristic（可用 MEMORYGUARD_ENRICHER 覆盖）。

    Skill / TRAE 场景：用 host 队列（list_pending + apply），不要靠
    MEMORYGUARD_ENRICHER=model 在 build_projection 里同步阻塞调 LLM。

    - host: 仅表示「启用宿主入队」意图，仍返回 HeuristicEnricher（投影保持确定性）
    - model 模式优先级:
      1. set_model_backend 注入的 backend
      2. provider_api 自动构建(从环境变量/config.local.json)
      3. 回退 _ModelFallbackEnricher(标记 model_unavailable)
    """
    import os as _os
    resolved = (mode or _os.environ.get("MEMORYGUARD_ENRICHER", "heuristic")).strip().lower()
    if resolved in {"off", "none", "passthrough"}:
        return PassthroughEnricher()
    if resolved in {"host", "host_queue"}:
        # 宿主 AI 异步整理：构建路径仍用启发式；真正 LLM 走 MCP 队列
        return HeuristicEnricher()
    if resolved == "model":
        # 1. 显式注入的 backend
        if _model_backend is not None:
            return ModelEnricher()
        # 2. provider_api 自动构建
        from .provider_api import get_provider
        provider = get_provider(workspace)
        if provider is not None:
            return ModelEnricher(ProviderModelBackend())
        # 3. 回退
        return _ModelFallbackEnricher()
    return HeuristicEnricher()
