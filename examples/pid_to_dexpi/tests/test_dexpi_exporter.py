"""Tests for DEXPI XML exporter."""

import tempfile
from pathlib import Path

import pytest
from lxml import etree

from pid2dexpi.dexpi_exporter import DEXPIExporter
from pid2dexpi.pid_model import (
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


class TestDEXPIExporter:
    """Tests for DEXPIExporter class."""

    @pytest.fixture
    def sample_model(self) -> PIDModel:
        """Create a sample P&ID model for testing."""
        model = PIDModel(name="Test P&ID")

        # Add equipment
        vessel = Equipment(
            id="V-101",
            equipment_type=EquipmentType.VESSEL,
            name="Test Vessel",
            position=Point(100, 200),
        )
        nozzle = Nozzle(
            id="N-101",
            equipment_id="V-101",
            position=Point(110, 200),
            direction="outlet",
            nominal_diameter='6"',
        )
        vessel.add_nozzle(nozzle)
        model.add_equipment(vessel)

        # Add pump
        pump = Equipment(
            id="P-201A",
            equipment_type=EquipmentType.PUMP,
            name="Feed Pump",
            position=Point(200, 200),
        )
        model.add_equipment(pump)

        # Add pipe run
        pipe = PipeRun(
            id='6"-410-P-123-A',
            line_type=LineType.PROCESS,
            nominal_diameter='6"',
            service_code="P",
            sequence_number="123",
            path_points=[Point(110, 200), Point(150, 200), Point(190, 200)],
        )
        pipe.from_equipment_id = "V-101"
        pipe.to_equipment_id = "P-201A"
        model.add_pipe_run(pipe)

        # Add instrument
        inst = Instrument(
            id="FT-101",
            instrument_type=InstrumentType.FLOW,
            function="transmitter",
            position=Point(150, 220),
            connected_line_id='6"-410-P-123-A',
        )
        model.add_instrument(inst)

        # Add connections
        conn1 = Connection(
            from_id="V-101",
            to_id='6"-410-P-123-A',
            from_type="equipment",
            to_type="pipe",
            connection_type="outlet",
        )
        conn2 = Connection(
            from_id='6"-410-P-123-A',
            to_id="P-201A",
            from_type="pipe",
            to_type="equipment",
            connection_type="inlet",
        )
        model.add_connection(conn1)
        model.add_connection(conn2)

        return model

    def test_export_creates_file(self, sample_model):
        """Test that export creates an XML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            assert output_path.exists()
            assert output_path.stat().st_size > 0

    def test_export_valid_xml(self, sample_model):
        """Test that exported XML is valid and well-formed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            # Parse the XML to verify it's well-formed
            tree = etree.parse(str(output_path))
            root = tree.getroot()

            assert root is not None
            assert "PlantModel" in root.tag

    def test_export_contains_equipment(self, sample_model):
        """Test that exported XML contains equipment elements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            tree = etree.parse(str(output_path))
            root = tree.getroot()

            # Check for vessel
            vessels = root.xpath(
                "//*[local-name()='Vessel'][@TagName='V-101']",
            )
            assert len(vessels) == 1

            # Check for pump
            pumps = root.xpath(
                "//*[local-name()='Pump'][@TagName='P-201A']",
            )
            assert len(pumps) == 1

    def test_export_contains_piping(self, sample_model):
        """Test that exported XML contains piping elements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            tree = etree.parse(str(output_path))
            root = tree.getroot()

            # Check for pipe run
            pipes = root.xpath(
                "//*[local-name()='PipingNetworkSegment']",
            )
            assert len(pipes) == 1

    def test_export_contains_instruments(self, sample_model):
        """Test that exported XML contains instrument elements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            tree = etree.parse(str(output_path))
            root = tree.getroot()

            # Check for instrument
            instruments = root.xpath(
                "//*[local-name()='ProcessInstrument'][@TagName='FT-101']",
            )
            assert len(instruments) == 1

    def test_export_contains_metadata(self, sample_model):
        """Test that exported XML contains metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_output.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(sample_model, output_path)

            tree = etree.parse(str(output_path))
            root = tree.getroot()

            # Check for metadata
            metadata = root.xpath("//*[local-name()='Metadata']")
            assert len(metadata) == 1

            # Check for creation date
            creation_dates = root.xpath("//*[local-name()='CreationDate']")
            assert len(creation_dates) == 1

    def test_sanitize_id(self):
        """Test ID sanitization."""
        exporter = DEXPIExporter()

        # Test with quotes
        assert exporter._sanitize_id('6"-410-P-123-A') == "ID_6-410-P-123-A"

        # Test with spaces
        assert exporter._sanitize_id("V 101") == "V_101"

        # Test already valid
        assert exporter._sanitize_id("V-101") == "V-101"

    def test_generate_id(self):
        """Test ID generation."""
        exporter = DEXPIExporter()

        id1 = exporter._generate_id("TEST")
        id2 = exporter._generate_id("TEST")

        assert id1.startswith("TEST_")
        assert id2.startswith("TEST_")
        assert id1 != id2  # Should be unique


class TestDEXPIExporterEdgeCases:
    """Tests for edge cases in DEXPI exporter."""

    def test_export_empty_model(self):
        """Test exporting an empty model."""
        model = PIDModel(name="Empty Model")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "empty.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(model, output_path)

            assert output_path.exists()

            # Verify basic structure exists
            tree = etree.parse(str(output_path))
            root = tree.getroot()
            assert "PlantModel" in root.tag

    def test_export_equipment_without_position(self):
        """Test exporting equipment without position."""
        model = PIDModel(name="Test")
        equip = Equipment(id="V-101", equipment_type=EquipmentType.VESSEL)
        model.add_equipment(equip)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test.dexpi.xml"
            exporter = DEXPIExporter()

            exporter.export(model, output_path)

            assert output_path.exists()
