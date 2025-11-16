# pid2dexpi: P&ID to DEXPI and Graph Converter

A complete Python application for converting refinery P&ID (Piping and Instrumentation Diagram) drawings from DXF format to:

1. **DEXPI XML** - ISO 15926-based standard for process engineering data exchange
2. **Graph representations** - GraphML, JSON, and Cypher formats for graph databases and visualization

## Features

- **DXF Parsing**: Extract equipment, piping, instruments, and connections from AutoCAD/CAD DXF files
- **DEXPI Export**: Generate standards-compliant DEXPI XML suitable for import into process engineering software
- **Graph Export**: Create graph representations for:
  - GraphML (Gephi, Cytoscape, Neo4j)
  - JSON (custom applications, web visualization)
  - Cypher (Neo4j graph database)
- **Configurable Mappings**: Customize layer names, block types, and tag patterns
- **Extensible Architecture**: Easy to add new equipment types, export formats, or parsing rules

## Project Structure

```
pid_to_dexpi/
├── src/
│   └── pid2dexpi/
│       ├── __init__.py           # Package initialization
│       ├── cli.py                # Command-line interface
│       ├── config.py             # DXF mapping configuration
│       ├── dexpi_exporter.py     # DEXPI XML export
│       ├── dxf_reader.py         # DXF file reader
│       ├── graph_builder.py      # Graph builder and export
│       └── pid_model.py          # Internal P&ID data model
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_dexpi_exporter.py
│   ├── test_graph_builder.py
│   └── test_pid_model.py
├── pyproject.toml                # Project configuration
├── requirements.txt              # Dependencies
└── README.md                     # This file
```

## Installation

### Prerequisites

- Python 3.11 or higher
- pip or uv package manager

### Steps

1. **Clone or navigate to the project directory**:
   ```bash
   cd examples/pid_to_dexpi
   ```

2. **Create a virtual environment** (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Install the package in development mode**:
   ```bash
   pip install -e .
   ```

## Usage

### Command-Line Interface

The main entry point is the `pid2dexpi` command-line tool.

#### Basic Usage

Convert a DXF file to all formats (DEXPI XML + GraphML + JSON):

```bash
pid2dexpi --input drawing.dxf --output-base output/drawing
```

This creates:
- `output/drawing.dexpi.xml` - DEXPI XML file
- `output/drawing.graphml` - GraphML graph file
- `output/drawing.graph.json` - JSON graph file

#### Export Only DEXPI XML

```bash
pid2dexpi -i drawing.dxf -o output/drawing --format dexpi
```

#### Export Only Graph Formats

```bash
pid2dexpi -i drawing.dxf -o output/drawing --format graph
```

#### Export Specific Graph Formats

```bash
pid2dexpi -i drawing.dxf -o output/drawing --format graph --graph-formats graphml cypher
```

Available graph formats:
- `graphml` - GraphML XML format
- `json` - JSON with nodes/edges lists
- `cypher` - Neo4j Cypher CREATE statements

#### Verbose Logging and Statistics

```bash
pid2dexpi -i drawing.dxf -o output/drawing -v --stats
```

### Python API

You can also use the package programmatically:

```python
from pid2dexpi import DXFReader, DEXPIExporter, GraphBuilder

# Read DXF file
reader = DXFReader()
model = reader.read_dxf("drawing.dxf")

# Print summary
print(model.summary())

# Export to DEXPI XML
exporter = DEXPIExporter()
exporter.export(model, "output/drawing.dexpi.xml")

# Build and export graph
builder = GraphBuilder()
graph = builder.build_graph(model)
builder.export_graphml("output/drawing.graphml")
builder.export_json("output/drawing.graph.json")
builder.export_cypher("output/drawing.cypher")

# Get statistics
stats = builder.get_statistics()
print(f"Nodes: {stats['node_count']}, Edges: {stats['edge_count']}")
```

## Configuration

### DXF Layer Mapping

The tool uses layer names and block names to identify P&ID elements. You can customize these mappings in `src/pid2dexpi/config.py`:

```python
LAYER_PATTERNS = {
    r"^EQUIP.*": "equipment",
    r"^PIPE.*": "pipe",
    r"^INST.*": "instrument",
    # Add your custom layer patterns here
}
```

### Equipment Type Mapping

Block names are mapped to equipment types:

```python
BLOCK_TO_EQUIPMENT = {
    "VESSEL": EquipmentType.VESSEL,
    "PUMP": EquipmentType.PUMP,
    "HEAT_EXCHANGER": EquipmentType.HEAT_EXCHANGER,
    # Add your custom block mappings here
}
```

### Instrument Tag Parsing

The tool follows ISA instrument tag standards (e.g., `FT-101`, `PIC-202`):

- First letter: Measured variable (F=Flow, P=Pressure, T=Temperature, L=Level)
- Subsequent letters: Function (I=Indicator, T=Transmitter, C=Controller)
- Number: Loop/instrument identifier

### Line Number Parsing

Typical refinery line numbering format: `size-area-service-sequence-insulation`

Example: `6"-410-P-123-A`
- `6"` - Nominal diameter
- `410` - Area code
- `P` - Service code (Process)
- `123` - Sequence number
- `A` - Insulation code

## DEXPI Output

The DEXPI XML output conforms to the DEXPI standard (https://dexpi.org/), implementing:

- **Equipment**: Vessels, pumps, heat exchangers, valves, etc.
- **Piping**: PipingNetworkSegment with geometry and attributes
- **Instruments**: ProcessInstrument with function and range data
- **Connections**: Relationships between components
- **Metadata**: Creation date, source file, application info

### Supported DEXPI Elements

This implementation supports a pragmatic subset of DEXPI:

- Equipment classes (Vessel, Pump, Compressor, HeatExchanger, etc.)
- PipingNetworkSegment with polyline geometry
- ProcessInstrument with type and function
- Nozzle connection points
- GenericAttributes for custom properties

## Graph Output

### Graph Structure

- **Nodes**: Equipment, instruments, and pipe runs
- **Edges**: "Connected to" relationships with direction
- **Attributes**: All P&ID metadata (tags, types, dimensions, etc.)

### GraphML Format

GraphML is an XML-based graph format supported by:
- **Gephi**: Graph visualization and exploration
- **Cytoscape**: Network analysis and visualization
- **Neo4j**: Import to graph database
- **NetworkX**: Python graph analysis

### JSON Format

The JSON export provides a simple structure:

```json
{
  "graph_info": {
    "name": "Drawing Name",
    "node_count": 42,
    "edge_count": 56
  },
  "nodes": [
    {
      "id": "V-101",
      "type": "equipment",
      "equipment_type": "vessel",
      "x": 100.0,
      "y": 200.0
    }
  ],
  "edges": [
    {
      "source": "V-101",
      "target": "6\"-410-P-123-A",
      "connection_type": "outlet"
    }
  ]
}
```

### Cypher Format

Cypher output can be executed in Neo4j:

```cypher
CREATE (n:Equipment {id: "V-101", equipment_type: "vessel", tag: "V-101"})
CREATE (n:Pipe {id: "6\"-410-P-123-A", line_type: "process"})

MATCH (a {id: "V-101"}), (b {id: "6\"-410-P-123-A"})
CREATE (a)-[r:OUTLET]->(b)
```

## Testing

Run the test suite:

```bash
pytest
```

Run with coverage:

```bash
pytest --cov=pid2dexpi --cov-report=html
```

Run specific test file:

```bash
pytest tests/test_pid_model.py -v
```

## Development

### Code Formatting

```bash
black src/ tests/
```

### Type Checking

```bash
mypy src/
```

### Adding New Equipment Types

1. Add enum value to `EquipmentType` in `pid_model.py`
2. Add mapping in `BLOCK_TO_EQUIPMENT` in `config.py`
3. Add DEXPI class mapping in `EQUIPMENT_CLASS_MAP` in `dexpi_exporter.py`

### Adding New Export Formats

1. Add export method to `GraphBuilder` or `DEXPIExporter`
2. Update CLI in `cli.py` to support new format
3. Add tests in `tests/`

## Limitations and Future Enhancements

### Current Limitations

- Simplified connection inference based on spatial proximity
- 2D geometry only (no 3D support)
- Limited attribute extraction from DXF
- Subset of DEXPI specification implemented

### Potential Enhancements

- Advanced topology analysis for better connection detection
- Support for more DXF entity types (circles, arcs, hatches)
- Attribute extraction from DXF extended data (XDATA)
- Full DEXPI 1.3 specification support
- Integration with process simulation tools
- Web-based visualization interface
- Support for other CAD formats (DWG, PDF extraction)

## References

- **DEXPI**: https://dexpi.org/
- **ISO 15926**: Process data standard
- **ezdxf Documentation**: https://ezdxf.readthedocs.io/
- **NetworkX**: https://networkx.org/
- **ISA S5.1**: Instrumentation symbols and identification

## License

This project is part of the OpenAI Agents SDK examples.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## Support

For issues and questions:
- Check the documentation above
- Review existing issues in the repository
- Create a new issue with details about your problem

## Authors

Refinery Engineering Team

## Acknowledgments

- DEXPI Consortium for the DEXPI standard
- ezdxf library maintainers
- NetworkX community
