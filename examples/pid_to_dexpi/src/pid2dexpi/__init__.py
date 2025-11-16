"""
pid2dexpi: P&ID to DEXPI and Graph Converter

A Python package for converting refinery P&ID drawings from DXF format to:
1. DEXPI XML (ISO 15926-based standard for process engineering data)
2. Graph representations (GraphML, JSON, Cypher)

Main components:
- DXFReader: Reads DXF files and extracts P&ID elements
- PIDModel: Internal object model representing P&ID data
- DEXPIExporter: Exports to DEXPI XML format
- GraphBuilder: Builds and exports graph representations
- CLI: Command-line interface

Example usage:
    >>> from pid2dexpi import DXFReader, DEXPIExporter, GraphBuilder
    >>> reader = DXFReader()
    >>> model = reader.read_dxf("drawing.dxf")
    >>> exporter = DEXPIExporter()
    >>> exporter.export(model, "output.dexpi.xml")
    >>> builder = GraphBuilder()
    >>> graph = builder.build_graph(model)
    >>> builder.export_graphml("output.graphml")
"""

__version__ = "0.1.0"
__author__ = "Refinery Engineering Team"

from .config import DXFConfig
from .dexpi_exporter import DEXPIExporter
from .dxf_reader import DXFReader
from .graph_builder import GraphBuilder
from .pid_model import (
    Connection,
    Equipment,
    EquipmentType,
    Instrument,
    InstrumentType,
    LineType,
    Nozzle,
    PIDModel,
    PipeRun,
    Point,
)

__all__ = [
    # Version
    "__version__",
    "__author__",
    # Main classes
    "DXFReader",
    "DEXPIExporter",
    "GraphBuilder",
    "DXFConfig",
    # Data model
    "PIDModel",
    "Equipment",
    "PipeRun",
    "Instrument",
    "Nozzle",
    "Connection",
    "Point",
    # Enums
    "EquipmentType",
    "InstrumentType",
    "LineType",
]
