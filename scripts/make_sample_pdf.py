"""Generate the sample City Map architecture PDF (dev-only, requires fpdf2).

Run once: python scripts/make_sample_pdf.py
"""

from pathlib import Path

from fpdf import FPDF

OUT = Path(__file__).resolve().parents[1] / "data" / "sample" / "city_map" / "architecture-overview.pdf"

PAGES = [
    (
        "City Map Architecture Overview",
        [
            "The City Map service stores the capability topology as a directed graph.",
            "Nodes are capabilities; edges are typed relationships (depends_on,",
            "registered_in). The graph is persisted in DynamoDB and exposed through",
            "a GraphQL API consumed by the Capability Registry and deploy pipelines.",
            "",
            "Write path: ownership changes enter through the City Map UI, are",
            "validated (owner_team required, approver on tier-1 changes), and",
            "published to the capability-events Kafka topic.",
        ],
    ),
    (
        "Data Flow and Consumers",
        [
            "The Capability Registry consumes capability-events and syncs ownership",
            "every 15 minutes. Deploy pipelines query the City Map at release time",
            "to block deploys of capabilities whose dependencies are not Active.",
            "",
            "Disaster recovery: the graph is snapshotted to S3 hourly. Restoring",
            "from snapshot takes under 10 minutes and is drilled quarterly by the",
            "platform team (owner: jordan.lee).",
        ],
    ),
]


def main() -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    for title, lines in PAGES:
        pdf.add_page()
        pdf.set_font("helvetica", style="B", size=16)
        pdf.cell(0, 12, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", size=11)
        for line in lines:
            if line:
                pdf.multi_cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.ln(4)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
