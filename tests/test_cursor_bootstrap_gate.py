from memoryguard.host_hooks import _is_memoryguard_bootstrap, _is_memoryguard_write

def test_direct():
    assert _is_memoryguard_bootstrap("memoryguard_context_bootstrap")

def test_wrapper():
    assert _is_memoryguard_bootstrap(
        "CallMcpTool",
        {"toolName": "memoryguard_context_bootstrap"},
    )
    assert not _is_memoryguard_bootstrap(
        "CallMcpTool",
        {"toolName": "memoryguard_memory_search"},
    )

def test_write_wrapper():
    assert _is_memoryguard_write("CallMcpTool", {"toolName": "memoryguard_memory_write"})
