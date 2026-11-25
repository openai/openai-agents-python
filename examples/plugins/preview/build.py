"""Build a local plugin preview with Hatchling and optionally bundle the matching Codex runtime."""

import argparse
import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZipFile

from hatchling.builders.wheel import WheelBuilder

ROOT = Path(__file__).resolve().parents[3]
TAG = "cp310-abi3-manylinux_2_28_x86_64"
SHA256 = "2912ef478fe35a2357616cfd0e0e76eed581230921f0ada1310327b86f0a0a6c"


class CodexWheelBuilder(WheelBuilder):
    def get_default_build_data(self) -> dict[str, Any]:
        return {**super().get_default_build_data(), "pure_python": False, "tag": TAG}


def stage_codex(wheel: Path, directory: str) -> dict[str, str]:
    data = wheel.read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Expected the Codex wheel from run 45484940731. See preview/README.md.")
    includes = {}
    with ZipFile(io.BytesIO(data)) as archive:
        for file in archive.infolist():
            name = file.filename
            if name.startswith("agents/models/") and "codex" in name and name.endswith(".py"):
                if (ROOT / "src" / name).read_bytes() != archive.read(file):
                    raise ValueError(f"The pinned runtime no longer matches {name}")
            if name == "agents/models/_codex_native.abi3.so" or name.startswith(
                "agents/models/_codex_bin/"
            ):
                path = Path(archive.extract(file, directory))
                path.chmod((file.external_attr >> 16) & 0o777)
                includes[str(path)] = name
    return includes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--codex-wheel", type=Path, help="Pinned Linux x86-64 wheel from #6123")
    args = parser.parse_args()
    with TemporaryDirectory() as temporary:
        builder = CodexWheelBuilder(str(ROOT)) if args.codex_wheel else WheelBuilder(str(ROOT))
        builder.raw_config["project"]["version"] += "+plugins.preview.1"
        config = builder.raw_config["tool"]["hatch"]["build"]
        config["exclude"] += ["/src/agents/models/_codex_bin", "/src/agents/models/_codex_native.*"]
        if args.codex_wheel:
            config["force-include"] = stage_codex(args.codex_wheel, temporary)
        (wheel,) = builder.build(directory=str(args.out_dir.resolve()), versions=["standard"])
    print(wheel)


if __name__ == "__main__":
    main()
