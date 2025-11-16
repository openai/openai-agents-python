"""
Configuration and mapping tables for DXF to P&ID conversion.

This module contains:
- Layer name to element type mappings
- Block name to equipment type mappings
- Attribute name mappings
- Parsing rules and patterns

These mappings can be customized for different CAD standards and conventions.
"""

from __future__ import annotations

import re
from typing import Any

from .pid_model import EquipmentType, InstrumentType, LineType


class DXFConfig:
    """
    Configuration for DXF parsing.

    Contains mapping tables to translate DXF entities (layers, blocks, attributes)
    into P&ID elements.
    """

    # Layer name patterns mapped to element categories
    # These are regex patterns that match against DXF layer names
    LAYER_PATTERNS: dict[str, str] = {
        r"^EQUIP.*": "equipment",
        r"^EQUIPMENT.*": "equipment",
        r"^VESSEL.*": "equipment",
        r"^PUMP.*": "equipment",
        r"^TANK.*": "equipment",
        r"^PIPE.*": "pipe",
        r"^LINE.*": "pipe",
        r"^PROCESS.*": "pipe",
        r"^INST.*": "instrument",
        r"^INSTRUMENT.*": "instrument",
        r"^CONTROL.*": "instrument",
        r"^SIGNAL.*": "signal",
        r"^TEXT.*": "annotation",
        r"^ANNO.*": "annotation",
        r"^DIM.*": "dimension",
        r"^BORDER.*": "border",
        r"^TITLE.*": "title",
    }

    # Block name patterns mapped to equipment types
    # Block references (INSERT entities) often represent equipment symbols
    BLOCK_TO_EQUIPMENT: dict[str, EquipmentType] = {
        # Vessels and tanks
        "VESSEL": EquipmentType.VESSEL,
        "TANK": EquipmentType.TANK,
        "DRUM": EquipmentType.DRUM,
        "SEPARATOR": EquipmentType.SEPARATOR,
        "COLUMN": EquipmentType.COLUMN,
        "REACTOR": EquipmentType.REACTOR,
        # Rotating equipment
        "PUMP": EquipmentType.PUMP,
        "CENTRIFUGAL_PUMP": EquipmentType.PUMP,
        "COMPRESSOR": EquipmentType.COMPRESSOR,
        # Heat transfer equipment
        "HEAT_EXCHANGER": EquipmentType.HEAT_EXCHANGER,
        "HX": EquipmentType.HEAT_EXCHANGER,
        "EXCHANGER": EquipmentType.HEAT_EXCHANGER,
        "COOLER": EquipmentType.HEAT_EXCHANGER,
        "HEATER": EquipmentType.HEAT_EXCHANGER,
        # Valves
        "VALVE": EquipmentType.VALVE,
        "GATE_VALVE": EquipmentType.VALVE,
        "BALL_VALVE": EquipmentType.VALVE,
        "GLOBE_VALVE": EquipmentType.VALVE,
        "CHECK_VALVE": EquipmentType.VALVE,
        "CONTROL_VALVE": EquipmentType.VALVE,
        # Filters
        "FILTER": EquipmentType.FILTER,
        "STRAINER": EquipmentType.FILTER,
    }

    # Instrument tag prefix to instrument type mapping (ISA standard)
    # Format: XY-nnn where X is measured variable, Y is function
    INSTRUMENT_PREFIX_MAP: dict[str, InstrumentType] = {
        "F": InstrumentType.FLOW,  # Flow
        "P": InstrumentType.PRESSURE,  # Pressure
        "T": InstrumentType.TEMPERATURE,  # Temperature
        "L": InstrumentType.LEVEL,  # Level
        "A": InstrumentType.ANALYSIS,  # Analysis
    }

    INSTRUMENT_SUFFIX_MAP: dict[str, str] = {
        "I": "indicator",
        "T": "transmitter",
        "C": "controller",
        "R": "recorder",
        "S": "switch",
        "V": "valve",  # Control valve
        "E": "element",  # Sensing element
    }

    # Line service code to line type mapping
    # Common refinery line numbering: size-area-service-sequence-insulation
    # Example: 6"-410-P-123-A
    LINE_SERVICE_MAP: dict[str, LineType] = {
        "P": LineType.PROCESS,
        "U": LineType.UTILITY,
        "S": LineType.STEAM,
        "A": LineType.AIR,
        "W": LineType.WATER,
        "C": LineType.CHEMICAL,
        "I": LineType.SIGNAL,
    }

    # Attribute names to look for in DXF blocks
    # These are common attribute tags used in P&ID drawings
    EQUIPMENT_ATTRIBUTE_NAMES = {
        "TAG",
        "TAG_NUMBER",
        "EQUIPMENT_TAG",
        "EQUIP_TAG",
        "NAME",
        "DESCRIPTION",
        "DESC",
        "SERVICE",
        "SIZE",
        "CAPACITY",
    }

    PIPE_ATTRIBUTE_NAMES = {
        "LINE_NUMBER",
        "LINE_NO",
        "LINE_TAG",
        "PIPE_TAG",
        "SIZE",
        "SERVICE",
        "SPEC",
        "INSULATION",
        "INSUL",
    }

    INSTRUMENT_ATTRIBUTE_NAMES = {
        "TAG",
        "TAG_NUMBER",
        "INST_TAG",
        "INSTRUMENT_TAG",
        "FUNCTION",
        "RANGE",
        "SETPOINT",
        "SP",
    }

    @staticmethod
    def match_layer_category(layer_name: str) -> str | None:
        """
        Match a DXF layer name to a category using regex patterns.

        Args:
            layer_name: The DXF layer name (e.g., "EQUIPMENT-VESSELS")

        Returns:
            Category string ("equipment", "pipe", "instrument", etc.) or None
        """
        layer_upper = layer_name.upper()
        for pattern, category in DXFConfig.LAYER_PATTERNS.items():
            if re.match(pattern, layer_upper):
                return category
        return None

    @staticmethod
    def map_block_to_equipment_type(block_name: str) -> EquipmentType:
        """
        Map a DXF block name to an equipment type.

        Args:
            block_name: The DXF block name (e.g., "PUMP", "VESSEL")

        Returns:
            EquipmentType enum value
        """
        block_upper = block_name.upper()

        # Try exact match first
        if block_upper in DXFConfig.BLOCK_TO_EQUIPMENT:
            return DXFConfig.BLOCK_TO_EQUIPMENT[block_upper]

        # Try partial match (e.g., "CENTRIFUGAL_PUMP_001" matches "PUMP")
        for key, equip_type in DXFConfig.BLOCK_TO_EQUIPMENT.items():
            if key in block_upper:
                return equip_type

        return EquipmentType.UNKNOWN

    @staticmethod
    def parse_instrument_tag(tag: str) -> tuple[InstrumentType, str | None]:
        """
        Parse an instrument tag to determine type and function.

        ISA standard format: XY-nnn
        X = measured variable (F, P, T, L, A)
        Y = function (I, T, C, R, S, V, E)

        Args:
            tag: Instrument tag (e.g., "FT-101", "PIC-202")

        Returns:
            Tuple of (InstrumentType, function_string)
        """
        tag_upper = tag.upper()

        # Try to match pattern like "FT-101" or "PIC-202"
        match = re.match(r"^([A-Z])([A-Z]+)-?(\d+)", tag_upper)
        if not match:
            return InstrumentType.UNKNOWN, None

        measured_var = match.group(1)
        function_letters = match.group(2)

        inst_type = DXFConfig.INSTRUMENT_PREFIX_MAP.get(measured_var, InstrumentType.UNKNOWN)

        # Parse function from suffix
        functions = []
        for letter in function_letters:
            func = DXFConfig.INSTRUMENT_SUFFIX_MAP.get(letter)
            if func:
                functions.append(func)

        function_str = "+".join(functions) if functions else None

        return inst_type, function_str

    @staticmethod
    def parse_line_number(line_number: str) -> dict[str, Any]:
        """
        Parse a line number into components.

        Typical format: size-area-service-sequence-insulation
        Example: 6"-410-P-123-A or 6-410-P-123-A

        Args:
            line_number: Line number string

        Returns:
            Dictionary with parsed components
        """
        result: dict[str, Any] = {
            "nominal_diameter": None,
            "area": None,
            "service_code": None,
            "sequence": None,
            "insulation": None,
            "line_type": LineType.UNKNOWN,
        }

        # Remove quotes and extra spaces
        clean = line_number.strip().replace('"', "").replace("'", "")

        # Split on dash or underscore
        parts = re.split(r"[-_]", clean)

        if len(parts) >= 3:
            result["nominal_diameter"] = parts[0]
            result["area"] = parts[1]
            result["service_code"] = parts[2]

            # Map service code to line type
            service_code = parts[2].upper()
            result["line_type"] = DXFConfig.LINE_SERVICE_MAP.get(
                service_code, LineType.UNKNOWN
            )

        if len(parts) >= 4:
            result["sequence"] = parts[3]

        if len(parts) >= 5:
            result["insulation"] = parts[4]

        return result

    @staticmethod
    def extract_tag_from_text(text: str) -> str | None:
        """
        Extract a tag number from arbitrary text.

        Looks for patterns like:
        - Equipment: V-101, P-201A, TK-301
        - Instruments: FT-101, PIC-202
        - Lines: 6"-410-P-123-A

        Args:
            text: Text string potentially containing a tag

        Returns:
            Extracted tag or None
        """
        text = text.strip().upper()

        # Equipment tag pattern: letters-digits[letter]
        # E.g., V-101, P-201A, TK-301
        equip_pattern = r"\b([A-Z]{1,3}-\d{2,4}[A-Z]?)\b"
        match = re.search(equip_pattern, text)
        if match:
            return match.group(1)

        # Instrument tag pattern: letters-digits
        # E.g., FT-101, PIC-202
        inst_pattern = r"\b([A-Z]{2,4}-\d{2,4})\b"
        match = re.search(inst_pattern, text)
        if match:
            return match.group(1)

        # Line number pattern with quotes
        # E.g., 6"-410-P-123-A
        line_pattern = r'\b(\d+["\']?-\d+-[A-Z]+-\d+(?:-[A-Z])?)\b'
        match = re.search(line_pattern, text)
        if match:
            return match.group(1)

        return None
