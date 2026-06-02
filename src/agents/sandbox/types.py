import stat
from dataclasses import dataclass
from enum import IntEnum

from pydantic import BaseModel, Field
from typing_extensions import Self


class User(BaseModel):
    name: str

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, User):
            return NotImplemented
        return self.name == other.name


class Group(BaseModel):
    name: str
    users: list[User]

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Group):
            return NotImplemented
        return self.name == other.name


class Permissions(BaseModel):
    owner: int = Field(default=0o7)
    group: int = Field(default=0)
    other: int = Field(default=0)
    directory: bool = Field(default=False)

    def to_mode(self) -> int:
        mode = 0
        for perms, shift in [(self.owner, 6), (self.group, 3), (self.other, 0)]:
            mode |= int(perms) << shift
        if self.directory:
            mode |= stat.S_IFDIR
        return mode

    @classmethod
    def from_mode(cls, mode: int) -> "Permissions":
        return cls(
            owner=(mode >> 6) & 0b111,
            group=(mode >> 3) & 0b111,
            other=(mode >> 0) & 0b111,
            directory=bool(mode & stat.S_IFDIR),
        )

    @classmethod
    def from_str(cls, perms: str) -> "Permissions":
        if len(perms) == 11 and perms[-1] in {"@", "+"}:
            perms = perms[:-1]
        if len(perms) != 10:
            raise ValueError(f"invalid permissions string length: {perms!r}")

        directory = perms[0] == "d"
        if perms[0] not in {"d", "-"}:
            raise ValueError(f"invalid permissions type: {perms!r}")

        def parse_triplet(triplet: str) -> int:
            if len(triplet) != 3:
                raise ValueError(f"invalid permissions triplet: {triplet!r}")
            mask = 0
            if triplet[0] == "r":
                mask |= FileMode.READ
            elif triplet[0] != "-":
                raise ValueError(f"invalid read flag: {triplet!r}")
            if triplet[1] == "w":
                mask |= FileMode.WRITE
            elif triplet[1] != "-":
                raise ValueError(f"invalid write flag: {triplet!r}")
            if triplet[2] == "x":
                mask |= FileMode.EXEC
            elif triplet[2] != "-":
                raise ValueError(f"invalid exec flag: {triplet!r}")
            return int(mask)

        owner = parse_triplet(perms[1:4])
        group = parse_triplet(perms[4:7])
        other = parse_triplet(perms[7:10])
        return cls(
            owner=owner,
            group=group,
            other=other,
            directory=directory,
        )

    def owner_can(self, mode: int) -> Self:
        self.owner = mode
        return self

    def group_can(self, mode: int) -> Self:
        self.group = mode
        return self

    def others_can(self, mode: int) -> Self:
        self.other = mode
        return self

    def __repr__(self) -> str:
        def fmt(perms: int) -> str:
            return "".join(
                c if perms & p else "-"
                for p, c in [(FileMode.READ, "r"), (FileMode.WRITE, "w"), (FileMode.EXEC, "x")]
            )

        return ("d" if self.directory else "-") + "".join(
            fmt(perms) for perms in (self.owner, self.group, self.other)
        )

    def __str__(self) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Permissions):
            return NotImplemented
        return self.to_mode() == other.to_mode()

    def __hash__(self) -> int:
        return hash(self.to_mode())


class FileMode(IntEnum):
    ALL = 0o7
    NONE = 0

    READ = 1 << 2
    WRITE = 1 << 1
    EXEC = 1


class ExecResult:
    stdout: bytes
    stderr: bytes
    exit_code: int

    def __init__(self, *, stdout: bytes, stderr: bytes, exit_code: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class ExposedPortEndpoint:
    host: str
    port: int
    tls: bool = False
    query: str = ""

    def url_for(self, scheme: str) -> str:
        normalized = scheme.lower()
        if normalized not in {"http", "ws"}:
            raise ValueError("scheme must be either 'http' or 'ws'")

        if normalized == "http":
            prefix = "https" if self.tls else "http"
            default_port = 443 if self.tls else 80
        else:
            prefix = "wss" if self.tls else "ws"
            default_port = 443 if self.tls else 80

        if ":" in self.host and not self.host.startswith("["):
            host = f"[{self.host}]"
        else:
            host = self.host

        if self.port == default_port:
            base = f"{prefix}://{host}/"
        else:
            base = f"{prefix}://{host}:{self.port}/"

        if self.query:
            query = self.query[1:] if self.query.startswith("?") else self.query
            if query:
                return f"{base}?{query}"

        return base
