"""Encoding boundary defence: byte-level decode, mojibake detection, safe repair.

Guards two boundaries that previously persisted corrupted text:

1. **stdin** (``decode_hook_bytes``): strict UTF-8 first (the current correct
   path); when the host shipped locale-encoded bytes, recover the intended
   JSON text instead of silently persisting garbage.
2. **persist** (``guard_persist_content``): reject or auto-repair content that
   is already-mojibake'd *valid Unicode* -- the class of corruption the old
   ``.encode("utf-8", errors="strict")`` check could not stop.

The corruption this defends against is UTF-8 bytes decoded as a GBK-family
codec (Windows cp936), which yields ``涓嶈兘``-style Hanzi and, for byte pairs
GBK does not assign, PUA codepoints U+E000-U+F8FF.  Because the bytes are
conserved, such spans can often be recovered losslessly by re-encoding with
``gb18030`` and decoding the result as UTF-8 -- and crucially the PUA
codepoints *participate* in that round-trip (gb18030 maps them back to the
original byte pairs).  Only genuinely unalignable runs (multi-stage corruption)
are unrecoverable; those keep their non-PUA bytes and have PUA stripped.

Zero runtime dependencies (stdlib only).  The repair is deliberately
conservative: a span is rewritten **only** when a strict round-trip
``gb18030 encode -> utf-8 decode`` produces text with **zero** remaining
mojibake markers.  Legitimate text is never transformed.
"""
from __future__ import annotations

import re
from typing import Any

# UTF-8 bytes decoded as GBK yield these characteristic Hanzi.  Curated from
# observed corruption + known mojibake tables; the set is filtered to keep
# only non-ASCII characters (the copy-pasted samples carry ASCII fragments
# like "laude" / "n1" that must never count as mojibake markers).
_GBK_MOJIBAKE_GARBAGE_RAW = (
    "涓嶈兘鐨鐙鐗娑撹鎴鏄鎸鎵鎺鍐鍒鍦欏叆鎬庝箞"
    "浣犲岀琚鑾浠鍛閰寮鏉鏂攰骞杩姹鐢纭畠璞摰囧捫浼穑"
    "骀缂缁缃鏌鎶鎻鏇鎭鎯鎼鍗鍙鍥鍚锛鏈绾灏璇闂杞閭垯镊"
    "濂绂璁鐪镐鏍鐩鐧鐭鍏朵粬锛锟斤拷ユマュォチュォ"
    # observed live AppData history (2026-08): dense single-layer mojibake.
    "珛鍒ゆ柇姟屼綘濮閼犲炴楠縗晳宀樼党妗懖暘崑濡楃憰▏炲妞鎱橽婧"
    "癱佸勪槸畻竵嬶簳忎灦棰戠兘欏敚閻彛鐞箓銈嗘爣娴犺ｉ"
    "绋戝嚭鏁寸収鍋氭柟妗堜笉瑕佸皢鐢ㄥ瓙浠ｇ悊骞叉椿"
    "鐜板湪鍏嶈垂涓汉璁稿彲灏辨槸鍙晢鐢ㄤ簡锛屼笉搴旇鏈夋按鍗扮殑"
    "娑撹敚閻欏彛鐞﹁箓銈嗘爣娴犺濮ｉ晳宀樼党娑撳礇妗撮懖鍝勭暘閼犲"
    "鍙庨崑姘ｈ〔濡楃憰鍋冨▏閼綥鐟炴禒鎱橽楠炲弶妞縗"
    "鍐欏叆鎬庝箞浼氫贡鐮侊紵杩樼畻鍒癱鍐欏叆鐨勶紝鐜板湪瑙勫垯"
    "鎬庝箞閮芥壘涓嶅埌浜嗭紝浣犲府鎴戞煡涓€涓嬶紝涓嶈琛ヤ竵寮忎慨澶嶏紝"
    "鎶婂簳灞傛灦鏋勪慨濂芥墠瀵广€傚厛鏌ユ竻妤氬師鍥狅紝涓嶈鍔ㄦ墜"
    "鎶婅閲忓尯鎹釜鏄剧ず鏂瑰紡锛屽悕瀛楄閲忔敾鍑诲姏闃插尽鍔涙樉绀轰竴"
    # second-layer mojibake (re-garbaged output of a double-corrupted span);
    # observed on live history (2026-08): 蒋 琦 蹂 镑 岘 绛 崛 桴 鑲 衲 妗 瑕 偃 鑾 慙 姣
    "蒋琦蹂镑岘绛崛桴鑲衲妗瑕偃鑾慙姣"
)
_GBK_MOJIBAKE_GARBAGE = frozenset(
    ch for ch in _GBK_MOJIBAKE_GARBAGE_RAW if ord(ch) >= 0x80
)

# High-precision 2-char garbage tokens (known UTF-8-as-GBK outputs).
_GBK_MOJIBAKE_BIGRAMS = (
    "涓嶈兘", "涓嶈", "涓殑", "涓細", "涓嬮潰", "涓婇潰", "涓や釜",
    "鎴戜滑", "鎴戜笉", "鎴戜滑鐨", "娑撹", "娑堟伅", "娑堟伅",
    "鐨勶", "鐨勫", "鐨勪", "鐨勭", "鐨勬", "鐨勫紑",
    "閰嶇疆", "鏂囦欢", "鏂囩尞", "鏂规硶", "鏂扮殑", "鏂板", "鏂扮増",
    "鏁版嵁", "鏇存柊", "鏈嶅姟", "鏈夋晥", "鏈変汉", "鏈夊叧", "鏈夋椂",
    "鏃堕棿", "鏃堕暱", "鏃ュ織", "鐩稿叧", "鐩存帴", "鍒涘缓", "鍒嗘瀽",
    "鍙互", "鍙兘", "鍦ㄤ簬", "鍦ㄨ繖", "閫夐」", "杩斿洖", "杩欎釜",
    "杩欐牱", "杩欓噷", "杩欐", "鐢ㄦ埛", "鐢ㄤ簬", "鐢ㄥ埌", "鏄剧ず",
    "鏄笉", "闂寸殑", "骞冲彴", "骞朵笖", "骞朵笉", "镐庝箞", "浣犲ソ",
    "浣犱滑", "浣犵殑", "绠＄悊", "绠＄粺", "绠＄▼", "缂栫爜", "缂栧啓",
    "缂撳瓨", "缃戠粶", "缃戦〉", "鑾峰彇", "鑾风殑", "鎻愪緵", "鎻愮ず",
    "鎶撳彇", "鎶婃帶", "鎺ㄨ崘", "鎺ュ彛", "鎺ュ叆", "鏌ユ壘", "鏌ョ湅",
    "鏌ヨ", "鏍规嵁", "鏍囧噯", "鏍稿績", "鏂囧瓧", "鏂囨", "鍙戝竷",
    "鍙戦", "璇存槑", "璇存敞", "鎵ц", "鎵惧埌", "缂撳紦", "鎸囧畾",
    "鎸囧嚭", "鎸佺画", "鎸佹湁", "鎷夊嚭", "鎷夎繃", "鎻愬埌", "鎻愬墠",
    # observed live bigrams (2026-08).
    "娑撹敚", "閻欏彛", "鐞﹁箓", "銈嗘爣", "娴犺", "濮ｉ晳", "宀樼党",
    "娑撳礇", "妗撮懖", "鍝勭暘", "閼犲", "鍙庨崑", "姘ｈ", "濡楃憰",
    "鍋冨▏", "閼綥", "鐟炴禒", "鎱橽", "楠炲弶", "妞縗", "鍏嶈垂",
    "涓汉璁", "稿彲灏", "辨槸鍙", "晢鐢", "搴旇", "鏈夋按", "鍗扮殑",
    "浼氫贡", "鐮侊紵", "杩樼畻", "鍒癱", "鏌ユ竻", "妤氬師", "鍥犲紶",
    "鍔ㄦ墜", "鍐欏叆", "鎬庝箞", "涓嶇粰", "鏁寸収", "鍋氭柟", "妗堜笉",
    "瑕佷娇", "鐢ㄥ瓙", "浠ｇ悊", "骞叉椿", "鐜板湪", "璁稿彲", "灏辨槸",
    "鐢ㄤ簡", "搴旇", "鏈夋按", "鍗扮殑", "鍒ゆ柇", "浠诲姟", "鍑哄畬",
    "鎶婅", "閲忓尯", "鎹釜", "鏄剧ず", "鏂瑰紡", "鍚嶅瓧", "閲忔敾",
    "鍑诲姏", "闃插尽", "鍔涙樉", "绀轰竴", "鏂囨", "鍙戝竷", "鏌ユ竻",
)

_PUA_RE = re.compile(r"[-]")
_FFFD = "�"

# Longest-first alternation so nested bigrams (涓嶈兘 contains 涓嶈) are counted
# as ONE match, not two -- a single quoted mojibake phrase must not trip the
# ``>= 2`` pervasiveness gate in ``should_repair``.
_BIGRAM_RE = re.compile(
    "|".join(re.escape(b) for b in sorted(_GBK_MOJIBAKE_BIGRAMS, key=len, reverse=True))
)


def _is_cjk_garbage(ch: str) -> bool:
    return ch in _GBK_MOJIBAKE_GARBAGE


def _mojibake_score(text: str) -> int:
    """Relative corruption weight, used only as a strict-ordering gate."""
    score = 0
    score += 10 * sum(1 for ch in text if _PUA_RE.match(ch))
    score += 5 * text.count(_FFFD)
    score += 2 * sum(1 for ch in text if _is_cjk_garbage(ch))
    score += 3 * sum(1 for bigram in _GBK_MOJIBAKE_BIGRAMS if bigram in text)
    return score


def looks_like_mojibake(text: str) -> bool:
    """Conservative, high-precision corruption detector.

    Only strong signals count: PUA codepoints, lossy ``\\ufffd`` markers,
    known garbage bigrams, and three consecutive garbage Hanzi.  A single
    rare Hanzi (e.g. ``涓涓细流``) is deliberately not enough.
    """
    value = str(text or "")
    if not value:
        return False
    if _PUA_RE.search(value):
        return True
    if _FFFD in value:
        return True
    if any(bigram in value for bigram in _GBK_MOJIBAKE_BIGRAMS):
        return True
    for index in range(len(value) - 2):
        if all(_is_cjk_garbage(ch) for ch in value[index : index + 3]):
            return True
    return False


def should_repair(content: str) -> bool:
    """Turn-level gate: only *pervasively* corrupt content gets repaired.

    Distinguishes genuinely corrupted turns (dense garbage / PUA codepoints)
    from clean turns that merely QUOTE mojibake example strings (one bigram
    inside a large clean body).  PUA / U+FFFD always qualify.
    """
    value = str(content or "")
    if not value:
        return False
    if _PUA_RE.search(value) or _FFFD in value:
        return True
    garbage = sum(1 for ch in value if _is_cjk_garbage(ch))
    if not garbage:
        return False
    ratio = garbage / len(value)
    # Non-overlapping count: nested bigrams (涓嶈兘 ⊃ 涓嶈) collapse into the
    # longest match so a single quoted mojibake phrase still counts as one.
    bigrams = len(_BIGRAM_RE.findall(value))
    # Both the bigram and the dense-char rules are density-gated: a clean turn
    # that merely QUOTES mojibake example strings (a few garbage tokens in a
    # large body, e.g. 12 garbage chars in a 1776-char turn) must not be
    # treated as pervasively corrupt, while genuinely corrupted short turns
    # (garbage ratio ~1.0) still qualify.
    if bigrams >= 2 and ratio >= 0.08:
        return True
    return garbage >= 8 and ratio >= 0.08


def strip_pua_residue(text: str) -> tuple[str, int]:
    """Drop PUA / U+FFFD codepoints (carry no readable information).

    Returns ``(cleaned, lost)`` where ``lost`` is the count of removed
    characters so callers can surface the documented lossy part.
    """
    cleaned = []
    lost = 0
    for ch in str(text or ""):
        if _PUA_RE.match(ch) or ch == _FFFD:
            lost += 1
        else:
            cleaned.append(ch)
    return "".join(cleaned), lost


# Max sub-span length probed by the fallback scan.  Aligned recoverable
# segments in mixed-corruption turns are short (a handful of CJK chars), so a
# bounded probe keeps the scan near-linear while still catching them.
_MAX_SUBSPAN = 64


def repair_utf8_as_gbk(text: str) -> str:
    """Lossy-safe inverse of ``UTF-8 bytes decoded as GBK``.

    Recovery works over maximal gb18030-encodable spans (which covers every
    character a GBK-family mis-decode could have produced, including the PUA
    codepoints and ASCII that rode along):

    1. whole-span round-trip ``gb18030 encode -> utf-8 decode``; the span is
       rewritten iff the result carries **zero** mojibake markers.  This
       handles pure-mojibake turns losslessly.
    2. otherwise, a bounded longest-prefix scan recovers whatever aligned
       sub-segments remain (mixed-corruption turns), advancing past
       unrecoverable characters.

    ASCII and non-corrupted text is never transformed -- round-trips of it
    either fail the gates or are byte-identical.  PUA / U+FFFD that survive
    unrecovered are stripped and counted via ``strip_pua_residue``'s contract.

    Only *pervasively* corrupt content (``should_repair``) is repaired: clean
    turns that merely QUOTE mojibake example strings are passed through
    byte-identical (plus any PUA residue stripped).  Callers that already gate
    via ``guard_persist_content`` are unaffected; this makes the raw function
    safe for direct use (e.g. the one-shot repair script).
    """
    value = str(text or "")
    if not value:
        return value

    # Clean / quoting / PUA-only content: nothing to round-trip.  `should_repair`
    # excludes clean turns that merely QUOTE mojibake example strings; the
    # garbage check catches PUA-only turns (e.g. a single stray PUA residue in
    # an otherwise clean body), which have no CJK garbage to recover and must
    # not walk the sub-scan.
    if not should_repair(value) or not any(_is_cjk_garbage(ch) for ch in value):
        stripped, _lost = strip_pua_residue(value)
        return stripped

    out: list[str] = []
    run: list[str] = []

    def flush_run() -> None:
        if not run:
            return
        joined = "".join(run)
        replaced, _lost = _recover_span(joined)
        if replaced is not None:
            out.append(replaced)
        else:
            kept, _klost = strip_pua_residue(joined)
            out.append(kept)
        run.clear()

    for ch in value:
        if ch == _FFFD:
            # U+FFFD is a lossy marker, not bytes; ends the current run.
            flush_run()
        else:
            run.append(ch)
    flush_run()

    return "".join(out)


def _recover_span(span: str) -> tuple[str | None, int]:
    """Recover one span; returns ``(replacement, pua_lost)`` or ``(None, lost)``.

    If any part of the span round-trips to clean text, returns the rewritten
    string (unrecovered residue kept as-is).  Otherwise ``None`` is returned
    and the caller strips PUA itself.
    """
    if not span:
        return None, 0

    # Fast path: the whole span is one clean mojibake block.
    whole = _try_roundtrip(span)
    if whole is not None:
        return whole, 0

    # Only spend effort on spans that actually smell corrupted; pure ASCII or
    # clean text never reaches the scan.
    if not looks_like_mojibake(span):
        return None, 0

    out: list[str] = []
    lost = 0
    index = 0
    n = len(span)
    while index < n:
        if not looks_like_mojibake(span[index:]):
            # Remainder is clean: keep byte-identical.
            out.append(span[index:])
            break
        found = False
        # Longest-prefix probe (bounded), then a 1-char step for unaligned
        # boundaries.  The score gate inside _try_roundtrip prevents garbage
        # from being rewritten, and misaligned prefixes usually fail decode.
        end = min(n, index + _MAX_SUBSPAN)
        for j in range(end, index, -1):
            candidate = _try_roundtrip(span[index:j])
            if candidate is not None:
                out.append(candidate)
                index = j
                found = True
                break
        if found:
            continue
        ch = span[index]
        if _PUA_RE.match(ch) or ch == _FFFD:
            lost += 1
        else:
            out.append(ch)
        index += 1
    return ("".join(out), lost) if out else (None, lost)


def _is_dense_garbage(run: str) -> bool:
    """Input-side gate: only dense-garbage spans may round-trip.

    A single clean character whose GBK bytes happen to form valid UTF-8
    (e.g. ``失`` -> U+02A7) would otherwise be spuriously "repaired" by the
    bounded prefix scan, and a sparse/misaligned prefix round-trips to a
    *second* mojibake layer.  Genuine UTF-8-as-GBK runs are dense in garbage
    Hanzi over their non-ASCII characters; anything well below half of the
    non-ASCII characters is not a real mojibake block and must not move.
    """
    non_ascii = [ch for ch in run if ord(ch) >= 0x80]
    if not non_ascii:
        return False
    hits = sum(
        1
        for ch in non_ascii
        if _is_cjk_garbage(ch) or _PUA_RE.match(ch) or ch == _FFFD
    )
    return hits / len(non_ascii) >= 0.5


def _try_roundtrip(run: str) -> str | None:
    """Attempt ``gb18030 encode -> utf-8 decode``; return cleaned text or None.

    Every gate must pass:

    1. **dense input** -- sparse or clean prefixes never round-trip (a lone
       clean char like ``失`` whose GBK bytes happen to be valid UTF-8 would
       otherwise be spuriously "repaired" to ``ʧ``);
    2. **strict round-trip** -- ``gb18030 encode -> utf-8 decode`` succeeds;
    3. **involution** -- re-encoding the decoded text as UTF-8 and decoding as
       GB18030 reproduces the span byte-for-byte.  A genuine UTF-8-as-GBK
       mis-decode is always an involution; double-corrupted spans fail it
       (they decode to a *second* garbage layer or to invalid UTF-8) and are
       never rewritten;
    4. **zero markers** -- the result carries no PUA, U+FFFD, garbage Hanzi or
       garbage bigram (belt-and-suspenders on top of the involution).

    Anything that fails a gate returns ``None`` (leave unchanged).
    """
    if not run:
        return None
    if not _is_dense_garbage(run):
        return None
    try:
        raw_bytes = run.encode("gb18030", errors="strict")
    except UnicodeEncodeError:
        return None
    try:
        cleaned = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    try:
        back = cleaned.encode("utf-8", errors="strict").decode(
            "gb18030", errors="strict"
        )
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if back != run:
        return None
    if _mojibake_score(cleaned) != 0:
        return None
    return cleaned


def guard_persist_content(content: str) -> str:
    """Persist-boundary choke point: never silently persist garbage.

    Pervasively corrupt content is auto-repaired; ``history_content_mojibake``
    is raised when irrecoverable corruption remains (PUA residue is stripped
    first, so this fires only for non-PUA garbage that cannot round-trip).
    Clean content that merely quotes mojibake example strings is passed
    through untouched.
    """
    value = str(content or "")
    if not should_repair(value):
        return value
    repaired = repair_utf8_as_gbk(value)
    if looks_like_mojibake(repaired):
        raise ValueError("history_content_mojibake")
    return repaired


def decode_hook_bytes(raw: bytes, *, source: str = "hook_stdin") -> str:
    """Byte-level hook stdin decoder.

    Prefers strict UTF-8 (current correct behaviour).  When the host shipped
    locale-encoded bytes (Windows GBK pipe), recover the intended text
    instead of failing the whole hook.  Never returns a value the caller
    would persist without re-checking through ``guard_persist_content``.
    """
    if not raw or not raw.strip():
        return ""
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        pass
    # Best-effort: strict UTF-8 failed, so the bytes are not UTF-8.  GB18030
    # is a full-byte superset and decodes without loss.
    try:
        text = raw.decode("gb18030", errors="strict")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")
    if not looks_like_mojibake(text):
        return text
    repaired = repair_utf8_as_gbk(text)
    if repaired != text and not looks_like_mojibake(repaired):
        return repaired
    return text


# Keep the module importable as a plain utility library; no side effects.
__all__ = [
    "decode_hook_bytes",
    "guard_persist_content",
    "looks_like_mojibake",
    "repair_utf8_as_gbk",
    "should_repair",
    "strip_pua_residue",
]
