"""
P&ID internal object model.

This module defines the core data classes representing P&ID elements:
- Equipment (vessels, pumps, compressors, heat exchangers, etc.)
- PipeRun (process lines connecting equipment)
- Instrument (measurement and control devices)
- Nozzle (connection points on equipment)
- Connection (relationships between elements)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EquipmentType(Enum):
    """Common equipment types in refinery P&IDs."""

    VESSEL = "vessel"
    TANK = "tank"
    PUMP = "pump"
    COMPRESSOR = "compressor"
    HEAT_EXCHANGER = "heat_exchanger"
    VALVE = "valve"
    REACTOR = "reactor"
    COLUMN = "column"
    DRUM = "drum"
    SEPARATOR = "separator"
    FILTER = "filter"
    UNKNOWN = "unknown"


class InstrumentType(Enum):
    """Common instrument types based on ISA nomenclature."""

    FLOW = "flow"  # F
    PRESSURE = "pressure"  # P
    TEMPERATURE = "temperature"  # T
    LEVEL = "level"  # L
    ANALYSIS = "analysis"  # A
    CONTROL = "control"  # C
    INDICATOR = "indicator"  # I
    TRANSMITTER = "transmitter"  # T (suffix)
    CONTROLLER = "controller"  # C (suffix)
    RECORDER = "recorder"  # R
    SWITCH = "switch"  # S
    UNKNOWN = "unknown"


class LineType(Enum):
    """Common line service types in refineries."""

    PROCESS = "process"
    UTILITY = "utility"
    STEAM = "steam"
    AIR = "air"
    WATER = "water"
    CHEMICAL = "chemical"
    SIGNAL = "signal"  # Instrument signal line
    UNKNOWN = "unknown"


@dataclass
class Point:
    """2D point with x, y coordinates."""

    x: float
    y: float

    def to_tuple(self) -> tuple[float, float]:
        """Return as tuple."""
        return (self.x, self.y)


@dataclass
class Nozzle:
    """
    Connection point on equipment.

    Nozzles represent physical connection points (inlets, outlets)
    on process equipment.
    """

    id: str
    equipment_id: str
    position: Point
    direction: str | None = None  # "inlet", "outlet", or None
    nominal_diameter: str | None = None  # e.g., "6\""
    service: str | None = None
    connected_line_id: str | None = None

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class Equipment:
    """
    Process equipment element.

    Represents physical equipment such as vessels, pumps, heat exchangers, etc.
    """

    id: str  # Tag number, e.g., "V-101", "P-201A"
    equipment_type: EquipmentType
    name: str | None = None
    description: str | None = None
    position: Point | None = None
    nozzles: list[Nozzle] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.id)

    def add_nozzle(self, nozzle: Nozzle) -> None:
        """Add a nozzle to this equipment."""
        self.nozzles.append(nozzle)


@dataclass
class PipeRun:
    """
    Process piping line.

    Represents a piping segment connecting equipment, typically
    identified by a line number (e.g., "6-410-P-123-A").
    """

    id: str  # Line number
    line_type: LineType
    nominal_diameter: str | None = None  # e.g., "6\""
    service_code: str | None = None  # e.g., "P" for process
    sequence_number: str | None = None  # e.g., "123"
    insulation_code: str | None = None
    path_points: list[Point] = field(default_factory=list)
    from_equipment_id: str | None = None
    to_equipment_id: str | None = None
    from_nozzle_id: str | None = None
    to_nozzle_id: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class Instrument:
    """
    Measurement and control instrument.

    Represents instruments like flow meters, pressure transmitters,
    temperature indicators, control valves, etc.
    """

    id: str  # Tag number, e.g., "FT-101", "PIC-202"
    instrument_type: InstrumentType
    function: str | None = None  # "transmitter", "indicator", "controller"
    position: Point | None = None
    connected_line_id: str | None = None
    connected_equipment_id: str | None = None
    setpoint: str | None = None
    range_min: str | None = None
    range_max: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.id)


@dataclass
class Connection:
    """
    Logical connection between P&ID elements.

    Represents the "connected_to" relationship, which can be:
    - Equipment to PipeRun
    - PipeRun to Equipment
    - PipeRun to PipeRun
    - Instrument to PipeRun
    - Instrument to Equipment
    """

    from_id: str
    to_id: str
    from_type: str  # "equipment", "pipe", "instrument"
    to_type: str
    connection_type: str | None = None  # "inlet", "outlet", "signal", etc.
    properties: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.from_id, self.to_id))


@dataclass
class PIDModel:
    """
    Complete P&ID model.

    This is the top-level container holding all P&ID elements
    extracted from a DXF drawing.
    """

    name: str
    equipment: dict[str, Equipment] = field(default_factory=dict)
    pipe_runs: dict[str, PipeRun] = field(default_factory=dict)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    nozzles: dict[str, Nozzle] = field(default_factory=dict)
    connections: list[Connection] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_equipment(self, equip: Equipment) -> None:
        """Add equipment to the model."""
        self.equipment[equip.id] = equip
        # Also add its nozzles to the nozzles dict
        for nozzle in equip.nozzles:
            self.nozzles[nozzle.id] = nozzle

    def add_pipe_run(self, pipe: PipeRun) -> None:
        """Add pipe run to the model."""
        self.pipe_runs[pipe.id] = pipe

    def add_instrument(self, inst: Instrument) -> None:
        """Add instrument to the model."""
        self.instruments[inst.id] = inst

    def add_connection(self, conn: Connection) -> None:
        """Add connection to the model."""
        self.connections.append(conn)

    def get_all_elements(self) -> list[Equipment | PipeRun | Instrument]:
        """Return all elements as a flat list."""
        elements: list[Equipment | PipeRun | Instrument] = []
        elements.extend(self.equipment.values())
        elements.extend(self.pipe_runs.values())
        elements.extend(self.instruments.values())
        return elements

    def summary(self) -> str:
        """Return a summary string of the model contents."""
        return (
            f"PIDModel '{self.name}':\n"
            f"  Equipment: {len(self.equipment)}\n"
            f"  Pipe Runs: {len(self.pipe_runs)}\n"
            f"  Instruments: {len(self.instruments)}\n"
            f"  Nozzles: {len(self.nozzles)}\n"
            f"  Connections: {len(self.connections)}"
        )
