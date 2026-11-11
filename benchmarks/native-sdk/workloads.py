"""Frozen workload inputs shared by the comparison clients."""

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Workload:
    name: str
    mode: str = "conversation"
    iterations: int = 40
    concurrency: int = 1
    prefill: int = 1
    input_chars: int = 128
    output_chars: int = 128
    chunks: int = 1
    stream: bool = False
    tools_per_round: int = 0
    tool_rounds: int = 0
    sync_tool: bool = False
    delay_ms: float = 0

    def dictionary(self):
        return asdict(self)


WORKLOADS = {
    value.name: value
    for value in [
        Workload("cold_first_result", mode="oneshot", iterations=1, prefill=0),
        Workload("oneshot_text", mode="oneshot", iterations=20),
        *[
            Workload(
                f"oneshot_output_64k_{chunks}_plain",
                mode="oneshot",
                iterations=20,
                output_chars=65536,
                chunks=chunks,
            )
            for chunks in (1, 256, 4096)
        ],
        Workload("warm_text", iterations=1),
        Workload("warm_tool", iterations=1, tools_per_round=1, tool_rounds=1),
        Workload("warm_chain_5", iterations=1, tools_per_round=1, tool_rounds=5),
        Workload("warm_stream", iterations=1, stream=True, chunks=16),
        Workload("conversation_text", iterations=80),
        Workload(
            "oneshot_tool",
            mode="oneshot",
            iterations=20,
            tools_per_round=1,
            tool_rounds=1,
        ),
        Workload("conversation_tool", tools_per_round=1, tool_rounds=1),
        Workload("tool_chain_5", iterations=20, tools_per_round=1, tool_rounds=5),
        Workload("tool_fanout_8", iterations=20, tools_per_round=8, tool_rounds=1),
        Workload("sync_tool", tools_per_round=1, tool_rounds=1, sync_tool=True),
        Workload(
            "streamed_tool",
            iterations=20,
            stream=True,
            chunks=16,
            tools_per_round=1,
            tool_rounds=1,
        ),
        Workload("stream_128", iterations=25, stream=True, chunks=128, output_chars=8192),
        Workload(
            "stream_256_large",
            iterations=15,
            stream=True,
            chunks=256,
            output_chars=65536,
        ),
        Workload("input_64k", mode="oneshot", iterations=15, input_chars=65536),
        Workload("input_512k", mode="oneshot", iterations=15, input_chars=512 * 1024),
        Workload("history_32", iterations=20, prefill=32, input_chars=256, output_chars=256),
        Workload("history_256", iterations=20, prefill=256, input_chars=256, output_chars=256),
        Workload("concurrency_8", iterations=20, concurrency=8),
        Workload("concurrency_32", iterations=10, concurrency=32),
        Workload("oneshot_concurrency_8", mode="oneshot", iterations=10, concurrency=8),
        Workload("model_delay_50ms", iterations=15, delay_ms=50),
    ]
}


def get_workload(name, *, iterations=None):
    value = WORKLOADS[name]
    return replace(value, iterations=iterations) if iterations is not None else value
