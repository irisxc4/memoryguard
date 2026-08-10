"""AccessContext:可信身份与授权边界。

MCP 连接启动时从环境变量派生可信 agent 身份,
工具参数里的 agent_instance_id 仅作校验,不可自报冒充。

安全策略:
- MEMORYGUARD_AGENT_ID: 当前连接绑定的可信 agent 身份
- MEMORYGUARD_ADMIN=1: 管理员权限(可创建 binding)
- MEMORYGUARD_STRICT_BINDING 默认 "1":无 binding 拒绝读写
"""
from __future__ import annotations

import os
from dataclasses import dataclass


TRUSTED_SESSION_SOURCES = frozenset({"host", "transport"})
_SESSION_SOURCES = frozenset({
    "host", "transport", "generated", "manual", "client", "absent",
})

# Process-local connection normalization for proven legacy-global migrations.
# Never mutate os.environ: the host owns that immutable connection envelope.
_RUNTIME_AGENT_ID_OVERRIDE = ""
_RUNTIME_PROVIDER_OVERRIDE = ""


def set_runtime_connection_override(
    *, agent_instance_id: str = "", provider: str = "",
) -> None:
    global _RUNTIME_AGENT_ID_OVERRIDE, _RUNTIME_PROVIDER_OVERRIDE
    _RUNTIME_AGENT_ID_OVERRIDE = str(agent_instance_id or "").strip()
    _RUNTIME_PROVIDER_OVERRIDE = str(provider or "").strip().lower()


def clear_runtime_connection_override() -> None:
    set_runtime_connection_override()


def effective_agent_id() -> str:
    return _RUNTIME_AGENT_ID_OVERRIDE or os.environ.get("MEMORYGUARD_AGENT_ID", "")


def effective_provider() -> str:
    return _RUNTIME_PROVIDER_OVERRIDE or os.environ.get("MEMORYGUARD_PROVIDER", "")


def session_trust_is_valid(
    session_id: object,
    session_source: object,
    session_trusted: object,
) -> bool:
    """Validate immutable session provenance at governance boundaries."""
    source = str(session_source or "").strip().casefold()
    return (
        session_trusted is True
        and bool(str(session_id or "").strip())
        and source in TRUSTED_SESSION_SOURCES
    )


@dataclass(frozen=True)
class AccessContext:
    """可信访问上下文,从环境变量派生,不由客户端自报。"""
    trusted_agent_id: str  # 当前连接绑定的可信 agent 身份
    is_admin: bool         # 管理员权限(可创建 binding)
    strict_binding: bool   # 严格绑定模式(默认 True)
    allow_anon: bool       # 允许匿名(MEMORYGUARD_AGENT_ID 未设置时)
    session_id: str = ""   # host/transport 注入的 session 身份
    session_source: str = "absent"  # provenance source; never inferred from id alone
    session_trusted: bool = False

    def __post_init__(self) -> None:
        session_id = str(self.session_id or "").strip()
        source = str(self.session_source or "").strip().casefold()
        if source not in _SESSION_SOURCES:
            source = "absent"
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_source", source)
        object.__setattr__(
            self,
            "session_trusted",
            session_trust_is_valid(session_id, source, self.session_trusted),
        )

    def resolve_agent(self, claimed_agent_id: str = "") -> tuple[str, str]:
        """解析本连接身份；可信环境变量为事实源，请求参数只做一致性校验。"""
        claimed = claimed_agent_id or ""
        if self.trusted_agent_id:
            if claimed and claimed != self.trusted_agent_id:
                return ("", f"agent_instance_id mismatch: claimed={claimed!r} "
                            f"but connection is bound to {self.trusted_agent_id!r}")
            return (self.trusted_agent_id, "")
        if self.allow_anon:
            return (claimed, "")
        return ("", "MEMORYGUARD_AGENT_ID not set; anonymous access denied "
                    "(set MEMORYGUARD_ALLOW_ANON=1 to allow)")

    def check_agent(self, claimed_agent_id: str) -> tuple[bool, str]:
        """校验请求中的 agent_instance_id 是否可信。

        返回 (ok, error_message)。
        - MEMORYGUARD_AGENT_ID 已设置时:缺省采用可信身份;显式 claimed 必须匹配
        - MEMORYGUARD_AGENT_ID 未设置时:allow_anon 模式允许;否则拒绝
        """
        _, err = self.resolve_agent(claimed_agent_id)
        return (not err, err)

    def require_admin(self) -> tuple[bool, str]:
        """校验管理员权限。"""
        if not self.is_admin:
            return (False, "admin capability required (set MEMORYGUARD_ADMIN=1)")
        return (True, "")

    @property
    def principal(self) -> str:
        """Return the connection-owned principal used by server capabilities."""
        return self.trusted_agent_id

    def require_capability_issue(self) -> tuple[bool, str]:
        """Require an admin context with a non-anonymous trusted principal."""
        ok, error = self.require_admin()
        if not ok:
            return (False, error)
        if not self.principal:
            return (False, "trusted principal required for capability issuance")
        if self.session_source != "absent" and not self.session_trusted:
            return (False, "trusted session context required")
        if self.session_id and not self.session_trusted:
            return (False, "trusted session context required")
        return (True, "")


def load_access_context() -> AccessContext:
    """从环境变量加载 AccessContext。"""
    return AccessContext(
        trusted_agent_id=effective_agent_id(),
        is_admin=os.environ.get("MEMORYGUARD_ADMIN", "") == "1",
        # P0-A: 默认 STRICT_BINDING=1
        strict_binding=os.environ.get("MEMORYGUARD_STRICT_BINDING", "1") != "0",
        # 默认拒绝匿名;需显式 MEMORYGUARD_ALLOW_ANON=1 开启
        allow_anon=os.environ.get("MEMORYGUARD_ALLOW_ANON", "") == "1",
        session_id=os.environ.get("MEMORYGUARD_SESSION_ID", ""),
        session_source=os.environ.get("MEMORYGUARD_SESSION_SOURCE", "absent"),
        session_trusted=True,
    )


def preflight_check(ctx: AccessContext | None = None, *, stream=None) -> list[str]:
    """A3: 启动预检,打印身份与权限态。

    返回 warning 列表(空列表表示全部正常)。
    缺身份或权限配置不当时输出明确告警。
    """
    import sys
    if stream is None:
        stream = sys.stderr
    if ctx is None:
        ctx = load_access_context()

    warnings: list[str] = []

    # 打印当前状态
    agent_display = ctx.trusted_agent_id or "(not set)"
    anon_display = "ON" if ctx.allow_anon else "OFF"
    admin_display = "ON" if ctx.is_admin else "OFF"
    strict_display = "ON" if ctx.strict_binding else "OFF"
    print(
        f"[memoryguard] identity: agent_id={agent_display} "
        f"admin={admin_display} allow_anon={anon_display} "
        f"strict_binding={strict_display}",
        file=stream,
    )

    # 缺身份告警
    if not ctx.trusted_agent_id and not ctx.allow_anon:
        w = ("MEMORYGUARD_AGENT_ID not set and MEMORYGUARD_ALLOW_ANON=0; "
             "all read/write will be denied")
        warnings.append(w)
        print(f"[memoryguard] WARNING: {w}", file=stream)

    # admin 告警(binding_create 需 admin)
    if not ctx.is_admin:
        w = "MEMORYGUARD_ADMIN not set; binding_create will be denied"
        warnings.append(w)
        print(f"[memoryguard] WARNING: {w}", file=stream)

    # strict 关闭告警
    if not ctx.strict_binding:
        w = ("MEMORYGUARD_STRICT_BINDING=0; unbound agents will fall back to "
             "'default' group (insecure for production)")
        warnings.append(w)
        print(f"[memoryguard] WARNING: {w}", file=stream)

    if not warnings:
        print("[memoryguard] preflight OK", file=stream)

    return warnings
