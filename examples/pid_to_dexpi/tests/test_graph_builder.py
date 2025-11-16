"""Tests for graph builder."""

import json
import tempfile
from pathlib import Path

import pytest

from pid2dexpi.graph_builder import GraphBuilder
from pid2dexpi.pid_model import (
    Connection,
    Equipment,
    EquipmentType,
    Instrument,
    InstrumentType,
    LineType,
    PIDModel,
    PipeRun,
    Point,
)


class TestGraphBuilder:
    """Tests for GraphBuilder class."""

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
        model.add_equipment(vessel)

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
            path_points=[Point(110, 200), Point(150, 200), Point(190, 200)],
        )
        model.add_pipe_run(pipe)

        # Add instrument
        inst = Instrument(
            id="FT-101",
            instrument_type=InstrumentType.FLOW,
            function="transmitter",
            position=Point(150, 220),
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
        conn3 = Connection(
            from_id="FT-101",
            to_id='6"-410-P-123-A',
            from_type="instrument",
            to_type="pipe",
            connection_type="measurement",
        )
        model.add_connection(conn1)
        model.add_connection(conn2)
        model.add_connection(conn3)

        return model

    def test_build_graph(self, sample_model):
        """Test building a graph from model."""
        builder = GraphBuilder()
        graph = builder.build_graph(sample_model)

        assert graph is not None
        assert graph.number_of_nodes() == 4  # 2 equipment + 1 pipe + 1 instrument
        assert graph.number_of_edges() == 3  # 3 connections

    def test_graph_nodes_have_correct_types(self, sample_model):
        """Test that graph nodes have correct type attributes."""
        builder = GraphBuilder()
        graph = builder.build_graph(sample_model)

        # Check vessel node
        vessel_attrs = graph.nodes["V-101"]
        assert vessel_attrs["type"] == "equipment"
        assert vessel_attrs["equipment_type"] == "vessel"
        assert vessel_attrs["tag"] == "V-101"

        # Check pipe node
        pipe_attrs = graph.nodes['6"-410-P-123-A']
        assert pipe_attrs["type"] == "pipe"
        assert pipe_attrs["line_type"] == "process"

        # Check instrument node
        inst_attrs = graph.nodes["FT-101"]
        assert inst_attrs["type"] == "instrument"
        assert inst_attrs["instrument_type"] == "flow"

    def test_graph_edges_have_attributes(self, sample_model):
        """Test that graph edges have attributes."""
        builder = GraphBuilder()
        graph = builder.build_graph(sample_model)

        # Check edge from vessel to pipe
        edge_attrs = graph.edges["V-101", '6"-410-P-123-A']
        assert edge_attrs["from_type"] == "equipment"
        assert edge_attrs["to_type"] == "pipe"
        assert edge_attrs["connection_type"] == "outlet"

    def test_export_graphml(self, sample_model):
        """Test exporting to GraphML format."""
        builder = GraphBuilder()
        builder.build_graph(sample_model)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test.graphml"
            builder.export_graphml(output_path)

            assert output_path.exists()
            assert output_path.stat().st_size > 0

            # Verify it's valid XML
            with open(output_path) as f:
                content = f.read()
                assert "graphml" in content.lower()
                assert "V-101" in content
                assert "FT-101" in content

    def test_export_json(self, sample_model):
        """Test exporting to JSON format."""
        builder = GraphBuilder()
        builder.build_graph(sample_model)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test.graph.json"
            builder.export_json(output_path)

            assert output_path.exists()

            # Load and verify JSON structure
            with open(output_path) as f:
                data = json.load(f)

            assert "graph_info" in data
            assert "nodes" in data
            assert "edges" in data

            assert data["graph_info"]["node_count"] == 4
            assert data["graph_info"]["edge_count"] == 3
            assert len(data["nodes"]) == 4
            assert len(data["edges"]) == 3

            # Check node structure
            node_ids = {node["id"] for node in data["nodes"]}
            assert "V-101" in node_ids
            assert "P-201A" in node_ids
            assert '6"-410-P-123-A' in node_ids
            assert "FT-101" in node_ids

    def test_export_cypher(self, sample_model):
        """Test exporting to Cypher format."""
        builder = GraphBuilder()
        builder.build_graph(sample_model)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test.cypher"
            builder.export_cypher(output_path)

            assert output_path.exists()

            # Verify content
            with open(output_path) as f:
                content = f.read()

            assert "CREATE" in content
            assert "MATCH" in content
            assert "V-101" in content
            assert "FT-101" in content

    def test_get_statistics(self, sample_model):
        """Test getting graph statistics."""
        builder = GraphBuilder()
        builder.build_graph(sample_model)

        stats = builder.get_statistics()

        assert stats["node_count"] == 4
        assert stats["edge_count"] == 3
        assert "node_types" in stats
        assert stats["node_types"]["equipment"] == 2
        assert stats["node_types"]["pipe"] == 1
        assert stats["node_types"]["instrument"] == 1
        assert "density" in stats
        assert "avg_degree" in stats

    def test_build_graph_without_calling_raises_error(self):
        """Test that export methods raise error if graph not built."""
        builder = GraphBuilder()

        with pytest.raises(ValueError, match="Graph has not been built"):
            with tempfile.TemporaryDirectory() as tmpdir:
                builder.export_graphml(Path(tmpdir) / "test.graphml")

        with pytest.raises(ValueError, match="Graph has not been built"):
            with tempfile.TemporaryDirectory() as tmpdir:
                builder.export_json(Path(tmpdir) / "test.json")

        with pytest.raises(ValueError, match="Graph has not been built"):
            builder.get_statistics()


class TestGraphBuilderEdgeCases:
    """Tests for edge cases in graph builder."""

    def test_build_graph_from_empty_model(self):
        """Test building graph from empty model."""
        model = PIDModel(name="Empty")
        builder = GraphBuilder()

        graph = builder.build_graph(model)

        assert graph.number_of_nodes() == 0
        assert graph.number_of_edges() == 0

    def test_build_graph_with_disconnected_elements(self):
        """Test building graph with disconnected elements."""
        model = PIDModel(name="Disconnected")

        # Add equipment without connections
        model.add_equipment(Equipment(id="V-101", equipment_type=EquipmentType.VESSEL))
        model.add_equipment(Equipment(id="P-201A", equipment_type=EquipmentType.PUMP))

        builder = GraphBuilder()
        graph = builder.build_graph(model)

        assert graph.number_of_nodes() == 2
        assert graph.number_of_edges() == 0

    def test_calculate_path_length(self):
        """Test path length calculation."""
        pipe = PipeRun(
            id="LINE-001",
            line_type=LineType.PROCESS,
            path_points=[Point(0, 0), Point(10, 0), Point(10, 10)],
        )

        length = GraphBuilder._calculate_path_length(pipe)

        # Length should be 10 + 10 = 20
        assert length == pytest.approx(20.0, rel=0.01)

    def test_calculate_path_length_empty(self):
        """Test path length calculation with no points."""
        pipe = PipeRun(id="LINE-001", line_type=LineType.PROCESS, path_points=[])

        length = GraphBuilder._calculate_path_length(pipe)

        assert length == 0.0
