"""Provider API 注入测试:验证真模型全链路。

用 mock provider backend 模拟 API 调用,验证:
1. ProviderConfig 从环境变量加载
2. ProviderConfig 从 config.local.json 加载
3. set_provider 注入后 get_enricher('model') 用真模型
4. ProviderModelBackend.classify 调 provider.chat
5. ProviderModelBackend.translate 调 provider.chat
6. ProviderEmbeddingBackend.embed_text 调 provider.embed
7. API 失败回退 heuristic
8. 无 provider 回退 heuristic
"""
import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class MockProviderBackend:
    """Mock provider backend,记录所有调用。"""

    def __init__(self):
        self.chat_calls: list[tuple[str, str]] = []
        self.embed_calls: list[str] = []
        self.chat_response = '{"kind":"preference","confidence":0.92}'
        self.embed_response = [0.1] * 8

    def chat(self, system: str, user: str, max_tokens: int = 500) -> str:
        self.chat_calls.append((system, user))
        return self.chat_response

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return self.embed_response


def test_config_from_env():
    """ProviderConfig 从环境变量加载。"""
    from memoryguard.provider_api import load_provider_config, clear_provider

    clear_provider()
    old_env = dict(os.environ)
    try:
        os.environ["MEMORYGUARD_PROVIDER_TYPE"] = "openai_compatible"
        os.environ["MEMORYGUARD_PROVIDER_API_BASE"] = "http://localhost:11434/v1"
        os.environ["MEMORYGUARD_PROVIDER_API_KEY"] = "test-key"
        os.environ["MEMORYGUARD_PROVIDER_MODEL"] = "llama3"
        cfg = load_provider_config()
        assert cfg.is_configured()
        assert cfg.provider_type == "openai_compatible"
        assert cfg.api_base == "http://localhost:11434/v1"
        assert cfg.model == "llama3"
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        clear_provider()


def test_config_from_local_json():
    """ProviderConfig 从 config.local.json 加载。"""
    from memoryguard.provider_api import load_provider_config, clear_provider

    clear_provider()
    with tempfile.TemporaryDirectory() as ws:
        mg_dir = Path(ws) / ".memoryguard"
        mg_dir.mkdir()
        local_config = mg_dir / "config.local.json"
        local_config.write_text(json.dumps({
            "provider": {
                "provider_type": "anthropic",
                "api_base": "https://api.anthropic.com/v1",
                "api_key": "sk-ant-test",
                "model": "claude-3-5-sonnet-20241022",
            }
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        cfg = load_provider_config(ws)
        assert cfg.is_configured()
        assert cfg.provider_type == "anthropic"
        assert cfg.model == "claude-3-5-sonnet-20241022"
    clear_provider()


def test_set_provider_injection():
    """set_provider 注入后 get_provider 返回注入的 backend。"""
    from memoryguard.provider_api import set_provider, get_provider, clear_provider

    clear_provider()
    mock = MockProviderBackend()
    set_provider(mock)
    assert get_provider() is mock
    set_provider(None)
    clear_provider()


def test_enricher_uses_provider():
    """get_enricher('model') 自动用 provider_api 构建 ModelEnricher。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_enricher import get_enricher, ModelEnricher

    clear_provider()
    mock = MockProviderBackend()
    set_provider(mock)
    try:
        enricher = get_enricher("model")
        # 应该是 ModelEnricher(不是 _ModelFallbackEnricher)
        assert isinstance(enricher, ModelEnricher)
        result = enricher.enrich(title="test", body="我喜欢用 Python")
        assert result.kind == "preference"
        assert result.confidence == 0.92
        assert result.enrichment_mode == "model"
        # 验证调了 provider.chat
        assert len(mock.chat_calls) > 0
    finally:
        set_provider(None)
        clear_provider()


def test_enricher_fallback_without_provider():
    """无 provider 时 get_enricher('model') 回退 _ModelFallbackEnricher。"""
    from memoryguard.provider_api import clear_provider
    from memoryguard.semantic_enricher import get_enricher, _ModelFallbackEnricher

    clear_provider()
    enricher = get_enricher("model")
    assert isinstance(enricher, _ModelFallbackEnricher)


def test_provider_classify_calls_chat():
    """ProviderModelBackend.classify 调 provider.chat。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_enricher import ProviderModelBackend

    clear_provider()
    mock = MockProviderBackend()
    set_provider(mock)
    try:
        backend = ProviderModelBackend()
        kind, conf = backend.classify("test", "我喜欢 Python")
        assert kind == "preference"
        assert conf == 0.92
        assert len(mock.chat_calls) == 1
        # system prompt 应含"记忆分类器"
        assert "记忆分类器" in mock.chat_calls[0][0]
    finally:
        set_provider(None)
        clear_provider()


def test_provider_translate_calls_chat():
    """ProviderModelBackend.translate 调 provider.chat。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_enricher import ProviderModelBackend

    clear_provider()
    mock = MockProviderBackend()
    mock.chat_response = "用户偏好 Python"
    set_provider(mock)
    try:
        backend = ProviderModelBackend()
        result = backend.translate("I prefer Python", "zh")
        assert result == "用户偏好 Python"
        assert len(mock.chat_calls) == 1
    finally:
        set_provider(None)
        clear_provider()


def test_provider_embed():
    """ProviderEmbeddingBackend.embed_text 调 provider.embed。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_dedup import ProviderEmbeddingBackend

    clear_provider()
    mock = MockProviderBackend()
    set_provider(mock)
    try:
        backend = ProviderEmbeddingBackend()
        vec = backend.embed_text("test text")
        assert vec == [0.1] * 8
        assert len(mock.embed_calls) == 1
        assert mock.embed_calls[0] == "test text"
    finally:
        set_provider(None)
        clear_provider()


def test_api_failure_fallback():
    """API 调用失败时回退 heuristic。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_enricher import ProviderModelBackend

    clear_provider()

    class FailingProvider:
        def chat(self, system, user, max_tokens=500):
            raise ConnectionError("API down")
        def embed(self, text):
            raise ConnectionError("API down")

    set_provider(FailingProvider())
    try:
        backend = ProviderModelBackend()
        kind, conf = backend.classify("test", "用户偏好 Python")
        # 应回退 heuristic,kind 仍有效,confidence=0.5
        assert kind in ("preference", "fact")
        assert conf == 0.5
    finally:
        set_provider(None)
        clear_provider()


def test_semantic_dedup_uses_provider():
    """SemanticDedup 自动检测 provider 并用 ProviderEmbeddingBackend。"""
    from memoryguard.provider_api import set_provider, clear_provider
    from memoryguard.semantic_dedup import SemanticDedup, ProviderEmbeddingBackend

    clear_provider()
    mock = MockProviderBackend()
    set_provider(mock)
    try:
        with tempfile.TemporaryDirectory() as ws:
            dedup = SemanticDedup(ws, "default")
            assert isinstance(dedup.backend, ProviderEmbeddingBackend)
            vec = dedup.embed_text("test")
            assert vec == [0.1] * 8
    finally:
        set_provider(None)
        clear_provider()


def test_openai_compatible_backend_construction():
    """OpenAICompatibleBackend 能正确构造。"""
    from memoryguard.provider_api import ProviderConfig, OpenAICompatibleBackend

    cfg = ProviderConfig(
        provider_type="openai_compatible",
        api_base="http://localhost:11434/v1",
        api_key="test-key",
        model="llama3",
    )
    backend = OpenAICompatibleBackend(cfg)
    assert backend.base == "http://localhost:11434/v1"
    assert backend.headers["Authorization"] == "Bearer test-key"


if __name__ == "__main__":
    test_config_from_env()
    print("OK: config from env")
    test_config_from_local_json()
    print("OK: config from local json")
    test_set_provider_injection()
    print("OK: set_provider injection")
    test_enricher_uses_provider()
    print("OK: enricher uses provider")
    test_enricher_fallback_without_provider()
    print("OK: enricher fallback without provider")
    test_provider_classify_calls_chat()
    print("OK: provider classify calls chat")
    test_provider_translate_calls_chat()
    print("OK: provider translate calls chat")
    test_provider_embed()
    print("OK: provider embed")
    test_api_failure_fallback()
    print("OK: api failure fallback")
    test_semantic_dedup_uses_provider()
    print("OK: semantic dedup uses provider")
    test_openai_compatible_backend_construction()
    print("OK: openai compatible backend construction")
    print("\nAll provider API tests passed.")
