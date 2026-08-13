from __future__ import annotations

import json
from unittest.mock import patch

from memoryguard.host_agent_backend import batch_enrich_via_cli


def test_batch_enrich_retries_only_missing_results_once() -> None:
    tasks = [
        {
            "task_id": f"task-{index}",
            "input": {
                "title": f"title-{index}",
                "body": f"body-{index}",
                "kind_hint": "fact",
            },
        }
        for index in range(3)
    ]
    calls: list[list[str]] = []

    def mock_call(agent, cli, system, user, timeout=60, expect_array=False):
        items = json.loads(user)
        calls.append([str(item["task_id"]) for item in items])
        chosen = [items[0], items[2]] if len(calls) == 1 else items
        return [
            {
                "index": int(item["index"]),
                "kind": "fact",
                "title": f"translated-{item['task_id']}",
                "body": "body",
                "confidence": 0.9,
            }
            for item in chosen
        ]

    with patch("memoryguard.host_agent_backend._call_llm_json", side_effect=mock_call):
        results = batch_enrich_via_cli(tasks, agent="mock", cli_path="mock")

    assert calls == [["task-0", "task-1", "task-2"], ["task-1"]]
    assert [item["task_id"] for item in results] == ["task-0", "task-1", "task-2"]
