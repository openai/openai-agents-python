"""
DEXPI XML exporter for P&ID models.

This module exports P&ID models to DEXPI (Data Exchange in the Process Industry) XML format.
DEXPI is an ISO 15926-based standard for exchanging P&ID and process engineering data.

This implementation supports a pragmatic subset of DEXPI focused on:
- Equipment (ProcessInstrument, Vessel, Pump, etc.)
- Piping (PipingNetworkSegment, PipeConnector)
- Instruments (ProcessInstrument)
- Connections (ProcessConnection)

For full DEXPI specification, see: https://dexpi.org/
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import etree
from lxml.etree import Element, SubElement

from .pid_model import (
    Connection,
    Equipment,
    EquipmentType,
    Instrument,
    InstrumentType,
    PIDModel,
    PipeRun,
)

logger = logging.getLogger(__name__)


class DEXPIExporter:
    """
    Export P&ID models to DEXPI XML format.

    This exporter creates a DEXPI-compliant XML representation of P&ID data
    that can be imported into process engineering software.
    """

    # DEXPI namespace
    DEXPI_NS = "http://www.dexpi.org/2013/DEXPI"
    XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

    # Equipment type mapping to DEXPI component classes
    EQUIPMENT_CLASS_MAP = {
        EquipmentType.VESSEL: "Vessel",
        EquipmentType.TANK: "Tank",
        EquipmentType.PUMP: "Pump",
        EquipmentType.COMPRESSOR: "Compressor",
        EquipmentType.HEAT_EXCHANGER: "HeatExchanger",
        EquipmentType.VALVE: "Valve",
        EquipmentType.REACTOR: "Reactor",
        EquipmentType.COLUMN: "Column",
        EquipmentType.DRUM: "Drum",
        EquipmentType.SEPARATOR: "Separator",
        EquipmentType.FILTER: "Filter",
        EquipmentType.UNKNOWN: "Equipment",
    }

    # Instrument type mapping
    INSTRUMENT_CLASS_MAP = {
        InstrumentType.FLOW: "FlowMeter",
        InstrumentType.PRESSURE: "PressureMeter",
        InstrumentType.TEMPERATURE: "TemperatureMeter",
        InstrumentType.LEVEL: "LevelMeter",
        InstrumentType.ANALYSIS: "Analyzer",
        InstrumentType.CONTROL: "Controller",
        InstrumentType.INDICATOR: "Indicator",
        InstrumentType.TRANSMITTER: "Transmitter",
        InstrumentType.CONTROLLER: "Controller",
        InstrumentType.RECORDER: "Recorder",
        InstrumentType.SWITCH: "Switch",
        InstrumentType.UNKNOWN: "Instrument",
    }

    def __init__(self):
        """Initialize the DEXPI exporter."""
        self.nsmap = {
            None: self.DEXPI_NS,
            "xsi": self.XSI_NS,
        }

    def export(self, model: PIDModel, output_path: str | Path) -> None:
        """
        Export a P&ID model to DEXPI XML file.

        Args:
            model: PIDModel to export
            output_path: Path for output XML file
        """
        output_path = Path(output_path)
        logger.info(f"Exporting DEXPI XML to: {output_path}")

        # Build XML tree
        root = self._build_dexpi_tree(model)

        # Write to file with pretty printing
        tree = etree.ElementTree(root)
        tree.write(
            str(output_path),
            pretty_print=True,
            xml_declaration=True,
            encoding="UTF-8",
        )

        logger.info(f"DEXPI XML export complete: {output_path}")

    def _build_dexpi_tree(self, model: PIDModel) -> Element:
        """
        Build the DEXPI XML tree structure.

        Args:
            model: PIDModel to convert

        Returns:
            Root XML element
        """
        # Create root element
        root = Element(
            f"{{{self.DEXPI_NS}}}PlantModel",
            nsmap=self.nsmap,
            attrib={
                f"{{{self.XSI_NS}}}schemaLocation": f"{self.DEXPI_NS} Proteus.xsd",
            },
        )

        # Add metadata
        self._add_metadata(root, model)

        # Create PlantItem container
        plant_item = SubElement(root, f"{{{self.DEXPI_NS}}}PlantItem")
        plant_item.set("ID", self._generate_id("PLANT"))
        plant_item.set("TagName", model.name)

        # Add Equipment section
        equipment_section = SubElement(plant_item, f"{{{self.DEXPI_NS}}}Equipment")
        for equip in model.equipment.values():
            self._add_equipment(equipment_section, equip)

        # Add PipingNetworkSystem section
        piping_section = SubElement(plant_item, f"{{{self.DEXPI_NS}}}PipingNetworkSystem")
        piping_section.set("ID", self._generate_id("PIPING"))

        for pipe in model.pipe_runs.values():
            self._add_pipe_run(piping_section, pipe)

        # Add ProcessInstrumentation section
        instrument_section = SubElement(
            plant_item, f"{{{self.DEXPI_NS}}}ProcessInstrumentation"
        )
        for inst in model.instruments.values():
            self._add_instrument(instrument_section, inst)

        # Add Connections section
        if model.connections:
            connections_section = SubElement(plant_item, f"{{{self.DEXPI_NS}}}Connections")
            for conn in model.connections:
                self._add_connection(connections_section, conn)

        return root

    def _add_metadata(self, root: Element, model: PIDModel) -> None:
        """
        Add metadata to the root element.

        Args:
            root: Root XML element
            model: PIDModel
        """
        metadata = SubElement(root, f"{{{self.DEXPI_NS}}}Metadata")

        # Add creation date
        date_elem = SubElement(metadata, f"{{{self.DEXPI_NS}}}CreationDate")
        date_elem.text = datetime.now().isoformat()

        # Add source information
        if "source_file" in model.metadata:
            source_elem = SubElement(metadata, f"{{{self.DEXPI_NS}}}SourceFile")
            source_elem.text = str(model.metadata["source_file"])

        # Add application info
        app_elem = SubElement(metadata, f"{{{self.DEXPI_NS}}}CreatingApplication")
        app_elem.text = "pid2dexpi v0.1.0"

    def _add_equipment(self, parent: Element, equip: Equipment) -> None:
        """
        Add equipment element to XML.

        Args:
            parent: Parent XML element
            equip: Equipment object
        """
        # Get DEXPI class name
        class_name = self.EQUIPMENT_CLASS_MAP.get(equip.equipment_type, "Equipment")

        # Create equipment element
        equip_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}{class_name}")
        equip_elem.set("ID", self._sanitize_id(equip.id))
        equip_elem.set("TagName", equip.id)

        if equip.name:
            equip_elem.set("Name", equip.name)

        # Add position if available
        if equip.position:
            self._add_position(equip_elem, equip.position.x, equip.position.y)

        # Add properties as GenericAttributes
        if equip.properties:
            self._add_generic_attributes(equip_elem, equip.properties)

        # Add nozzles
        if equip.nozzles:
            nozzles_elem = SubElement(equip_elem, f"{{{self.DEXPI_NS}}}Nozzles")
            for nozzle in equip.nozzles:
                nozzle_elem = SubElement(nozzles_elem, f"{{{self.DEXPI_NS}}}Nozzle")
                nozzle_elem.set("ID", self._sanitize_id(nozzle.id))

                if nozzle.nominal_diameter:
                    nozzle_elem.set("NominalDiameter", nozzle.nominal_diameter)

                if nozzle.direction:
                    nozzle_elem.set("FlowDirection", nozzle.direction)

                if nozzle.position:
                    self._add_position(nozzle_elem, nozzle.position.x, nozzle.position.y)

    def _add_pipe_run(self, parent: Element, pipe: PipeRun) -> None:
        """
        Add pipe run element to XML.

        Args:
            parent: Parent XML element
            pipe: PipeRun object
        """
        # Create PipingNetworkSegment element
        pipe_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}PipingNetworkSegment")
        pipe_elem.set("ID", self._sanitize_id(pipe.id))
        pipe_elem.set("TagName", pipe.id)

        if pipe.nominal_diameter:
            pipe_elem.set("NominalDiameter", pipe.nominal_diameter)

        # Add line type
        pipe_elem.set("PipeLineType", pipe.line_type.value)

        # Add path geometry
        if pipe.path_points:
            geometry_elem = SubElement(pipe_elem, f"{{{self.DEXPI_NS}}}Geometry")
            polyline_elem = SubElement(geometry_elem, f"{{{self.DEXPI_NS}}}Polyline")

            for point in pipe.path_points:
                point_elem = SubElement(polyline_elem, f"{{{self.DEXPI_NS}}}Point")
                point_elem.set("X", str(point.x))
                point_elem.set("Y", str(point.y))

        # Add service information
        if pipe.service_code or pipe.insulation_code:
            attrs = {}
            if pipe.service_code:
                attrs["ServiceCode"] = pipe.service_code
            if pipe.insulation_code:
                attrs["InsulationCode"] = pipe.insulation_code
            self._add_generic_attributes(pipe_elem, attrs)

        # Add connection references
        if pipe.from_equipment_id:
            from_elem = SubElement(pipe_elem, f"{{{self.DEXPI_NS}}}FromEquipment")
            from_elem.set("ComponentRef", self._sanitize_id(pipe.from_equipment_id))

        if pipe.to_equipment_id:
            to_elem = SubElement(pipe_elem, f"{{{self.DEXPI_NS}}}ToEquipment")
            to_elem.set("ComponentRef", self._sanitize_id(pipe.to_equipment_id))

    def _add_instrument(self, parent: Element, inst: Instrument) -> None:
        """
        Add instrument element to XML.

        Args:
            parent: Parent XML element
            inst: Instrument object
        """
        # Get DEXPI class name
        class_name = self.INSTRUMENT_CLASS_MAP.get(inst.instrument_type, "Instrument")

        # Create instrument element
        inst_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}ProcessInstrument")
        inst_elem.set("ID", self._sanitize_id(inst.id))
        inst_elem.set("TagName", inst.id)
        inst_elem.set("InstrumentType", class_name)

        if inst.function:
            inst_elem.set("Function", inst.function)

        # Add position if available
        if inst.position:
            self._add_position(inst_elem, inst.position.x, inst.position.y)

        # Add range and setpoint
        attrs = {}
        if inst.range_min:
            attrs["RangeMin"] = inst.range_min
        if inst.range_max:
            attrs["RangeMax"] = inst.range_max
        if inst.setpoint:
            attrs["Setpoint"] = inst.setpoint

        if attrs or inst.properties:
            combined_attrs = {**attrs, **inst.properties}
            self._add_generic_attributes(inst_elem, combined_attrs)

        # Add connection reference
        if inst.connected_line_id:
            conn_elem = SubElement(inst_elem, f"{{{self.DEXPI_NS}}}ConnectedTo")
            conn_elem.set("ComponentRef", self._sanitize_id(inst.connected_line_id))

    def _add_connection(self, parent: Element, conn: Connection) -> None:
        """
        Add connection element to XML.

        Args:
            parent: Parent XML element
            conn: Connection object
        """
        conn_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}ProcessConnection")
        conn_elem.set("ID", self._generate_id("CONN"))

        # Add from reference
        from_elem = SubElement(conn_elem, f"{{{self.DEXPI_NS}}}FromComponent")
        from_elem.set("ComponentRef", self._sanitize_id(conn.from_id))
        from_elem.set("ComponentType", conn.from_type)

        # Add to reference
        to_elem = SubElement(conn_elem, f"{{{self.DEXPI_NS}}}ToComponent")
        to_elem.set("ComponentRef", self._sanitize_id(conn.to_id))
        to_elem.set("ComponentType", conn.to_type)

        if conn.connection_type:
            conn_elem.set("ConnectionType", conn.connection_type)

        # Add properties
        if conn.properties:
            self._add_generic_attributes(conn_elem, conn.properties)

    def _add_position(self, parent: Element, x: float, y: float, z: float = 0.0) -> None:
        """
        Add position/coordinates to element.

        Args:
            parent: Parent XML element
            x: X coordinate
            y: Y coordinate
            z: Z coordinate (default 0.0 for 2D drawings)
        """
        pos_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}Position")
        pos_elem.set("X", str(x))
        pos_elem.set("Y", str(y))
        pos_elem.set("Z", str(z))

    def _add_generic_attributes(self, parent: Element, attributes: dict[str, Any]) -> None:
        """
        Add generic attributes section to element.

        Args:
            parent: Parent XML element
            attributes: Dictionary of attribute name-value pairs
        """
        if not attributes:
            return

        attrs_elem = SubElement(parent, f"{{{self.DEXPI_NS}}}GenericAttributes")

        for name, value in attributes.items():
            attr_elem = SubElement(attrs_elem, f"{{{self.DEXPI_NS}}}GenericAttribute")
            attr_elem.set("Name", str(name))
            attr_elem.set("Value", str(value))

    @staticmethod
    def _generate_id(prefix: str) -> str:
        """
        Generate a unique ID with prefix.

        Args:
            prefix: Prefix for the ID

        Returns:
            Unique ID string
        """
        return f"{prefix}_{uuid.uuid4().hex[:8].upper()}"

    @staticmethod
    def _sanitize_id(id_str: str) -> str:
        """
        Sanitize an ID string for XML.

        XML IDs cannot start with numbers and should not contain special chars.

        Args:
            id_str: Original ID string

        Returns:
            Sanitized ID string
        """
        # Replace special characters with underscore
        sanitized = id_str.replace('"', "").replace("'", "").replace(" ", "_")
        sanitized = sanitized.replace("/", "_").replace("\\", "_")

        # Ensure it doesn't start with a number
        if sanitized and sanitized[0].isdigit():
            sanitized = f"ID_{sanitized}"

        return sanitized
