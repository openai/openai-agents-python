"""Tests for P&ID data model."""

import pytest

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


class TestPoint:
    """Tests for Point class."""

    def test_point_creation(self):
        """Test creating a point."""
        point = Point(x=10.0, y=20.0)
        assert point.x == 10.0
        assert point.y == 20.0

    def test_point_to_tuple(self):
        """Test converting point to tuple."""
        point = Point(x=10.0, y=20.0)
        assert point.to_tuple() == (10.0, 20.0)


class TestNozzle:
    """Tests for Nozzle class."""

    def test_nozzle_creation(self):
        """Test creating a nozzle."""
        nozzle = Nozzle(
            id="N-101",
            equipment_id="V-101",
            position=Point(x=10.0, y=20.0),
            direction="inlet",
            nominal_diameter='6"',
        )
        assert nozzle.id == "N-101"
        assert nozzle.equipment_id == "V-101"
        assert nozzle.direction == "inlet"
        assert nozzle.nominal_diameter == '6"'

    def test_nozzle_hashable(self):
        """Test that nozzles are hashable."""
        nozzle = Nozzle(id="N-101", equipment_id="V-101", position=Point(0, 0))
        nozzle_dict = {nozzle: "test"}
        assert nozzle in nozzle_dict


class TestEquipment:
    """Tests for Equipment class."""

    def test_equipment_creation(self):
        """Test creating equipment."""
        equip = Equipment(
            id="V-101",
            equipment_type=EquipmentType.VESSEL,
            name="Test Vessel",
            position=Point(x=100.0, y=200.0),
        )
        assert equip.id == "V-101"
        assert equip.equipment_type == EquipmentType.VESSEL
        assert equip.name == "Test Vessel"
        assert equip.position.x == 100.0

    def test_equipment_add_nozzle(self):
        """Test adding nozzles to equipment."""
        equip = Equipment(id="V-101", equipment_type=EquipmentType.VESSEL)
        nozzle = Nozzle(id="N-101", equipment_id="V-101", position=Point(0, 0))

        equip.add_nozzle(nozzle)
        assert len(equip.nozzles) == 1
        assert equip.nozzles[0] == nozzle

    def test_equipment_hashable(self):
        """Test that equipment is hashable."""
        equip = Equipment(id="V-101", equipment_type=EquipmentType.VESSEL)
        equip_dict = {equip: "test"}
        assert equip in equip_dict


class TestPipeRun:
    """Tests for PipeRun class."""

    def test_pipe_run_creation(self):
        """Test creating a pipe run."""
        pipe = PipeRun(
            id='6"-410-P-123-A',
            line_type=LineType.PROCESS,
            nominal_diameter='6"',
            service_code="P",
            sequence_number="123",
            insulation_code="A",
        )
        assert pipe.id == '6"-410-P-123-A'
        assert pipe.line_type == LineType.PROCESS
        assert pipe.nominal_diameter == '6"'
        assert pipe.service_code == "P"

    def test_pipe_run_with_path(self):
        """Test pipe run with path points."""
        pipe = PipeRun(
            id="LINE-001",
            line_type=LineType.PROCESS,
            path_points=[Point(0, 0), Point(10, 0), Point(10, 10)],
        )
        assert len(pipe.path_points) == 3
        assert pipe.path_points[0].x == 0
        assert pipe.path_points[2].y == 10


class TestInstrument:
    """Tests for Instrument class."""

    def test_instrument_creation(self):
        """Test creating an instrument."""
        inst = Instrument(
            id="FT-101",
            instrument_type=InstrumentType.FLOW,
            function="transmitter",
            position=Point(x=50.0, y=75.0),
        )
        assert inst.id == "FT-101"
        assert inst.instrument_type == InstrumentType.FLOW
        assert inst.function == "transmitter"

    def test_instrument_with_range(self):
        """Test instrument with range and setpoint."""
        inst = Instrument(
            id="PIC-202",
            instrument_type=InstrumentType.PRESSURE,
            function="indicator+controller",
            range_min="0",
            range_max="100",
            setpoint="50",
        )
        assert inst.range_min == "0"
        assert inst.range_max == "100"
        assert inst.setpoint == "50"


class TestConnection:
    """Tests for Connection class."""

    def test_connection_creation(self):
        """Test creating a connection."""
        conn = Connection(
            from_id="V-101",
            to_id='6"-410-P-123-A',
            from_type="equipment",
            to_type="pipe",
            connection_type="outlet",
        )
        assert conn.from_id == "V-101"
        assert conn.to_id == '6"-410-P-123-A'
        assert conn.connection_type == "outlet"

    def test_connection_hashable(self):
        """Test that connections are hashable."""
        conn = Connection(from_id="A", to_id="B", from_type="equipment", to_type="pipe")
        conn_dict = {conn: "test"}
        assert conn in conn_dict


class TestPIDModel:
    """Tests for PIDModel class."""

    def test_pid_model_creation(self):
        """Test creating a PID model."""
        model = PIDModel(name="Test P&ID")
        assert model.name == "Test P&ID"
        assert len(model.equipment) == 0
        assert len(model.pipe_runs) == 0
        assert len(model.instruments) == 0

    def test_add_equipment(self):
        """Test adding equipment to model."""
        model = PIDModel(name="Test")
        equip = Equipment(id="V-101", equipment_type=EquipmentType.VESSEL)

        model.add_equipment(equip)
        assert "V-101" in model.equipment
        assert model.equipment["V-101"] == equip

    def test_add_equipment_with_nozzles(self):
        """Test adding equipment with nozzles."""
        model = PIDModel(name="Test")
        equip = Equipment(id="V-101", equipment_type=EquipmentType.VESSEL)
        nozzle = Nozzle(id="N-101", equipment_id="V-101", position=Point(0, 0))
        equip.add_nozzle(nozzle)

        model.add_equipment(equip)
        assert "N-101" in model.nozzles
        assert model.nozzles["N-101"] == nozzle

    def test_add_pipe_run(self):
        """Test adding pipe run to model."""
        model = PIDModel(name="Test")
        pipe = PipeRun(id="LINE-001", line_type=LineType.PROCESS)

        model.add_pipe_run(pipe)
        assert "LINE-001" in model.pipe_runs

    def test_add_instrument(self):
        """Test adding instrument to model."""
        model = PIDModel(name="Test")
        inst = Instrument(id="FT-101", instrument_type=InstrumentType.FLOW)

        model.add_instrument(inst)
        assert "FT-101" in model.instruments

    def test_add_connection(self):
        """Test adding connection to model."""
        model = PIDModel(name="Test")
        conn = Connection(from_id="A", to_id="B", from_type="equipment", to_type="pipe")

        model.add_connection(conn)
        assert len(model.connections) == 1

    def test_get_all_elements(self):
        """Test getting all elements from model."""
        model = PIDModel(name="Test")
        model.add_equipment(Equipment(id="V-101", equipment_type=EquipmentType.VESSEL))
        model.add_pipe_run(PipeRun(id="LINE-001", line_type=LineType.PROCESS))
        model.add_instrument(Instrument(id="FT-101", instrument_type=InstrumentType.FLOW))

        elements = model.get_all_elements()
        assert len(elements) == 3

    def test_summary(self):
        """Test model summary."""
        model = PIDModel(name="Test P&ID")
        model.add_equipment(Equipment(id="V-101", equipment_type=EquipmentType.VESSEL))
        model.add_pipe_run(PipeRun(id="LINE-001", line_type=LineType.PROCESS))

        summary = model.summary()
        assert "Test P&ID" in summary
        assert "Equipment: 1" in summary
        assert "Pipe Runs: 1" in summary
