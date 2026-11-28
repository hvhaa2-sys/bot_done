"""PDF из проверенных рендеров, без замены шрифтов на компьютере для показа."""
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[2]
RENDERS = Path('/Users/glebsuchodoev/Library/Application Support/artifacts/artifact-sessions/exports/latest/'
               '6ef21f48a50fd451f0d292bfdafb2b9bcf673ef57f61f61ee4ee11d0d39fb460')
output = ROOT / 'Презентация/Защита проекта 2026-10-21.pdf'
if output.exists():
    raise SystemExit('Готовый PDF уже существует, выберите новое имя.')
pdf = canvas.Canvas(str(output), pagesize=(960, 540), pageCompression=1)
pdf.setTitle('Знания из переписки: ИИ-помощник менеджера по продажам')
pdf.setAuthor('Проектный практикум УрФУ')
for index in range(1, 17):
    pdf.drawImage(str(RENDERS / f'slide-{index}.png'), 0, 0, width=960, height=540)
    pdf.showPage()
pdf.save()
reader = PdfReader(output)
assert len(reader.pages) == 16
assert all(float(p.mediabox.width) / float(p.mediabox.height) == 16 / 9 for p in reader.pages)
print(f'PDF: {len(reader.pages)} страниц, 16:9, {output.stat().st_size} байт')
