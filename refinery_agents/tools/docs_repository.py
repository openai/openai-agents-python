"""Stubbed document repository for refinery engineering sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List


@dataclass(slots=True)
class DocumentReference:
    """Metadata describing a discoverable refinery engineering document."""

    doc_type: str
    identifier: str
    revision: str | None = None
    description: str | None = None
    path: str | None = None
    content_summary: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Convert the reference to a serialisable dictionary."""

        return {
            "doc_type": self.doc_type,
            "identifier": self.identifier,
            "revision": self.revision,
            "description": self.description,
            "path": self.path,
            "content_summary": self.content_summary,
        }


@dataclass(slots=True)
class DocumentRepository:
    """Simulated repository that exposes only allowed refinery document sources."""

    documents_root: str = "./sample_docs"
    _documents: dict[str, list[DocumentReference]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._documents = {
            "P&ID": [
                DocumentReference(
                    doc_type="P&ID",
                    identifier="410-PID-6201",
                    revision="C",
                    description="Unit 410 feed section including FT-410-P-123-A",
                    path=f"{self.documents_root}/pids/410-PID-6201.pdf",
                    content_summary=(
                        "Shows 6\" feed line with existing vortex flow meter FT-410-P-123-A, "
                        "bypass arrangement, and downstream control valve CV-410-45."
                    ),
                ),
                DocumentReference(
                    doc_type="P&ID",
                    identifier="410-PID-6202",
                    revision="B",
                    description="Meter downstream tie-ins to unit battery limits",
                    path=f"{self.documents_root}/pids/410-PID-6202.pdf",
                    content_summary=(
                        "Highlights pressure transmitters, isolation valves, and flare "
                        "header connection for the same line."
                    ),
                ),
            ],
            "PFD": [
                DocumentReference(
                    doc_type="PFD",
                    identifier="410-PFD-100",
                    revision="A",
                    description="Unit 410 overall process flow diagram",
                    path=f"{self.documents_root}/pfds/410-PFD-100.pdf",
                    content_summary=(
                        "Summarises material balance for feed surge drum and downstream processing."
                    ),
                )
            ],
            "LineList": [
                DocumentReference(
                    doc_type="LineList",
                    identifier="LL-410-0001",
                    revision="7",
                    description="6\" line 410-P-123 service data, design pressure/temperature",
                    path=f"{self.documents_root}/linelist/LL-410-0001.xlsx",
                    content_summary=(
                        "Design pressure 900 kPag, normal 520 kPag, design temp 230C, "
                        "corrosion allowance 3 mm."
                    ),
                )
            ],
            "InstrumentDatasheet": [
                DocumentReference(
                    doc_type="InstrumentDatasheet",
                    identifier="DS-FT-410-P-123-A",
                    revision="5",
                    description="Existing vortex meter datasheet",
                    path=f"{self.documents_root}/datasheets/DS-FT-410-P-123-A.pdf",
                    content_summary=(
                        "Vortex flow meter, 6\" ANSI 300, range 0-1800 m3/h, 4-20mA, HART."
                    ),
                ),
                DocumentReference(
                    doc_type="InstrumentDatasheet",
                    identifier="DS-CORIOLIS-6IN",
                    revision="2",
                    description="Coriolis mass flow meter proposal",
                    path=f"{self.documents_root}/datasheets/DS-CORIOLIS-6IN.pdf",
                    content_summary=(
                        "Dual straight-tube coriolis meter, accuracy ±0.1%, SIL2 capable transmitter."
                    ),
                ),
            ],
            "VendorManual": [
                DocumentReference(
                    doc_type="VendorManual",
                    identifier="VM-CORIOLIS-MODEL-X200",
                    revision="1.4",
                    description="Vendor manual for CoriFlow X200 mass flow meter",
                    path=f"{self.documents_root}/vendor/Coriflow-X200.pdf",
                    content_summary=(
                        "Installation clearances, diagnostics, density calibration, and "
                        "maintenance procedures for CoriFlow X200."
                    ),
                ),
                DocumentReference(
                    doc_type="VendorManual",
                    identifier="VM-ULTRASONIC-U500",
                    revision="3.1",
                    description="Clamp-on ultrasonic meter installation guide",
                    path=f"{self.documents_root}/vendor/Ultrasonic-U500.pdf",
                    content_summary=(
                        "Requirements for straight-run piping, temperature limits, and "
                        "signal cabling."
                    ),
                ),
            ],
            "PipeClass": [
                DocumentReference(
                    doc_type="PipeClass",
                    identifier="PC-CS-CL300",
                    revision="E",
                    description="Carbon steel Class 300 piping specification",
                    path=f"{self.documents_root}/pipeclass/PC-CS-CL300.pdf",
                    content_summary=(
                        "Material compatibility, gasket selection, flange ratings, and "
                        "bolt torque values for Class 300 piping."
                    ),
                )
            ],
            "Isometric": [
                DocumentReference(
                    doc_type="Isometric",
                    identifier="ISO-410-6IN-2450",
                    revision="F",
                    description="Isometric for 6\" flow meter spool FT-410-P-123-A",
                    path=f"{self.documents_root}/isometrics/ISO-410-6IN-2450.pdf",
                    content_summary=(
                        "Gives elevations, spool lengths, supports, and instrument tap locations."
                    ),
                )
            ],
            "JBCableSchedule": [
                DocumentReference(
                    doc_type="JBCableSchedule",
                    identifier="JB-410-A27",
                    revision="3",
                    description="Junction box cable schedule for flow instruments in unit 410",
                    path=f"{self.documents_root}/cables/JB-410-A27.xlsx",
                    content_summary=(
                        "Details multicore cable routing, spare pairs, and termination details."
                    ),
                )
            ],
            "Standard": [
                DocumentReference(
                    doc_type="Standard",
                    identifier="ISA-TR12.15.03",
                    revision="2019",
                    description="ISA guidance on custody transfer flow measurement",
                    path=f"{self.documents_root}/standards/ISA-TR12-15-03.pdf",
                    content_summary=(
                        "Recommends coriolis meters for high accuracy liquids and outlines "
                        "proof-test intervals."
                    ),
                ),
                DocumentReference(
                    doc_type="Standard",
                    identifier="API-551",
                    revision="2022",
                    description="API guide for process measurement instrumentation",
                    path=f"{self.documents_root}/standards/API-551.pdf",
                    content_summary=(
                        "Provides installation best practices for flow instruments and "
                        "redundancy considerations."
                    ),
                ),
            ],
        }

    def _filter(self, doc_type: str, keywords: Iterable[str | None]) -> list[DocumentReference]:
        """Return documents where all provided keywords are present in metadata."""

        docs = self._documents.get(doc_type, [])
        normalized = [kw.lower() for kw in keywords if kw]
        if not normalized:
            return list(docs)

        filtered: List[DocumentReference] = []
        for doc in docs:
            haystacks = " ".join(filter(None, [doc.identifier, doc.description or ""])).lower()
            if all(kw in haystacks for kw in normalized):
                filtered.append(doc)
        return filtered

    def find_pids(self, unit: str | None = None, equipment_tag: str | None = None) -> list[DocumentReference]:
        """Return P&IDs covering the requested unit and/or equipment tag."""

        return self._filter("P&ID", [unit, equipment_tag])

    def find_pfds(self, unit: str | None = None) -> list[DocumentReference]:
        """Return PFDs for the given unit."""

        return self._filter("PFD", [unit])

    def find_line_list(self, line_id: str) -> list[DocumentReference]:
        """Return line list entries for a specific line ID."""

        return self._filter("LineList", [line_id])

    def find_instrument_datasheet(self, tag: str) -> list[DocumentReference]:
        """Return instrument datasheets for the given tag or proposal."""

        return self._filter("InstrumentDatasheet", [tag])

    def find_vendor_manual(self, model: str) -> list[DocumentReference]:
        """Return vendor manuals for a vendor model family."""

        return self._filter("VendorManual", [model])

    def find_pipe_class(self, service: str, size: str | int) -> list[DocumentReference]:
        """Return pipe class specification covering the service and size."""

        return self._filter("PipeClass", [service, str(size)])

    def find_isometrics(self, identifier: str) -> list[DocumentReference]:
        """Return piping isometrics for a given identifier or tag."""

        return self._filter("Isometric", [identifier])

    def find_jb_cable_schedule(self, junction_box: str) -> list[DocumentReference]:
        """Return JB/cable schedules for a junction box identifier."""

        return self._filter("JBCableSchedule", [junction_box])

    def find_engineering_standards(self, query: str) -> list[DocumentReference]:
        """Return engineering standards matching the provided query string."""

        return self._filter("Standard", [query])

