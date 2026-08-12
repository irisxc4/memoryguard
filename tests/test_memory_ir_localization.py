"""Native V2 text localization contract tests."""

from __future__ import annotations

from memoryguard.runtime_v2.text_native import localize_native_text


def test_native_localizer_localizes_existing_english_atom_fields() -> None:
    result = localize_native_text(
        "Prefer compact project memory rules",
        "The agent should use project memory files as the source of truth and avoid unrelated global preferences.",
        "preference",
    )

    assert result["title"].startswith("偏好：")
    assert "中文整理：" not in result["body"]
    assert result["localization_mode"] == "heuristic"
    assert result["display_language"] == "mixed"
    assert result["original_title"] == "Prefer compact project memory rules"
    assert result["original_body"].startswith("The agent should use")


def test_native_localizer_marks_heuristic_not_translated() -> None:
    result = localize_native_text(
        "Prefer concise commit messages",
        "Avoid AI-sounding summaries in commit messages.",
        "preference",
    )

    assert result["localization_mode"] == "heuristic"
    assert result["display_language"] == "mixed"
    assert "中文整理：" not in result["body"]
    assert result["original_title"] == "Prefer concise commit messages"
    assert result["original_body"].startswith("Avoid AI-sounding")


def test_native_localizer_migrates_legacy_fake_chinese_prefix() -> None:
    result = localize_native_text(
        "偏好：Prefer compact project memory rules",
        "中文整理：The agent should use project memory files as the source of truth.",
        "preference",
    )

    assert "中文整理：" not in result["body"]
    assert "中文辅助摘要：" not in result["body"]
    assert result["localization_mode"] == "heuristic"
    assert result["original_body"].startswith("The agent should use")


def test_native_localizer_keeps_chinese_atom_body_by_default() -> None:
    result = localize_native_text("中文标题", "这是一条中文记忆正文", "fact")

    assert result["localization_mode"] == "none"
    assert result["title"] == "中文标题"
    assert result["body"] == "这是一条中文记忆正文"
