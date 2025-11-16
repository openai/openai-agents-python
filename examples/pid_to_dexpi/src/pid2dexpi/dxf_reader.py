"""
DXF file reader for P&ID drawings.

This module uses ezdxf to read DXF files and extract P&ID elements
(equipment, piping, instruments) based on layers, blocks, and attributes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import Insert, LWPolyline, Line, MText, Polyline, Text

from .config import DXFConfig
from .pid_model import (
    Connection,
    Equipment,
    EquipmentType,
    Instrument,
    Nozzle,
    PIDModel,
    PipeRun,
    Point,
)

logger = logging.getLogger(__name__)


class DXFReader:
    """
    DXF file reader for P&ID drawings.

    Extracts equipment, piping, instruments, and connections from DXF files
    using layer names, block references, and text annotations.
    """

    def __init__(self, config: DXFConfig | None = None):
        """
        Initialize the DXF reader.

        Args:
            config: DXF configuration for mappings. If None, uses default.
        """
        self.config = config or DXFConfig()

    def read_dxf(self, dxf_path: str | Path) -> PIDModel:
        """
        Read a DXF file and extract P&ID model.

        Args:
            dxf_path: Path to the DXF file

        Returns:
            PIDModel with extracted elements

        Raises:
            FileNotFoundError: If DXF file doesn't exist
            ezdxf.DXFError: If DXF file is invalid
        """
        dxf_path = Path(dxf_path)
        if not dxf_path.exists():
            raise FileNotFoundError(f"DXF file not found: {dxf_path}")

        logger.info(f"Reading DXF file: {dxf_path}")

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            logger.error(f"Failed to read DXF file: {e}")
            raise

        model = PIDModel(name=dxf_path.stem)
        model.metadata["source_file"] = str(dxf_path)

        # Extract elements from modelspace
        msp = doc.modelspace()

        # First pass: extract equipment, instruments, and text annotations
        text_annotations = self._extract_text_annotations(msp)
        self._extract_equipment(msp, model, text_annotations)
        self._extract_instruments(msp, model, text_annotations)

        # Second pass: extract piping lines
        self._extract_piping(msp, model, text_annotations)

        # Third pass: infer connections based on spatial proximity
        self._infer_connections(model)

        logger.info(f"Extracted P&ID model: {model.summary()}")
        return model

    def _extract_text_annotations(self, msp: Any) -> dict[str, str]:
        """
        Extract text annotations from the drawing.

        Args:
            msp: Model space

        Returns:
            Dictionary mapping position keys to text content
        """
        annotations: dict[str, str] = {}

        for entity in msp.query("TEXT MTEXT"):
            try:
                if isinstance(entity, Text):
                    text = entity.dxf.text
                    pos = entity.dxf.insert
                elif isinstance(entity, MText):
                    text = entity.text
                    pos = entity.dxf.insert
                else:
                    continue

                # Create a position key (rounded to nearest unit)
                pos_key = f"{int(pos.x)}_{int(pos.y)}"
                annotations[pos_key] = text.strip()

            except Exception as e:
                logger.warning(f"Failed to extract text: {e}")
                continue

        logger.debug(f"Extracted {len(annotations)} text annotations")
        return annotations

    def _extract_equipment(
        self, msp: Any, model: PIDModel, text_annotations: dict[str, str]
    ) -> None:
        """
        Extract equipment from block insertions.

        Args:
            msp: Model space
            model: PIDModel to populate
            text_annotations: Text annotations for tag matching
        """
        equipment_count = 0

        for entity in msp.query("INSERT"):
            if not isinstance(entity, Insert):
                continue

            # Check if layer indicates equipment
            layer_category = self.config.match_layer_category(entity.dxf.layer)
            if layer_category != "equipment":
                # Also check block name
                equip_type = self.config.map_block_to_equipment_type(entity.dxf.name)
                if equip_type == EquipmentType.UNKNOWN:
                    continue

            try:
                equipment = self._parse_equipment_insert(entity, text_annotations)
                if equipment:
                    model.add_equipment(equipment)
                    equipment_count += 1
            except Exception as e:
                logger.warning(f"Failed to parse equipment: {e}")
                continue

        logger.debug(f"Extracted {equipment_count} equipment items")

    def _parse_equipment_insert(
        self, insert: Insert, text_annotations: dict[str, str]
    ) -> Equipment | None:
        """
        Parse an INSERT entity as equipment.

        Args:
            insert: DXF INSERT entity
            text_annotations: Text annotations for tag matching

        Returns:
            Equipment object or None
        """
        # Determine equipment type from block name
        equip_type = self.config.map_block_to_equipment_type(insert.dxf.name)

        # Get position
        pos = Point(x=insert.dxf.insert.x, y=insert.dxf.insert.y)

        # Try to find tag number from attributes or nearby text
        tag = None
        properties: dict[str, Any] = {}

        # Check attributes
        if insert.has_attrib:
            for attrib in insert.attribs:
                attr_tag = attrib.dxf.tag.upper()
                attr_value = attrib.dxf.text

                if attr_tag in self.config.EQUIPMENT_ATTRIBUTE_NAMES:
                    if "TAG" in attr_tag and not tag:
                        tag = attr_value
                    properties[attr_tag.lower()] = attr_value

        # If no tag found in attributes, look for nearby text
        if not tag:
            tag = self._find_nearby_tag(pos, text_annotations, prefix_pattern=r"^[A-Z]{1,3}-")

        # Generate a tag if still not found
        if not tag:
            tag = f"EQUIP-{id(insert) % 10000:04d}"

        equipment = Equipment(
            id=tag,
            equipment_type=equip_type,
            position=pos,
            properties=properties,
        )

        # Add description if available
        if "description" in properties:
            equipment.description = properties["description"]
        if "name" in properties:
            equipment.name = properties["name"]

        return equipment

    def _extract_instruments(
        self, msp: Any, model: PIDModel, text_annotations: dict[str, str]
    ) -> None:
        """
        Extract instruments from the drawing.

        Args:
            msp: Model space
            model: PIDModel to populate
            text_annotations: Text annotations for tag matching
        """
        instrument_count = 0

        for entity in msp.query("INSERT"):
            if not isinstance(entity, Insert):
                continue

            # Check if layer indicates instrument
            layer_category = self.config.match_layer_category(entity.dxf.layer)
            if layer_category != "instrument":
                # Also check if block name suggests instrument (contains INST, FT, PT, etc.)
                block_name_upper = entity.dxf.name.upper()
                if not any(
                    prefix in block_name_upper for prefix in ["INST", "FT", "PT", "TT", "LT"]
                ):
                    continue

            try:
                instrument = self._parse_instrument_insert(entity, text_annotations)
                if instrument:
                    model.add_instrument(instrument)
                    instrument_count += 1
            except Exception as e:
                logger.warning(f"Failed to parse instrument: {e}")
                continue

        logger.debug(f"Extracted {instrument_count} instruments")

    def _parse_instrument_insert(
        self, insert: Insert, text_annotations: dict[str, str]
    ) -> Instrument | None:
        """
        Parse an INSERT entity as an instrument.

        Args:
            insert: DXF INSERT entity
            text_annotations: Text annotations for tag matching

        Returns:
            Instrument object or None
        """
        # Get position
        pos = Point(x=insert.dxf.insert.x, y=insert.dxf.insert.y)

        # Try to find tag number
        tag = None
        properties: dict[str, Any] = {}

        # Check attributes
        if insert.has_attrib:
            for attrib in insert.attribs:
                attr_tag = attrib.dxf.tag.upper()
                attr_value = attrib.dxf.text

                if attr_tag in self.config.INSTRUMENT_ATTRIBUTE_NAMES:
                    if "TAG" in attr_tag and not tag:
                        tag = attr_value
                    properties[attr_tag.lower()] = attr_value

        # If no tag found, look for nearby text with instrument pattern
        if not tag:
            tag = self._find_nearby_tag(
                pos, text_annotations, prefix_pattern=r"^[A-Z]{2,4}-\d+"
            )

        # Generate a tag if still not found
        if not tag:
            tag = f"INST-{id(insert) % 10000:04d}"

        # Parse instrument tag to get type and function
        inst_type, function = self.config.parse_instrument_tag(tag)

        instrument = Instrument(
            id=tag,
            instrument_type=inst_type,
            function=function,
            position=pos,
            properties=properties,
        )

        return instrument

    def _extract_piping(
        self, msp: Any, model: PIDModel, text_annotations: dict[str, str]
    ) -> None:
        """
        Extract piping lines from the drawing.

        Args:
            msp: Model space
            model: PIDModel to populate
            text_annotations: Text annotations for tag matching
        """
        pipe_count = 0

        # Extract lines and polylines from pipe layers
        for entity in msp.query("LINE LWPOLYLINE POLYLINE"):
            # Check if layer indicates piping
            layer_category = self.config.match_layer_category(entity.dxf.layer)
            if layer_category not in ["pipe", "process"]:
                continue

            try:
                pipe_run = self._parse_pipe_entity(entity, text_annotations)
                if pipe_run:
                    model.add_pipe_run(pipe_run)
                    pipe_count += 1
            except Exception as e:
                logger.warning(f"Failed to parse pipe: {e}")
                continue

        logger.debug(f"Extracted {pipe_count} pipe runs")

    def _parse_pipe_entity(
        self, entity: Line | LWPolyline | Polyline, text_annotations: dict[str, str]
    ) -> PipeRun | None:
        """
        Parse a line entity as a pipe run.

        Args:
            entity: DXF line or polyline entity
            text_annotations: Text annotations for tag matching

        Returns:
            PipeRun object or None
        """
        # Extract path points
        path_points: list[Point] = []

        if isinstance(entity, Line):
            path_points = [
                Point(x=entity.dxf.start.x, y=entity.dxf.start.y),
                Point(x=entity.dxf.end.x, y=entity.dxf.end.y),
            ]
        elif isinstance(entity, (LWPolyline, Polyline)):
            for point in entity.get_points():
                path_points.append(Point(x=point[0], y=point[1]))

        if len(path_points) < 2:
            return None

        # Find midpoint for tag search
        mid_idx = len(path_points) // 2
        mid_point = path_points[mid_idx]

        # Look for line number nearby
        line_number = self._find_nearby_tag(
            mid_point, text_annotations, prefix_pattern=r'\d+["\']?-\d+-[A-Z]+-\d+'
        )

        # Generate a line number if not found
        if not line_number:
            line_number = f"LINE-{id(entity) % 10000:04d}"

        # Parse line number
        line_info = self.config.parse_line_number(line_number)

        pipe_run = PipeRun(
            id=line_number,
            line_type=line_info["line_type"],
            nominal_diameter=line_info["nominal_diameter"],
            service_code=line_info["service_code"],
            sequence_number=line_info["sequence"],
            insulation_code=line_info["insulation"],
            path_points=path_points,
        )

        return pipe_run

    def _find_nearby_tag(
        self, pos: Point, text_annotations: dict[str, str], prefix_pattern: str, radius: int = 50
    ) -> str | None:
        """
        Find a tag in nearby text annotations.

        Args:
            pos: Position to search around
            text_annotations: Text annotations dict
            prefix_pattern: Regex pattern for tag prefix
            radius: Search radius

        Returns:
            Tag string or None
        """
        # Search in a grid around the position
        for dx in range(-radius, radius + 1, 10):
            for dy in range(-radius, radius + 1, 10):
                pos_key = f"{int(pos.x + dx)}_{int(pos.y + dy)}"
                if pos_key in text_annotations:
                    text = text_annotations[pos_key]
                    # Try to extract tag from text
                    import re

                    match = re.search(prefix_pattern, text.upper())
                    if match:
                        return text.strip()

        return None

    def _infer_connections(self, model: PIDModel) -> None:
        """
        Infer connections between elements based on spatial proximity.

        This is a simplified heuristic approach that connects:
        - Equipment nozzles to nearby pipe endpoints
        - Pipe endpoints to equipment
        - Instruments to nearby pipes

        Args:
            model: PIDModel to update with connections
        """
        connection_count = 0
        proximity_threshold = 10.0  # Units in DXF coordinates

        # Connect pipes to equipment
        for pipe in model.pipe_runs.values():
            if len(pipe.path_points) < 2:
                continue

            start_point = pipe.path_points[0]
            end_point = pipe.path_points[-1]

            # Check start point
            nearest_equip = self._find_nearest_equipment(start_point, model, proximity_threshold)
            if nearest_equip:
                pipe.from_equipment_id = nearest_equip.id
                conn = Connection(
                    from_id=nearest_equip.id,
                    to_id=pipe.id,
                    from_type="equipment",
                    to_type="pipe",
                    connection_type="outlet",
                )
                model.add_connection(conn)
                connection_count += 1

            # Check end point
            nearest_equip = self._find_nearest_equipment(end_point, model, proximity_threshold)
            if nearest_equip:
                pipe.to_equipment_id = nearest_equip.id
                conn = Connection(
                    from_id=pipe.id,
                    to_id=nearest_equip.id,
                    from_type="pipe",
                    to_type="equipment",
                    connection_type="inlet",
                )
                model.add_connection(conn)
                connection_count += 1

        # Connect instruments to pipes
        for instrument in model.instruments.values():
            if not instrument.position:
                continue

            nearest_pipe = self._find_nearest_pipe(
                instrument.position, model, proximity_threshold * 2
            )
            if nearest_pipe:
                instrument.connected_line_id = nearest_pipe.id
                conn = Connection(
                    from_id=instrument.id,
                    to_id=nearest_pipe.id,
                    from_type="instrument",
                    to_type="pipe",
                    connection_type="measurement",
                )
                model.add_connection(conn)
                connection_count += 1

        logger.debug(f"Inferred {connection_count} connections")

    def _find_nearest_equipment(
        self, point: Point, model: PIDModel, threshold: float
    ) -> Equipment | None:
        """Find the nearest equipment to a point within threshold."""
        nearest: Equipment | None = None
        min_distance = threshold

        for equip in model.equipment.values():
            if not equip.position:
                continue

            distance = self._distance(point, equip.position)
            if distance < min_distance:
                min_distance = distance
                nearest = equip

        return nearest

    def _find_nearest_pipe(
        self, point: Point, model: PIDModel, threshold: float
    ) -> PipeRun | None:
        """Find the nearest pipe to a point within threshold."""
        nearest: PipeRun | None = None
        min_distance = threshold

        for pipe in model.pipe_runs.values():
            for path_point in pipe.path_points:
                distance = self._distance(point, path_point)
                if distance < min_distance:
                    min_distance = distance
                    nearest = pipe

        return nearest

    @staticmethod
    def _distance(p1: Point, p2: Point) -> float:
        """Calculate Euclidean distance between two points."""
        return ((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2) ** 0.5
