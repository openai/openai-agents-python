"""
Command-line interface for pid2dexpi.

This module provides a CLI for converting P&ID DXF files to DEXPI XML
and graph representations.

Usage:
    pid2dexpi --input drawing.dxf --output-base output/drawing
    pid2dexpi -i drawing.dxf -o output/drawing --format all
    pid2dexpi -i drawing.dxf -o output/drawing --format dexpi
    pid2dexpi -i drawing.dxf -o output/drawing --format graph --graph-formats graphml json
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from .config import DXFConfig
from .dexpi_exporter import DEXPIExporter
from .dxf_reader import DXFReader
from .graph_builder import GraphBuilder

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    Args:
        args: Command-line arguments (None to use sys.argv)

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="Convert P&ID DXF drawings to DEXPI XML and graph representations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Convert to all formats (DEXPI XML + GraphML + JSON)
  pid2dexpi -i drawing.dxf -o output/drawing

  # Convert to DEXPI XML only
  pid2dexpi -i drawing.dxf -o output/drawing --format dexpi

  # Convert to graph formats only (GraphML and JSON)
  pid2dexpi -i drawing.dxf -o output/drawing --format graph

  # Convert to specific graph formats
  pid2dexpi -i drawing.dxf -o output/drawing --format graph --graph-formats graphml cypher

  # Verbose logging
  pid2dexpi -i drawing.dxf -o output/drawing -v
        """,
    )

    parser.add_argument(
        "-i",
        "--input",
        required=True,
        type=str,
        help="Input DXF file path",
    )

    parser.add_argument(
        "-o",
        "--output-base",
        required=True,
        type=str,
        help="Output base path (without extension). Output files will be named "
        "<output-base>.dexpi.xml, <output-base>.graphml, etc.",
    )

    parser.add_argument(
        "--format",
        choices=["all", "dexpi", "graph"],
        default="all",
        help="Output format: 'all' (DEXPI + graph), 'dexpi' only, or 'graph' only (default: all)",
    )

    parser.add_argument(
        "--graph-formats",
        nargs="+",
        choices=["graphml", "json", "cypher"],
        default=["graphml", "json"],
        help="Graph export formats (default: graphml json)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print graph statistics after processing",
    )

    return parser.parse_args(args)


def main(args: Sequence[str] | None = None) -> int:
    """
    Main entry point for the CLI.

    Args:
        args: Command-line arguments (None to use sys.argv)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parsed_args = parse_args(args)

    # Configure logging level
    if parsed_args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate input file
    input_path = Path(parsed_args.input)
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return 1

    if not input_path.suffix.lower() in [".dxf"]:
        logger.error(f"Input file must be a DXF file (got: {input_path.suffix})")
        return 1

    # Create output directory if needed
    output_base = Path(parsed_args.output_base)
    output_base.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 80)
    logger.info("P&ID to DEXPI Converter")
    logger.info("=" * 80)
    logger.info(f"Input:  {input_path}")
    logger.info(f"Output: {output_base}.*")
    logger.info(f"Format: {parsed_args.format}")
    logger.info("=" * 80)

    try:
        # Step 1: Read DXF file
        logger.info("\n[1/3] Reading DXF file...")
        config = DXFConfig()
        reader = DXFReader(config=config)
        model = reader.read_dxf(input_path)

        logger.info(f"\n{model.summary()}")

        # Step 2: Export to DEXPI XML (if requested)
        if parsed_args.format in ["all", "dexpi"]:
            logger.info("\n[2/3] Exporting to DEXPI XML...")
            dexpi_output = output_base.parent / f"{output_base.name}.dexpi.xml"
            exporter = DEXPIExporter()
            exporter.export(model, dexpi_output)
            logger.info(f"✓ DEXPI XML saved to: {dexpi_output}")

        # Step 3: Export to graph formats (if requested)
        if parsed_args.format in ["all", "graph"]:
            logger.info("\n[3/3] Building and exporting graph...")
            builder = GraphBuilder()
            graph = builder.build_graph(model)

            # Export to requested formats
            for graph_format in parsed_args.graph_formats:
                if graph_format == "graphml":
                    graphml_output = output_base.parent / f"{output_base.name}.graphml"
                    builder.export_graphml(graphml_output)
                    logger.info(f"✓ GraphML saved to: {graphml_output}")

                elif graph_format == "json":
                    json_output = output_base.parent / f"{output_base.name}.graph.json"
                    builder.export_json(json_output)
                    logger.info(f"✓ JSON graph saved to: {json_output}")

                elif graph_format == "cypher":
                    cypher_output = output_base.parent / f"{output_base.name}.cypher"
                    builder.export_cypher(cypher_output)
                    logger.info(f"✓ Cypher statements saved to: {cypher_output}")

            # Print statistics if requested
            if parsed_args.stats:
                logger.info("\nGraph Statistics:")
                logger.info("-" * 80)
                stats = builder.get_statistics()
                for key, value in stats.items():
                    if isinstance(value, dict):
                        logger.info(f"  {key}:")
                        for sub_key, sub_value in value.items():
                            logger.info(f"    {sub_key}: {sub_value}")
                    elif isinstance(value, float):
                        logger.info(f"  {key}: {value:.4f}")
                    else:
                        logger.info(f"  {key}: {value}")
                logger.info("-" * 80)

        logger.info("\n" + "=" * 80)
        logger.info("✓ Conversion complete!")
        logger.info("=" * 80)

        return 0

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        return 1

    except Exception as e:
        logger.error(f"Error during conversion: {e}", exc_info=parsed_args.verbose)
        return 1


if __name__ == "__main__":
    sys.exit(main())
