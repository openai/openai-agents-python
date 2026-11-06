"""Generate the synthetic dataroom fixture files."""

from pathlib import Path


def pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_plain_pdf(path: Path, lines: list[str]) -> None:
    content_lines = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
    for index, line in enumerate(lines):
        operator = "Tj" if index == 0 else "T* Tj"
        content_lines.append(f"({pdf_escape(line)}) {operator}")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("utf-8")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            "trailer\n"
            f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(pdf)


def write_financial_pdf(path: Path, title: str, lines: list[str], rows: list[list[str]]) -> None:
    write_plain_pdf(path, [title, *lines, *(" | ".join(row) for row in rows)])


def write_fixture_text(data_dir: Path, filename: str, content: str) -> None:
    (data_dir / filename).write_text(content.strip() + "\n", encoding="utf-8")


def main() -> None:
    data_dir = Path(__file__).resolve().parent
    write_fixture_text(
        data_dir,
        "10-k-mdna-overview.txt",
        """
UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
Washington, D.C. 20549

FORM 10-K
ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934
For the fiscal year ended December 31, 2025

HelioCart, Inc.

PART II
Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations

Revenue for fiscal 2025 was $1,284 million, compared with $1,008 million in fiscal 2024.
The increase was driven primarily by Platform revenue growth from merchant fraud
decisioning and payment orchestration workloads.

Gross margin improved to 71.4% in fiscal 2025 from 68.2% in fiscal 2024 because a higher
mix of transaction volume ran on lower-cost model serving infrastructure.

Operating income was $186 million in fiscal 2025, compared with $118 million in fiscal 2024.
Management uses "net revenue" and "revenue" interchangeably in this MD&A section.
""",
    )
    write_fixture_text(
        data_dir,
        "10-k-mdna-liquidity.txt",
        """
UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
Washington, D.C. 20549

FORM 10-K
ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934
For the fiscal year ended December 31, 2025

HelioCart, Inc.

PART II
Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations

Liquidity and capital resources. Net cash provided by operating activities was $248 million
in fiscal 2025, compared with $192 million in fiscal 2024, primarily because of higher
cash collections and improved operating margins.

Capital expenditures were $86 million in fiscal 2025 and $73 million in fiscal 2024.
Free cash flow, a non-GAAP measure defined as operating cash flow less capital
expenditures, was $162 million in fiscal 2025 and $119 million in fiscal 2024.
""",
    )
    write_fixture_text(
        data_dir,
        "10-k-note-segments.txt",
        """
UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
Washington, D.C. 20549

FORM 10-K
ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934
For the fiscal year ended December 31, 2025

HelioCart, Inc.

PART II
Item 8. Financial Statements and Supplementary Data

Note 4. Revenue by reportable segment

Platform segment revenue was $942 million in fiscal 2025 and $711 million in fiscal 2024.
Services segment revenue was $342 million in fiscal 2025 and $297 million in fiscal 2024.

Management refers to Platform revenue as "Subscription and transaction platform revenue"
in some tables; treat that label as the same Platform segment revenue metric.
""",
    )
    write_fixture_text(
        data_dir,
        "10-k-note-geography.txt",
        """
UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
Washington, D.C. 20549

FORM 10-K
ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934
For the fiscal year ended December 31, 2025

HelioCart, Inc.

PART II
Item 8. Financial Statements and Supplementary Data

Note 5. Revenue by geography

Americas revenue was $764 million in fiscal 2025, EMEA revenue was $343 million,
and APAC revenue was $177 million. Those regional line items reconcile to the
company-wide revenue figure disclosed in MD&A.
""",
    )
    write_fixture_text(
        data_dir,
        "10-k-note-balance-sheet.txt",
        """
UNITED STATES
SECURITIES AND EXCHANGE COMMISSION
Washington, D.C. 20549

FORM 10-K
ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934
For the fiscal year ended December 31, 2025

HelioCart, Inc.

PART II
Item 8. Financial Statements and Supplementary Data

Note 7. Selected balance sheet metrics

Cash and cash equivalents were $422 million as of December 31, 2025, compared with
$351 million as of December 31, 2024. Deferred revenue was $402 million as of
December 31, 2025, compared with $337 million as of December 31, 2024.
""",
    )

    write_financial_pdf(
        data_dir / "10-k-statements-of-operations.pdf",
        "Consolidated Statements of Operations",
        [
            "The table below presents annual operating results for fiscal 2025 and fiscal 2024.",
            "Revenue and net revenue refer to the same top-line measure for this synthetic filing.",
        ],
        [
            ["Metric", "FY2025", "FY2024"],
            ["Net revenue", "1,284", "1,008"],
            ["Gross profit", "917", "687"],
            ["Operating income", "186", "118"],
        ],
    )
    write_financial_pdf(
        data_dir / "10-k-balance-sheets.pdf",
        "Consolidated Balance Sheets",
        [
            "The table below presents selected balance sheet amounts as of December 31, 2025 and 2024.",
            "Amounts are shown in USD millions.",
        ],
        [
            ["Metric", "2025", "2024"],
            ["Cash and cash equivalents", "422", "351"],
            ["Accounts receivable", "211", "187"],
            ["Deferred revenue", "402", "337"],
        ],
    )
    write_financial_pdf(
        data_dir / "10-k-statements-of-cash-flows.pdf",
        "Consolidated Statements of Cash Flows",
        [
            "The table below presents selected annual cash flow metrics for fiscal 2025 and 2024.",
            "Net cash provided by operating activities is also described as operating cash flow in MD&A.",
        ],
        [
            ["Metric", "FY2025", "FY2024"],
            ["Net cash provided by operating activities", "248", "192"],
            ["Capital expenditures", "86", "73"],
            ["Free cash flow", "162", "119"],
        ],
    )


if __name__ == "__main__":
    main()
