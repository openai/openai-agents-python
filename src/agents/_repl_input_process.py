from __future__ import annotations

import json
import os
import signal
import sys
from typing import Any, BinaryIO, cast


def _read_exactly(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_control(value: str) -> BinaryIO:
    kind, raw_value = value.split(":", 1)
    if kind == "fd":
        descriptor = int(raw_value)
    elif kind == "handle" and sys.platform == "win32":
        import msvcrt

        descriptor = msvcrt.open_osfhandle(int(raw_value), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    else:
        raise ValueError("Invalid control channel.")
    return cast(BinaryIO, os.fdopen(descriptor, "rb", buffering=0))


def main() -> None:
    terminal = sys.argv[3] == "terminal"
    cast(Any, sys.stdin).reconfigure(encoding=sys.argv[1], errors=sys.argv[2], newline=None)

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    readline_module: Any = None
    control = _open_control(sys.argv[4])
    config_size = int.from_bytes(_read_exactly(control, 8), "big")
    config = json.loads(_read_exactly(control, config_size))
    if config["readline"]:
        import readline

        readline_module = readline
        for item in config["history"]:
            readline_module.add_history(item)
    output = cast(Any, sys.stderr if terminal else sys.stdout).buffer
    prompt = sys.argv[5] if terminal else ""

    while True:
        if control.read(1) != b"R":
            break
        history_length = (
            readline_module.get_current_history_length() if readline_module is not None else 0
        )
        result: list[Any]
        try:
            if terminal and readline_module is None:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                value = input()
            else:
                value = input(prompt)
        except EOFError:
            result = ["eof", None, []]
        except UnicodeDecodeError as exc:
            result = [
                "decode_error",
                [exc.encoding, bytes(exc.object).hex(), exc.start, exc.end, exc.reason],
                [],
            ]
        except BaseException as exc:
            result = ["error", type(exc).__name__, []]
        else:
            history = (
                [
                    readline_module.get_history_item(index)
                    for index in range(
                        history_length + 1,
                        readline_module.get_current_history_length() + 1,
                    )
                ]
                if readline_module is not None
                else []
            )
            result = ["line", value, history]

        payload = json.dumps(result).encode()
        output.write(len(payload).to_bytes(8, "big"))
        output.write(payload)
        output.flush()
        if result[0] != "line":
            break


if __name__ == "__main__":
    main()
