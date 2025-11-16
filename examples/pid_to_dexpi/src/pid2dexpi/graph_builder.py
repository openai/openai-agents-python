"""
Graph builder for P&ID models.

This module builds graph representations of P&ID data using NetworkX.
The graph can be exported to:
- GraphML format (for Gephi, Cytoscape, Neo4j, etc.)
- JSON format with explicit nodes and edges lists

In the graph:
- Nodes represent equipment, instruments, and pipe runs
- Edges represent "connected_to" relationships
- Both nodes and edges carry attributes from the P&ID model
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import networkx as nx

from .pid_model import Equipment, Instrument, PIDModel, PipeRun

logger = logging.getLogger(__name__)


class GraphBuilder:
    """
    Build and export graph representations of P&ID models.

    Uses NetworkX to create directed graphs where:
    - Equipment, instruments, and pipe runs are nodes
    - Connections between elements are edges
    - Process flow direction determines edge direction
    """

    def __init__(self):
        """Initialize the graph builder."""
        self.graph: nx.DiGraph | None = None

    def build_graph(self, model: PIDModel) -> nx.DiGraph:
        """
        Build a directed graph from a P&ID model.

        Args:
            model: PIDModel to convert to graph

        Returns:
            NetworkX directed graph
        """
        logger.info(f"Building graph from P&ID model: {model.name}")

        # Create directed graph
        G = nx.DiGraph()
        G.graph["name"] = model.name
        G.graph["description"] = f"P&ID graph: {model.name}"

        # Add equipment nodes
        for equip in model.equipment.values():
            self._add_equipment_node(G, equip)

        # Add pipe run nodes
        for pipe in model.pipe_runs.values():
            self._add_pipe_node(G, pipe)

        # Add instrument nodes
        for inst in model.instruments.values():
            self._add_instrument_node(G, inst)

        # Add edges from connections
        for conn in model.connections:
            self._add_connection_edge(G, conn)

        logger.info(
            f"Graph built: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges"
        )

        self.graph = G
        return G

    def _add_equipment_node(self, G: nx.DiGraph, equip: Equipment) -> None:
        """
        Add equipment as a node to the graph.

        Args:
            G: NetworkX graph
            equip: Equipment object
        """
        node_attrs = {
            "type": "equipment",
            "equipment_type": equip.equipment_type.value,
            "label": equip.id,
            "tag": equip.id,
        }

        if equip.name:
            node_attrs["name"] = equip.name

        if equip.description:
            node_attrs["description"] = equip.description

        if equip.position:
            node_attrs["x"] = equip.position.x
            node_attrs["y"] = equip.position.y

        # Add custom properties with prefix to avoid conflicts
        for key, value in equip.properties.items():
            node_attrs[f"prop_{key}"] = str(value)

        G.add_node(equip.id, **node_attrs)

    def _add_pipe_node(self, G: nx.DiGraph, pipe: PipeRun) -> None:
        """
        Add pipe run as a node to the graph.

        Args:
            G: NetworkX graph
            pipe: PipeRun object
        """
        node_attrs = {
            "type": "pipe",
            "line_type": pipe.line_type.value,
            "label": pipe.id,
            "tag": pipe.id,
        }

        if pipe.nominal_diameter:
            node_attrs["diameter"] = pipe.nominal_diameter

        if pipe.service_code:
            node_attrs["service"] = pipe.service_code

        if pipe.sequence_number:
            node_attrs["sequence"] = pipe.sequence_number

        if pipe.insulation_code:
            node_attrs["insulation"] = pipe.insulation_code

        # Add path length if path points available
        if len(pipe.path_points) >= 2:
            length = self._calculate_path_length(pipe)
            node_attrs["path_length"] = length

        # Add custom properties
        for key, value in pipe.properties.items():
            node_attrs[f"prop_{key}"] = str(value)

        G.add_node(pipe.id, **node_attrs)

    def _add_instrument_node(self, G: nx.DiGraph, inst: Instrument) -> None:
        """
        Add instrument as a node to the graph.

        Args:
            G: NetworkX graph
            inst: Instrument object
        """
        node_attrs = {
            "type": "instrument",
            "instrument_type": inst.instrument_type.value,
            "label": inst.id,
            "tag": inst.id,
        }

        if inst.function:
            node_attrs["function"] = inst.function

        if inst.position:
            node_attrs["x"] = inst.position.x
            node_attrs["y"] = inst.position.y

        if inst.setpoint:
            node_attrs["setpoint"] = inst.setpoint

        if inst.range_min:
            node_attrs["range_min"] = inst.range_min

        if inst.range_max:
            node_attrs["range_max"] = inst.range_max

        # Add custom properties
        for key, value in inst.properties.items():
            node_attrs[f"prop_{key}"] = str(value)

        G.add_node(inst.id, **node_attrs)

    def _add_connection_edge(self, G: nx.DiGraph, conn: Any) -> None:
        """
        Add connection as an edge to the graph.

        Args:
            G: NetworkX graph
            conn: Connection object
        """
        # Only add edge if both nodes exist
        if conn.from_id not in G or conn.to_id not in G:
            logger.warning(
                f"Skipping connection {conn.from_id} -> {conn.to_id}: "
                f"one or both nodes not found"
            )
            return

        edge_attrs = {
            "from_type": conn.from_type,
            "to_type": conn.to_type,
        }

        if conn.connection_type:
            edge_attrs["connection_type"] = conn.connection_type

        # Add custom properties
        for key, value in conn.properties.items():
            edge_attrs[f"prop_{key}"] = str(value)

        G.add_edge(conn.from_id, conn.to_id, **edge_attrs)

    def export_graphml(self, output_path: str | Path) -> None:
        """
        Export graph to GraphML format.

        GraphML is an XML-based format supported by many graph tools:
        - Gephi (visualization)
        - Cytoscape (network analysis)
        - Neo4j (graph database import)
        - NetworkX (Python)

        Args:
            output_path: Path for output GraphML file

        Raises:
            ValueError: If graph has not been built yet
        """
        if self.graph is None:
            raise ValueError("Graph has not been built. Call build_graph() first.")

        output_path = Path(output_path)
        logger.info(f"Exporting GraphML to: {output_path}")

        nx.write_graphml(self.graph, str(output_path))

        logger.info(f"GraphML export complete: {output_path}")

    def export_json(self, output_path: str | Path) -> None:
        """
        Export graph to JSON format with explicit nodes and edges lists.

        The JSON structure:
        {
            "graph_info": {...},
            "nodes": [
                {"id": "...", "type": "...", ...},
                ...
            ],
            "edges": [
                {"source": "...", "target": "...", ...},
                ...
            ]
        }

        Args:
            output_path: Path for output JSON file

        Raises:
            ValueError: If graph has not been built yet
        """
        if self.graph is None:
            raise ValueError("Graph has not been built. Call build_graph() first.")

        output_path = Path(output_path)
        logger.info(f"Exporting JSON graph to: {output_path}")

        # Build JSON structure
        graph_data = {
            "graph_info": {
                "name": self.graph.graph.get("name", ""),
                "description": self.graph.graph.get("description", ""),
                "node_count": self.graph.number_of_nodes(),
                "edge_count": self.graph.number_of_edges(),
                "directed": True,
            },
            "nodes": [],
            "edges": [],
        }

        # Add nodes
        for node_id, attrs in self.graph.nodes(data=True):
            node_data = {"id": node_id}
            node_data.update(attrs)
            graph_data["nodes"].append(node_data)

        # Add edges
        for source, target, attrs in self.graph.edges(data=True):
            edge_data = {
                "source": source,
                "target": target,
            }
            edge_data.update(attrs)
            graph_data["edges"].append(edge_data)

        # Write to file
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)

        logger.info(f"JSON export complete: {output_path}")

    def export_cypher(self, output_path: str | Path) -> None:
        """
        Export graph as Cypher CREATE statements for Neo4j.

        This generates a .cypher file that can be executed in Neo4j
        to create the P&ID graph in a graph database.

        Args:
            output_path: Path for output Cypher file

        Raises:
            ValueError: If graph has not been built yet
        """
        if self.graph is None:
            raise ValueError("Graph has not been built. Call build_graph() first.")

        output_path = Path(output_path)
        logger.info(f"Exporting Cypher statements to: {output_path}")

        with open(output_path, "w", encoding="utf-8") as f:
            # Write header comment
            f.write("// P&ID Graph - Neo4j Cypher Import\n")
            f.write(f"// Generated from: {self.graph.graph.get('name', 'unknown')}\n")
            f.write("//\n\n")

            # Create nodes
            f.write("// Create nodes\n")
            for node_id, attrs in self.graph.nodes(data=True):
                node_type = attrs.get("type", "Node").capitalize()
                props = self._attrs_to_cypher_props(attrs, exclude=["type"])
                f.write(f'CREATE (n:{node_type} {{id: "{node_id}", {props}}})\n')

            f.write("\n")

            # Create relationships
            f.write("// Create relationships\n")
            for source, target, attrs in self.graph.edges(data=True):
                rel_type = attrs.get("connection_type", "CONNECTED_TO").upper().replace(" ", "_")
                props = self._attrs_to_cypher_props(attrs, exclude=["connection_type"])

                f.write(
                    f'MATCH (a {{id: "{source}"}}), (b {{id: "{target}"}})\n'
                    f"CREATE (a)-[r:{rel_type} {{{props}}}]->(b)\n"
                )

        logger.info(f"Cypher export complete: {output_path}")

    @staticmethod
    def _attrs_to_cypher_props(attrs: dict[str, Any], exclude: list[str] | None = None) -> str:
        """
        Convert attributes dictionary to Cypher properties string.

        Args:
            attrs: Attributes dictionary
            exclude: Keys to exclude

        Returns:
            Cypher properties string (e.g., "key1: 'value1', key2: 'value2'")
        """
        exclude = exclude or []
        props = []

        for key, value in attrs.items():
            if key in exclude or key == "id":
                continue

            # Escape strings
            if isinstance(value, str):
                value_str = f'"{value}"'
            elif isinstance(value, bool):
                value_str = str(value).lower()
            elif isinstance(value, (int, float)):
                value_str = str(value)
            else:
                value_str = f'"{str(value)}"'

            props.append(f"{key}: {value_str}")

        return ", ".join(props)

    @staticmethod
    def _calculate_path_length(pipe: PipeRun) -> float:
        """
        Calculate the total path length of a pipe run.

        Args:
            pipe: PipeRun object

        Returns:
            Total path length
        """
        if len(pipe.path_points) < 2:
            return 0.0

        total_length = 0.0
        for i in range(len(pipe.path_points) - 1):
            p1 = pipe.path_points[i]
            p2 = pipe.path_points[i + 1]
            distance = ((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2) ** 0.5
            total_length += distance

        return total_length

    def get_statistics(self) -> dict[str, Any]:
        """
        Get graph statistics.

        Returns:
            Dictionary with graph statistics

        Raises:
            ValueError: If graph has not been built yet
        """
        if self.graph is None:
            raise ValueError("Graph has not been built. Call build_graph() first.")

        # Count nodes by type
        node_types: dict[str, int] = {}
        for _, attrs in self.graph.nodes(data=True):
            node_type = attrs.get("type", "unknown")
            node_types[node_type] = node_types.get(node_type, 0) + 1

        # Count edges by type
        edge_types: dict[str, int] = {}
        for _, _, attrs in self.graph.edges(data=True):
            edge_type = attrs.get("connection_type", "unknown")
            edge_types[edge_type] = edge_types.get(edge_type, 0) + 1

        # Calculate basic graph metrics
        stats = {
            "node_count": self.graph.number_of_nodes(),
            "edge_count": self.graph.number_of_edges(),
            "node_types": node_types,
            "edge_types": edge_types,
            "density": nx.density(self.graph),
            "is_connected": nx.is_weakly_connected(self.graph),
        }

        # Add degree statistics
        if self.graph.number_of_nodes() > 0:
            degrees = [d for _, d in self.graph.degree()]
            stats["avg_degree"] = sum(degrees) / len(degrees)
            stats["max_degree"] = max(degrees)
            stats["min_degree"] = min(degrees)

        return stats
