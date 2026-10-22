"""PDF короткого показа из проверенных рендеров."""
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

root = Path(__file__).resolve().parent
images = root / ".build/первая_встреча_v3"
output = root / "01_23-24_октября_Основа/К первой встрече.pdf"
if output.exists():
    raise SystemExit("PDF уже существует, выберите новое имя.")
pdf = canvas.Canvas(str(output), pagesize=(960, 540), pageCompression=1)
pdf.setTitle("ИИ-помощник менеджера: первая встреча")
for i in range(1, 7):
    pdf.drawImage(str(images / f"slide-{i}.png"), 0, 0, width=960, height=540)
    pdf.showPage()
pdf.save()
reader = PdfReader(output)
assert len(reader.pages) == 6
for i, page in enumerate(reader.pages, 1):
    assert float(page.mediabox.width) / float(page.mediabox.height) == 16 / 9
    actual = page.images[0].image.convert("RGB")
    with Image.open(images / f"slide-{i}.png") as original:
        expected = original.convert("RGB")
        assert actual.size == expected.size and actual.tobytes() == expected.tobytes()
print("PDF: 6 страниц, 16:9, проверенные рендеры без замены шрифтов")
