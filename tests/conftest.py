from io import BytesIO

import pytest
from reportlab.pdfgen import canvas


@pytest.fixture(autouse=True)
def disable_configured_notifications(monkeypatch):
    # A developer's .env must never turn ordinary fixture tasks into real messages.
    # Notification tests opt in explicitly through the Settings constructor.
    monkeypatch.setenv("FEISHU_NOTIFY_ENABLED", "false")


@pytest.fixture
def pdf_bytes():
    stream = BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(72, 740, "MVP verification")
    pdf.drawString(72, 710, "Editable PDF content.")
    pdf.save()
    return stream.getvalue()
