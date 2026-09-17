"""Original, deterministic samples authored for this project; no external document data."""

from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle


def make_sample(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=(595, 842), invariant=1)
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(56, 774, "Project report")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(56, 736, "This paragraph must remain editable and unchanged.")
    pdf.drawString(56, 712, "The original PDF is attached for reference.")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(56, 672, "Checklist")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(66, 642, "1. Preserve the complete document.")
    pdf.drawString(66, 620, "2. Verify the destination page.")
    table = Table(
        [["Item", "Quantity"], ["Apples", "12"], ["Pears", "24"]],
        colWidths=[230, 180],
        rowHeights=30,
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 1, colors.HexColor("#879d90")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5f0e9")),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    table.wrapOn(pdf, 410, 90)
    table.drawOn(pdf, 56, 476)
    image = Image.new("RGB", (800, 280), "#e5f0e9")
    drawing = ImageDraw.Draw(image)
    for left, height in [(80, 85), (300, 145), (520, 210)]:
        drawing.rectangle((left, 255 - height, left + 140, 255), fill="#347c60")
    pdf.drawImage(ImageReader(image), 56, 295, 410, 143)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(56, 272, "Figure 1. An original illustration for image preservation.")
    pdf.showPage()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf.setFont("STSong-Light", 22)
    pdf.drawString(56, 774, "中文转换测试")
    pdf.setFont("STSong-Light", 13)
    pdf.drawString(56, 738, "这段文字必须保留原意，不得摘要、翻译或改写。")
    pdf.drawString(56, 710, "复杂区域允许作为图片保留，并标注来源页码。")
    pdf.setFont("Helvetica", 16)
    pdf.drawString(150, 640, "E = mc")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(205, 650, "2")
    complex_table = Table(
        [["Merged heading", ""], ["Left", "Right"]], colWidths=[200, 200], rowHeights=35
    )
    complex_table.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (1, 0)),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
            ]
        )
    )
    complex_table.wrapOn(pdf, 400, 70)
    complex_table.drawOn(pdf, 56, 510)
    pdf.save()


if __name__ == "__main__":
    make_sample(Path("output/samples/representative.pdf"))
