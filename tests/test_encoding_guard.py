# -*- coding: utf-8 -*-
"""Part A: byte-level decode, mojibake detection, conservative repair.

Core contract: legitimately clean text is never transformed; only pervasively
corrupt content (UTF-8 bytes decoded as GBK) round-trips to clean text.
"""
from __future__ import annotations

import pytest

from memoryguard.encoding_guard import (
    decode_hook_bytes,
    guard_persist_content,
    looks_like_mojibake,
    repair_utf8_as_gbk,
    should_repair,
    strip_pua_residue,
)


# --- strict UTF-8 / BOM pass-through --------------------------------------

def test_decode_hook_bytes_passes_clean_utf8_unchanged():
    raw = '{"event": "user_prompt"}'.encode("utf-8")
    assert decode_hook_bytes(raw) == '{"event": "user_prompt"}'


def test_decode_hook_bytes_strips_bom():
    raw = '﻿{"ok": true}'.encode("utf-8")
    assert decode_hook_bytes(raw) == '{"ok": true}'


def test_decode_hook_bytes_recovers_gbk_locale_stdin():
    # The 2026-08 live corruption: a clean UTF-8 JSON payload shipped over a
    # GBK pipe.  A GBK-locale hook that re-emits the (corrupted) text encodes
    # it with its locale codec; the bytes it writes are the ORIGINAL UTF-8
    # bytes (gb18030 is bijective over these codepoints), so strict UTF-8
    # decode recovers the intended text byte-for-byte.
    original = 'cursor写入怎么'
    mojibake = original.encode("utf-8").decode("gb18030")  # corrupted text in Python
    assert looks_like_mojibake(mojibake)
    raw_from_gbk_pipe = mojibake.encode("gb18030")
    assert decode_hook_bytes(raw_from_gbk_pipe) == original


def test_decode_hook_bytes_gb18030_fallback_recovers_locale_text():
    # Bytes that are NOT valid UTF-8 (a locale-encoded ASCII-adjacent byte) hit
    # the gb18030 fallback and come back as the intended clean text.
    assert decode_hook_bytes("ä".encode("gb18030")) == "ä"


def test_decode_hook_bytes_returns_corrupt_text_unchanged_when_valid_utf8():
    # Already-corrupted VALID Unicode re-encoded as UTF-8 is byte-valid; the
    # byte decoder passes it through (the persist boundary's guard_persist_content
    # is what rejects/repairs that class).
    mojibake = "cursor鍐欏叆鎬庝箞"
    assert decode_hook_bytes(mojibake.encode("utf-8")) == mojibake


# --- detection -------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "涓嶈兘",                          # 不能 -> bigram garbage
    "cursor鍐欏叆鎬庝箞",             # 写入怎么 -> garbage + bigram
    "normal prefix 娑撹敚閻欏彛 鐞﹁箓",  # observed live garbage tokens
    "luna medium涓蔣鐙口琦蹂ゆ标",      # observed live PUA-laden turn
])
def test_looks_like_mojibake_true_on_garbage(text: str):
    assert looks_like_mojibake(text)


def test_looks_like_mojibake_false_on_clean_text():
    assert not looks_like_mojibake("干净的项目 turn，完全正常")
    assert not looks_like_mojibake("plain ascii and numbers 123")
    # A single rare Hanzi must not count (e.g. a legit quote of a sample).
    assert not looks_like_mojibake("涓涓细流")


def test_looks_like_mojibake_true_on_pua_and_fffd():
    assert looks_like_mojibake("abcdef")          # PUA U+E102
    assert looks_like_mojibake("text � more")      # lossy marker


# --- repair ----------------------------------------------------------------

def test_repair_utf8_as_gbk_roundtrip():
    # "cursor写入怎么" mis-decoded as GBK recovers losslessly.
    original = "cursor写入怎么"
    mojibake = original.encode("utf-8").decode("gb18030")
    assert repair_utf8_as_gbk(mojibake) == original


def test_repair_is_conservative_on_clean_text():
    # Legitimate text is never transformed.
    for text in ["我们", "涓涓细流", "hello world 你好", "a é b"]:
        assert repair_utf8_as_gbk(text) == text


def test_repair_strips_unrecoverable_pua():
    cleaned, lost = strip_pua_residue("abcdef�ghi")
    assert cleaned == "abcdefghi"
    assert lost == 2


def test_guard_persist_content_raises_on_irrecoverable_garbage():
    # 锟斤拷 is the mojibake of U+FFFD lossy markers; its round-trip decodes to
    # U+FFFD which fails the zero-marker gate, so repair cannot recover it and
    # the persist gate must fail closed.
    with pytest.raises(ValueError, match="history_content_mojibake"):
        guard_persist_content("锟斤拷" * 20)


def test_guard_persist_content_repairs_recoverable_cjk_garbage():
    # "涓嶈兘" (the mojibake of 不能) IS recoverable -- its gb18030 bytes are
    # the original UTF-8 bytes -- so the gate repairs it instead of raising.
    assert guard_persist_content("涓嶈兘" * 3) == "不能" * 3


def test_guard_persist_content_repairs_recoverable_content():
    original = "cursor写入怎么"
    mojibake = original.encode("utf-8").decode("gb18030")
    assert guard_persist_content(mojibake) == original


def test_guard_persist_content_passes_clean_quoting_turns():
    # A clean turn that merely QUOTES a mojibake example string is left alone
    # (should_repair gates on pervasiveness, not a single bigram).
    quoted = '用户提到"涓嶈兘"这样的例子，实际是正常回复'
    assert guard_persist_content(quoted) == quoted


# --- should_repair gate -----------------------------------------------------

def test_should_repair_distinguishes_clean_quoting_from_pervasive():
    quoted = '这里有一个例子"涓嶈兘"其余都是干净内容'
    assert not should_repair(quoted)
    pervasive = "涓嶈兘" * 8
    assert should_repair(pervasive)
