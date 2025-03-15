#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "reportlab>=4.0.0",
# ]
# ///

"""
Script to generate a sample PDF document for testing the PDF extraction agent.
"""

import os
import sys

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
except ImportError:
    print("Required packages not found. Please run this script with uv:")
    print("uv run examples/extract_doc/sample_document.py")
    sys.exit(1)


def create_sample_pdf(output_path):
    """
    Create a sample PDF document with structured content for testing extraction.
    """
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    
    # Create custom styles
    title_style = styles["Title"]
    heading_style = styles["Heading1"]
    normal_style = styles["Normal"]
    
    # Create the content
    content = []
    
    # Title
    content.append(Paragraph("Research on Machine Learning Applications in Healthcare", title_style))
    content.append(Spacer(1, 12))
    
    # Authors
    content.append(Paragraph("Authors: Jane Smith, John Doe, Alice Johnson", styles["Heading3"]))
    content.append(Spacer(1, 12))
    
    # Publication Date
    content.append(Paragraph("Publication Date: March 15, 2025", styles["Heading3"]))
    content.append(Spacer(1, 24))
    
    # Abstract
    content.append(Paragraph("Abstract", heading_style))
    content.append(Paragraph(
        "This paper explores the applications of machine learning in healthcare, "
        "focusing on diagnostic tools, treatment optimization, and patient monitoring systems. "
        "We review recent advancements and discuss challenges and opportunities in this rapidly evolving field.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # Introduction
    content.append(Paragraph("1. Introduction", heading_style))
    content.append(Paragraph(
        "Machine learning has transformed healthcare in recent years, enabling more accurate "
        "diagnoses, personalized treatment plans, and efficient resource allocation. "
        "This paper provides an overview of current applications and future directions.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # Methods
    content.append(Paragraph("2. Methods", heading_style))
    content.append(Paragraph(
        "We conducted a systematic review of literature published between 2020 and 2025, "
        "focusing on peer-reviewed articles describing machine learning applications in clinical settings. "
        "Our analysis included both supervised and unsupervised learning approaches.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # Results
    content.append(Paragraph("3. Results", heading_style))
    content.append(Paragraph(
        "Our analysis identified three primary areas where machine learning has made significant impacts: "
        "diagnostic assistance, treatment optimization, and patient monitoring. Each area shows promising "
        "results but faces unique implementation challenges.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # Table: ML Applications
    content.append(Paragraph("Table 1: Machine Learning Applications in Healthcare", styles["Heading3"]))
    
    table_data = [
        ['Application Area', 'ML Techniques', 'Accuracy Range', 'Implementation Status'],
        ['Diagnostic Imaging', 'CNNs, Transfer Learning', '85-95%', 'Clinical Use'],
        ['Treatment Planning', 'Reinforcement Learning, GBMs', '75-88%', 'Clinical Trials'],
        ['Patient Monitoring', 'RNNs, LSTMs', '82-91%', 'Early Adoption'],
        ['Drug Discovery', 'GANs, Autoencoders', '70-85%', 'Research Phase']
    ]
    
    table = Table(table_data, colWidths=[120, 120, 100, 120])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    content.append(table)
    content.append(Spacer(1, 12))
    
    # Discussion
    content.append(Paragraph("4. Discussion", heading_style))
    content.append(Paragraph(
        "While machine learning shows great promise in healthcare, several challenges remain. "
        "These include data privacy concerns, model interpretability, regulatory approval processes, "
        "and integration with existing clinical workflows. Future research should address these challenges "
        "while expanding applications to underserved areas of medicine.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # Conclusion
    content.append(Paragraph("5. Conclusion", heading_style))
    content.append(Paragraph(
        "Machine learning continues to revolutionize healthcare by improving diagnostic accuracy, "
        "treatment efficacy, and patient outcomes. As technology advances and more data becomes "
        "available, we expect to see broader adoption and more sophisticated applications in clinical practice.",
        normal_style
    ))
    content.append(Spacer(1, 12))
    
    # References
    content.append(Paragraph("References", heading_style))
    references = [
        "Smith, J. et al. (2023). Deep Learning for Medical Image Analysis. Journal of AI in Medicine, 45(2), 112-128.",
        "Doe, J. & Johnson, A. (2024). Reinforcement Learning for Treatment Optimization. Healthcare Informatics Review, 18(3), 89-103.",
        "Chen, X. et al. (2022). Patient Monitoring Systems Using Recurrent Neural Networks. IEEE Transactions on Medical Systems, 41(4), 215-230.",
        "Williams, R. & Brown, T. (2025). Ethical Considerations in Healthcare AI. Bioethics Today, 12(1), 45-62.",
        "Garcia, M. et al. (2021). Generative Models for Drug Discovery. Nature Machine Intelligence, 3(5), 375-390."
    ]
    
    for ref in references:
        content.append(Paragraph(ref, normal_style))
        content.append(Spacer(1, 6))
    
    # Build the PDF
    doc.build(content)
    print(f"Sample PDF created at: {output_path}")


if __name__ == "__main__":
    # Create the examples directory if it doesn't exist
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, "sample_document.pdf")
    
    create_sample_pdf(output_path)
