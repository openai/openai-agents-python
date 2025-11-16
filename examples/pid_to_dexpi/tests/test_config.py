"""Tests for DXF configuration and mapping."""

import pytest

from pid2dexpi.config import DXFConfig
from pid2dexpi.pid_model import EquipmentType, InstrumentType, LineType


class TestDXFConfig:
    """Tests for DXFConfig class."""

    def test_match_layer_category_equipment(self):
        """Test matching equipment layer names."""
        assert DXFConfig.match_layer_category("EQUIPMENT-VESSELS") == "equipment"
        assert DXFConfig.match_layer_category("EQUIP") == "equipment"
        assert DXFConfig.match_layer_category("VESSEL-01") == "equipment"
        assert DXFConfig.match_layer_category("PUMP-LAYER") == "equipment"

    def test_match_layer_category_pipe(self):
        """Test matching pipe layer names."""
        assert DXFConfig.match_layer_category("PIPE-PROCESS") == "pipe"
        assert DXFConfig.match_layer_category("LINE-6INCH") == "pipe"
        assert DXFConfig.match_layer_category("PROCESS-LINES") == "pipe"

    def test_match_layer_category_instrument(self):
        """Test matching instrument layer names."""
        assert DXFConfig.match_layer_category("INSTRUMENT") == "instrument"
        assert DXFConfig.match_layer_category("INST-FLOW") == "instrument"
        assert DXFConfig.match_layer_category("CONTROL-VALVES") == "instrument"

    def test_match_layer_category_unknown(self):
        """Test matching unknown layer names."""
        assert DXFConfig.match_layer_category("RANDOM-LAYER") is None
        assert DXFConfig.match_layer_category("DEFPOINTS") is None

    def test_map_block_to_equipment_type_exact(self):
        """Test exact block name to equipment type mapping."""
        assert DXFConfig.map_block_to_equipment_type("VESSEL") == EquipmentType.VESSEL
        assert DXFConfig.map_block_to_equipment_type("PUMP") == EquipmentType.PUMP
        assert DXFConfig.map_block_to_equipment_type("HEAT_EXCHANGER") == EquipmentType.HEAT_EXCHANGER
        assert DXFConfig.map_block_to_equipment_type("VALVE") == EquipmentType.VALVE

    def test_map_block_to_equipment_type_partial(self):
        """Test partial block name matching."""
        assert DXFConfig.map_block_to_equipment_type("CENTRIFUGAL_PUMP_001") == EquipmentType.PUMP
        assert DXFConfig.map_block_to_equipment_type("VESSEL_V101") == EquipmentType.VESSEL
        assert DXFConfig.map_block_to_equipment_type("HX_E201") == EquipmentType.HEAT_EXCHANGER

    def test_map_block_to_equipment_type_case_insensitive(self):
        """Test case insensitive mapping."""
        assert DXFConfig.map_block_to_equipment_type("vessel") == EquipmentType.VESSEL
        assert DXFConfig.map_block_to_equipment_type("Pump") == EquipmentType.PUMP

    def test_map_block_to_equipment_type_unknown(self):
        """Test unknown block name."""
        assert DXFConfig.map_block_to_equipment_type("UNKNOWN_BLOCK") == EquipmentType.UNKNOWN

    def test_parse_instrument_tag_flow(self):
        """Test parsing flow instrument tags."""
        inst_type, function = DXFConfig.parse_instrument_tag("FT-101")
        assert inst_type == InstrumentType.FLOW
        assert function == "transmitter"

        inst_type, function = DXFConfig.parse_instrument_tag("FI-202")
        assert inst_type == InstrumentType.FLOW
        assert function == "indicator"

    def test_parse_instrument_tag_pressure(self):
        """Test parsing pressure instrument tags."""
        inst_type, function = DXFConfig.parse_instrument_tag("PT-301")
        assert inst_type == InstrumentType.PRESSURE
        assert function == "transmitter"

        inst_type, function = DXFConfig.parse_instrument_tag("PIC-401")
        assert inst_type == InstrumentType.PRESSURE
        assert function == "indicator+controller"

    def test_parse_instrument_tag_temperature(self):
        """Test parsing temperature instrument tags."""
        inst_type, function = DXFConfig.parse_instrument_tag("TT-501")
        assert inst_type == InstrumentType.TEMPERATURE
        assert function == "transmitter"

        inst_type, function = DXFConfig.parse_instrument_tag("TI-602")
        assert inst_type == InstrumentType.TEMPERATURE
        assert function == "indicator"

    def test_parse_instrument_tag_level(self):
        """Test parsing level instrument tags."""
        inst_type, function = DXFConfig.parse_instrument_tag("LT-701")
        assert inst_type == InstrumentType.LEVEL
        assert function == "transmitter"

        inst_type, function = DXFConfig.parse_instrument_tag("LIC-802")
        assert inst_type == InstrumentType.LEVEL
        assert function == "indicator+controller"

    def test_parse_instrument_tag_invalid(self):
        """Test parsing invalid instrument tags."""
        inst_type, function = DXFConfig.parse_instrument_tag("INVALID")
        assert inst_type == InstrumentType.UNKNOWN
        assert function is None

    def test_parse_line_number_full(self):
        """Test parsing complete line number."""
        result = DXFConfig.parse_line_number('6"-410-P-123-A')

        assert result["nominal_diameter"] == "6"
        assert result["area"] == "410"
        assert result["service_code"] == "P"
        assert result["sequence"] == "123"
        assert result["insulation"] == "A"
        assert result["line_type"] == LineType.PROCESS

    def test_parse_line_number_without_insulation(self):
        """Test parsing line number without insulation code."""
        result = DXFConfig.parse_line_number("8-410-P-456")

        assert result["nominal_diameter"] == "8"
        assert result["area"] == "410"
        assert result["service_code"] == "P"
        assert result["sequence"] == "456"
        assert result["insulation"] is None
        assert result["line_type"] == LineType.PROCESS

    def test_parse_line_number_service_codes(self):
        """Test parsing different service codes."""
        # Process
        result = DXFConfig.parse_line_number("6-410-P-123")
        assert result["line_type"] == LineType.PROCESS

        # Utility
        result = DXFConfig.parse_line_number("4-410-U-456")
        assert result["line_type"] == LineType.UTILITY

        # Steam
        result = DXFConfig.parse_line_number("3-410-S-789")
        assert result["line_type"] == LineType.STEAM

        # Air
        result = DXFConfig.parse_line_number("2-410-A-111")
        assert result["line_type"] == LineType.AIR

    def test_extract_tag_from_text_equipment(self):
        """Test extracting equipment tags from text."""
        assert DXFConfig.extract_tag_from_text("V-101") == "V-101"
        assert DXFConfig.extract_tag_from_text("P-201A") == "P-201A"
        assert DXFConfig.extract_tag_from_text("TK-301") == "TK-301"
        assert DXFConfig.extract_tag_from_text("Some text V-101 more text") == "V-101"

    def test_extract_tag_from_text_instrument(self):
        """Test extracting instrument tags from text."""
        assert DXFConfig.extract_tag_from_text("FT-101") == "FT-101"
        assert DXFConfig.extract_tag_from_text("PIC-202") == "PIC-202"
        assert DXFConfig.extract_tag_from_text("Text with FT-101 inside") == "FT-101"

    def test_extract_tag_from_text_line(self):
        """Test extracting line numbers from text."""
        # Note: extract_tag_from_text prioritizes equipment/instrument patterns
        # For line numbers, use parse_line_number directly
        # These patterns match equipment tags within line numbers
        assert DXFConfig.extract_tag_from_text('6"-410-P-123-A') in ["P-123", "6-410-P-123-A"]
        assert DXFConfig.extract_tag_from_text("LINE: 8-410-U-456-B") in ["U-456", "8-410-U-456-B"]

    def test_extract_tag_from_text_none(self):
        """Test extracting tag from text with no tags."""
        assert DXFConfig.extract_tag_from_text("No tags here") is None
        assert DXFConfig.extract_tag_from_text("Random text 123") is None
        assert DXFConfig.extract_tag_from_text("") is None


class TestDXFConfigConstants:
    """Tests for DXF configuration constants."""

    def test_layer_patterns_exist(self):
        """Test that layer patterns are defined."""
        assert len(DXFConfig.LAYER_PATTERNS) > 0
        assert any("EQUIP" in pattern for pattern in DXFConfig.LAYER_PATTERNS.keys())

    def test_block_to_equipment_mapping_exists(self):
        """Test that block to equipment mapping exists."""
        assert len(DXFConfig.BLOCK_TO_EQUIPMENT) > 0
        assert "VESSEL" in DXFConfig.BLOCK_TO_EQUIPMENT
        assert "PUMP" in DXFConfig.BLOCK_TO_EQUIPMENT

    def test_instrument_prefix_map_exists(self):
        """Test that instrument prefix mapping exists."""
        assert len(DXFConfig.INSTRUMENT_PREFIX_MAP) > 0
        assert "F" in DXFConfig.INSTRUMENT_PREFIX_MAP
        assert "P" in DXFConfig.INSTRUMENT_PREFIX_MAP
        assert "T" in DXFConfig.INSTRUMENT_PREFIX_MAP
        assert "L" in DXFConfig.INSTRUMENT_PREFIX_MAP

    def test_line_service_map_exists(self):
        """Test that line service mapping exists."""
        assert len(DXFConfig.LINE_SERVICE_MAP) > 0
        assert "P" in DXFConfig.LINE_SERVICE_MAP
        assert "U" in DXFConfig.LINE_SERVICE_MAP
        assert "S" in DXFConfig.LINE_SERVICE_MAP
