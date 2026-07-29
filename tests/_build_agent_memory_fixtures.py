"""Build synthetic agent memory fixtures (no real home dirs)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures" / "agent_memories"


def main() -> None:
    # Claude
    claude = ROOT / "home" / ".claude" / "projects" / "demo-proj"
    (claude / "memory").mkdir(parents=True, exist_ok=True)
    (claude / "subagents").mkdir(parents=True, exist_ok=True)
    (claude / "memory" / "MEMORY.md").write_text("# Memory Index\n- user.md\n", encoding="utf-8")
    (claude / "memory" / "user.md").write_text(
        "---\ntype: user\ntitle: Prefer concise code\n---\n"
        "User prefers concise code and short answers.\n",
        encoding="utf-8",
    )
    (claude / "memory" / "project.md").write_text(
        "---\ntype: project\ntitle: Python version\n---\n"
        "Project uses Python 3.12.\n",
        encoding="utf-8",
    )
    session_lines = [
        {"type": "message", "message": {"role": "user", "content": "Please remember that I prefer short answers"}},
        {"type": "tool_call", "tool_call": {"name": "bash", "input": "ls"}},
        {"type": "message", "message": {"role": "assistant", "content": "Noted."}},
        {"type": "message", "message": {"role": "user", "content": "hello"}},
    ]
    (claude / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(x) for x in session_lines) + "\n", encoding="utf-8",
    )
    (claude / "subagents" / "agent-1.jsonl").write_text(
        json.dumps({"type": "message", "message": {"role": "user", "content": "I prefer dark mode always"}})
        + "\n",
        encoding="utf-8",
    )

    # Cursor transcripts
    cur = ROOT / "home" / ".cursor" / "projects" / "encoded-path" / "agent-transcripts" / "uuid-a"
    cur.mkdir(parents=True, exist_ok=True)
    (cur / "uuid-a.jsonl").write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "I prefer TypeScript strict mode"}),
            json.dumps({"type": "tool_call", "name": "read"}),
            json.dumps({"role": "assistant", "content": "ok"}),
        ]) + "\n",
        encoding="utf-8",
    )

    # Codex sessions + sqlite + native memories
    codex = ROOT / "home" / ".codex" / "sessions" / "2026" / "07" / "28"
    codex.mkdir(parents=True, exist_ok=True)
    (ROOT / "home" / ".codex" / "memories").mkdir(parents=True, exist_ok=True)
    (ROOT / "home" / ".codex" / "memories" / "MEMORY.md").write_text(
        "# Codex Memory Index\n- preferences.md\n", encoding="utf-8",
    )
    (ROOT / "home" / ".codex" / "memories" / "preferences.md").write_text(
        "---\ntype: preference\n---\nUser prefers pytest and type hints.\n",
        encoding="utf-8",
    )
    (codex / "rollout-demo.jsonl").write_text(
        "\n".join([
            json.dumps({"timestamp": "t", "type": "message", "payload": {"role": "user", "content": "remember that I prefer pytest"}}),
            json.dumps({"timestamp": "t", "type": "tool_call", "payload": {"name": "shell"}}),
            json.dumps({"timestamp": "t", "type": "message", "payload": {"role": "assistant", "content": "sure"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    db = ROOT / "home" / ".codex" / "state_5.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, title TEXT)")
    conn.execute("INSERT INTO sessions VALUES ('s1', 'demo')")
    conn.commit()
    conn.close()

    # TRAE
    trae_day = ROOT / "home" / ".trae-cn" / "memory" / "projects" / "enc-proj" / "2026-07-28"
    trae_day.mkdir(parents=True, exist_ok=True)
    (ROOT / "home" / ".trae-cn" / "memory" / "user_profile.md").write_text(
        "# Profile\nUser likes concise Chinese replies.\n", encoding="utf-8",
    )
    (trae_day.parent / "project_memory.md").write_text(
        "# Project\nAlways run tests before commit.\n", encoding="utf-8",
    )
    (trae_day / "session_memory_1.jsonl").write_text(
        "\n".join([
            json.dumps({"intent": "setup", "actions": ["init"], "outcome": "ok", "learned": "Use uv for deps"}),
            json.dumps({"intent": "chat", "actions": ["talk"], "outcome": "", "learned": ""}),
            json.dumps({"type": "tool_call", "name": "x"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (trae_day / "topics.md").write_text(
        "[session_id: abc]\nFirst topic summary about testing.\n\n"
        "[session_id: def]\nSecond topic about packaging.\n",
        encoding="utf-8",
    )

    # Cursor vscdb stub
    vsc = ROOT / "appdata" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    vsc.parent.mkdir(parents=True, exist_ok=True)
    if vsc.exists():
        vsc.unlink()
    conn = sqlite3.connect(vsc)
    conn.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
    conn.execute("INSERT INTO ItemTable VALUES ('composer.sessions', ?)", (b"\x00",))
    conn.commit()
    conn.close()

    print("fixtures ok", ROOT)


if __name__ == "__main__":
    main()
