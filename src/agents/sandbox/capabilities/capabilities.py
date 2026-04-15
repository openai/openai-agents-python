from .capability import Capability
from .compaction import Compaction
from .filesystem import Filesystem
from .shell import Shell


class Capabilities:
    @classmethod
    def default(cls) -> list[Capability]:
        return [Filesystem(), Shell(), Compaction()]
