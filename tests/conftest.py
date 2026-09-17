from io import BytesIO

import pytest
from reportlab.pdfgen import canvas


@pytest.fixture
def pdf_bytes():
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(72, 740, "MVP verification")
    pdf.drawString(72, 710, "Editable PDF content.")
    pdf.save()
    return stream.getvalue()
