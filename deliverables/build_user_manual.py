"""Generate the iRIS CLEAR User Manual PDF.

Content is drawn from the application as implemented: routes in
`frontend/src/App.tsx`, screen names in `components/layout/navigation.ts`,
roles and permissions in `backend/app/core/enums.py` + `backend/app/db/seed.py`,
and the on-screen labels in each page component. Nothing here is invented.

Run:  backend/.venv/Scripts/python.exe deliverables/build_user_manual.py
"""

from __future__ import annotations

import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOGO = os.path.join(ROOT, "frontend", "public", "image", "irisclear.png")
OUT = os.path.join(HERE, "iRIS_CLEAR_User_Manual.pdf")

ORG = "Iris Regtech Solution"
APP = "iRIS CLEAR"
APP_VERSION = "1.0.0"
DOC_VERSION = "1.0"
DOC_DATE = "21 August 2026"

# ---------------------------------------------------------------------------
# Palette - taken from the application's own interface colours.
# ---------------------------------------------------------------------------
NAVY = colors.HexColor("#0F172A")
INK = colors.HexColor("#16213B")
BLUE = colors.HexColor("#2563EB")
BLUE_SOFT = colors.HexColor("#EAF0FE")
SLATE = colors.HexColor("#5B6478")
MUTED = colors.HexColor("#8B96AC")
LINE = colors.HexColor("#E4E7EC")
PANEL = colors.HexColor("#F7F9FC")
GREEN = colors.HexColor("#0E9F6E")
GREEN_SOFT = colors.HexColor("#E8F7F1")
AMBER = colors.HexColor("#B45309")
AMBER_SOFT = colors.HexColor("#FDF4E3")
ROSE = colors.HexColor("#BE123C")
ROSE_SOFT = colors.HexColor("#FDECEF")
WATERMARK = colors.HexColor("#ECEFF5")

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
FONTS = "C:\\Windows\\Fonts"
try:
    pdfmetrics.registerFont(TTFont("Sg", os.path.join(FONTS, "segoeui.ttf")))
    pdfmetrics.registerFont(TTFont("Sg-B", os.path.join(FONTS, "segoeuib.ttf")))
    pdfmetrics.registerFont(TTFont("Sg-I", os.path.join(FONTS, "segoeuii.ttf")))
    pdfmetrics.registerFont(TTFont("Sg-BI", os.path.join(FONTS, "segoeuiz.ttf")))
    pdfmetrics.registerFont(TTFont("Sg-L", os.path.join(FONTS, "segoeuisl.ttf")))
    pdfmetrics.registerFontFamily("Sg", normal="Sg", bold="Sg-B", italic="Sg-I", boldItalic="Sg-BI")
    F, FB, FI, FL = "Sg", "Sg-B", "Sg-I", "Sg-L"
except Exception:  # pragma: no cover - fallback on a machine without Segoe UI
    F, FB, FI, FL = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica"

PAGE_W, PAGE_H = A4
ML = MR = 18 * mm
MT = 20 * mm
MB = 16 * mm
CW = PAGE_W - ML - MR  # content width

# ---------------------------------------------------------------------------
# Paragraph styles
# ---------------------------------------------------------------------------
S = {
    "h1": ParagraphStyle("h1", fontName=FB, fontSize=15.5, leading=19, textColor=NAVY,
                         spaceBefore=0, spaceAfter=1.4 * mm),
    "eyebrow": ParagraphStyle("eyebrow", fontName=FB, fontSize=7, leading=9, textColor=BLUE,
                              spaceAfter=1.2 * mm),
    "h2": ParagraphStyle("h2", fontName=FB, fontSize=10.4, leading=13, textColor=INK,
                         spaceBefore=2.4 * mm, spaceAfter=1.0 * mm),
    "h3": ParagraphStyle("h3", fontName=FB, fontSize=8.8, leading=11.5, textColor=BLUE,
                         spaceBefore=1.8 * mm, spaceAfter=0.7 * mm),
    "body": ParagraphStyle("body", fontName=F, fontSize=8.7, leading=12.2, textColor=INK,
                           alignment=TA_JUSTIFY, spaceAfter=1.4 * mm),
    "lead": ParagraphStyle("lead", fontName=F, fontSize=9.3, leading=13.4, textColor=SLATE,
                           alignment=TA_LEFT, spaceAfter=2.0 * mm),
    "small": ParagraphStyle("small", fontName=F, fontSize=7.6, leading=10.4, textColor=SLATE,
                            spaceAfter=1.2 * mm),
    "caption": ParagraphStyle("caption", fontName=FI, fontSize=7.2, leading=9.4, textColor=MUTED,
                              alignment=TA_CENTER, spaceBefore=1.0 * mm, spaceAfter=1.6 * mm),
    "step": ParagraphStyle("step", fontName=F, fontSize=8.6, leading=11.8, textColor=INK,
                           leftIndent=6.4 * mm, firstLineIndent=-6.4 * mm, spaceAfter=0.9 * mm),
    "bullet": ParagraphStyle("bullet", fontName=F, fontSize=8.6, leading=11.8, textColor=INK,
                             leftIndent=4.2 * mm, firstLineIndent=-4.2 * mm, spaceAfter=0.9 * mm),
    "th": ParagraphStyle("th", fontName=FB, fontSize=7.6, leading=9.6, textColor=colors.white),
    "td": ParagraphStyle("td", fontName=F, fontSize=7.9, leading=10.4, textColor=INK),
    "tdb": ParagraphStyle("tdb", fontName=FB, fontSize=7.9, leading=10.4, textColor=INK),
    "td-s": ParagraphStyle("td-s", fontName=F, fontSize=7.4, leading=9.6, textColor=INK),
    "tdb-s": ParagraphStyle("tdb-s", fontName=FB, fontSize=7.4, leading=9.6, textColor=INK),
    "tdc": ParagraphStyle("tdc", fontName=FB, fontSize=8.6, leading=10.4, textColor=GREEN,
                          alignment=TA_CENTER),
    "tdx": ParagraphStyle("tdx", fontName=F, fontSize=8.6, leading=10.4, textColor=colors.HexColor("#C3C9D4"),
                          alignment=TA_CENTER),
    "callout": ParagraphStyle("callout", fontName=F, fontSize=8.3, leading=11.4, textColor=INK,
                              spaceAfter=0),
    "callhead": ParagraphStyle("callhead", fontName=FB, fontSize=8.3, leading=11.4, textColor=INK,
                               spaceAfter=0.6 * mm),
}


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def P(text, style="body"):
    return Paragraph(text, S[style])


def steps(items):
    out = []
    for i, text in enumerate(items, 1):
        out.append(Paragraph(f'<font name="{FB}" color="#2563EB">{i}.</font>&nbsp;&nbsp;{text}', S["step"]))
    return out


def bullets(items, style="bullet"):
    out = []
    for text in items:
        out.append(Paragraph(f'<font color="#2563EB">\u25aa</font>&nbsp;&nbsp;{text}', S[style]))
    return out


class Mark(Flowable):
    """A drawn tick or dash for the permission grid.

    Segoe UI carries no U+2713 glyph and the base-14 ZapfDingbats does not render
    everywhere, so the mark is drawn rather than typeset.
    """

    def __init__(self, width, on=True, size=2.5 * mm):
        super().__init__()
        self.width = width
        self.size = size
        self.on = on
        self.height = size + 1.4 * mm

    def draw(self):
        c = self.canv
        cx, cy, s = self.width / 2.0, self.height / 2.0, self.size
        if self.on:
            c.setStrokeColor(GREEN)
            c.setLineWidth(1.3)
            c.setLineCap(1)
            c.setLineJoin(1)
            p = c.beginPath()
            p.moveTo(cx - s * 0.46, cy + s * 0.02)
            p.lineTo(cx - s * 0.13, cy - s * 0.33)
            p.lineTo(cx + s * 0.47, cy + s * 0.40)
            c.drawPath(p, stroke=1, fill=0)
        else:
            c.setStrokeColor(colors.HexColor("#C7CDD8"))
            c.setLineWidth(1.0)
            c.line(cx - s * 0.36, cy, cx + s * 0.36, cy)


def tbl(rows, widths, header=True, aligns=None, pad=1.9, dense=False):
    """A compact enterprise table. `rows[0]` is the header when `header`."""
    body_st = "td-s" if dense else "td"
    bold_st = "tdb-s" if dense else "tdb"
    data = []
    for r_i, row in enumerate(rows):
        line = []
        for c_i, cell in enumerate(row):
            if isinstance(cell, Flowable):
                line.append(cell)
            elif header and r_i == 0:
                line.append(Paragraph(str(cell), S["th"]))
            elif cell in ("Y", "-"):
                line.append(Mark(widths[c_i] - 4.8 * mm, on=(cell == "Y")))
            elif c_i == 0 and len(row) > 1:
                line.append(Paragraph(str(cell), S[bold_st]))
            else:
                line.append(Paragraph(str(cell), S[body_st]))
        data.append(line)

    style = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), pad * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad * mm),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TOPPADDING", (0, 0), (-1, 0), 1.7 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 1.7 * mm),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
        ]
    else:
        style += [("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, PANEL])]
    if aligns:
        for col, al in aligns.items():
            style.append(("ALIGN", (col, 0), (col, -1), al))

    t = Table(data, colWidths=widths, hAlign="LEFT")
    t.setStyle(TableStyle(style))
    return t


def callout(title, text, tone="blue"):
    bg, bar = {
        "blue": (BLUE_SOFT, BLUE),
        "amber": (AMBER_SOFT, AMBER),
        "rose": (ROSE_SOFT, ROSE),
        "green": (GREEN_SOFT, GREEN),
    }[tone]
    inner = [Paragraph(title, S["callhead"]), Paragraph(text, S["callout"])] if title else [
        Paragraph(text, S["callout"])
    ]
    t = Table([[inner]], colWidths=[CW], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 1.6, bar),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.2 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
    ]))
    return t


def two_col(left, right, gap=5 * mm, ratio=0.5):
    lw = (CW - gap) * ratio
    rw = CW - gap - lw
    t = Table([[left, "", right]], colWidths=[lw, gap, rw], hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


class HRule(Flowable):
    def __init__(self, sb=1.5 * mm, sa=2.0 * mm):
        super().__init__()
        self.sb, self.sa = sb, sa
        self.width = CW
        self.height = self.sb + self.sa

    def draw(self):
        self.canv.setStrokeColor(LINE)
        self.canv.setLineWidth(0.6)
        self.canv.line(0, self.sa, CW, self.sa)


def rule(space_before=1.5 * mm, space_after=2.0 * mm):
    return HRule(space_before, space_after)


# ---------------------------------------------------------------------------
# Diagrams (interface layout schematics + flow charts - not screen captures)
# ---------------------------------------------------------------------------
class ShellDiagram(Flowable):
    """Schematic of the main workspace: sidebar, top bar, content area."""

    def __init__(self, width=CW, height=50 * mm):
        super().__init__()
        self.width, self.height = width, height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.roundRect(0, 0, w, h, 2 * mm, stroke=1, fill=1)

        # Sidebar
        sw = 34 * mm
        c.setFillColor(NAVY)
        c.roundRect(0, 0, sw, h, 2 * mm, stroke=0, fill=1)
        c.setFillColor(NAVY)
        c.rect(sw - 3 * mm, 0, 3 * mm, h, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FB, 6.2)
        c.drawString(4 * mm, h - 5.6 * mm, "iRIS CLEAR")
        nav = ["Dashboard", "Upload", "Contracts", "Portfolio", "Search",
               "Copilot", "Processing", "Clause Coverage", "Alerts", "Exports"]
        y = h - 10.4 * mm
        for i, label in enumerate(nav):
            if i == 0:
                c.setFillColor(BLUE)
                c.roundRect(2.6 * mm, y - 1.2 * mm, sw - 5.6 * mm, 3.3 * mm, 0.8 * mm, stroke=0, fill=1)
                c.setFillColor(colors.white)
            else:
                c.setFillColor(MUTED)
            c.setFont(F, 5.3)
            c.drawString(4.6 * mm, y, label)
            y -= 3.5 * mm
        c.setStrokeColor(colors.HexColor("#28354F"))
        c.setLineWidth(0.5)
        c.line(4 * mm, y + 1.4 * mm, sw - 5 * mm, y + 1.4 * mm)
        c.setFillColor(colors.HexColor("#5D6B85"))
        c.setFont(FB, 4.6)
        c.drawString(4 * mm, y - 2.2 * mm, "ADMINISTRATION")

        # Top bar
        bx = sw
        bw = w - sw
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.rect(bx, h - 9 * mm, bw, 9 * mm, stroke=1, fill=1)
        c.setFillColor(MUTED)
        c.setFont(FB, 4.6)
        c.drawString(bx + 3.5 * mm, h - 3.8 * mm, "WORKSPACE")
        c.setFillColor(NAVY)
        c.setFont(FB, 7.2)
        c.drawString(bx + 3.5 * mm, h - 7.4 * mm, "Dashboard")
        # Business unit selector + avatar
        c.setStrokeColor(LINE)
        c.setFillColor(PANEL)
        c.roundRect(bx + bw - 40 * mm, h - 7 * mm, 26 * mm, 4.6 * mm, 1 * mm, stroke=1, fill=1)
        c.setFillColor(SLATE)
        c.setFont(F, 5.2)
        c.drawString(bx + bw - 38.4 * mm, h - 5.6 * mm, "All my business units")
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.circle(bx + bw - 10.6 * mm, h - 4.7 * mm, 2.2 * mm, stroke=1, fill=1)
        c.setFillColor(BLUE)
        c.circle(bx + bw - 4.6 * mm, h - 4.7 * mm, 2.2 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FB, 4.4)
        c.drawCentredString(bx + bw - 4.6 * mm, h - 5.5 * mm, "SK")

        # KPI tiles
        tx = bx + 3.5 * mm
        tw = (bw - 7 * mm - 3 * 2.4 * mm) / 4
        for i, (lab, val) in enumerate([("Contracts", "128"), ("Expiring 90d", "12"),
                                        ("High risk", "9"), ("Needs review", "5")]):
            x = tx + i * (tw + 2.4 * mm)
            c.setFillColor(colors.white)
            c.setStrokeColor(LINE)
            c.roundRect(x, h - 22 * mm, tw, 10.5 * mm, 1.2 * mm, stroke=1, fill=1)
            c.setFillColor(MUTED)
            c.setFont(F, 4.8)
            c.drawString(x + 2 * mm, h - 14.6 * mm, lab)
            c.setFillColor(NAVY)
            c.setFont(FB, 9)
            c.drawString(x + 2 * mm, h - 20 * mm, val)
            c.setFillColor(BLUE)
            c.rect(x, h - 22 * mm, tw, 0.9 * mm, stroke=0, fill=1)

        # Panels
        pw = (bw - 7 * mm - 2.4 * mm) / 2
        for i, lab in enumerate(["Renewal watchlist", "Risk distribution"]):
            x = tx + i * (pw + 2.4 * mm)
            c.setFillColor(colors.white)
            c.setStrokeColor(LINE)
            c.roundRect(x, 3.5 * mm, pw, h - 27 * mm, 1.2 * mm, stroke=1, fill=1)
            c.setFillColor(NAVY)
            c.setFont(FB, 5.4)
            c.drawString(x + 2 * mm, h - 26.5 * mm, lab)
            c.setFillColor(colors.HexColor("#EEF1F6"))
            for r in range(4):
                c.rect(x + 2 * mm, h - 31.5 * mm - r * 3.1 * mm, pw - 8 * mm, 1.5 * mm, stroke=0, fill=1)


class SplitDiagram(Flowable):
    """Schematic of the contract review workspace: extracted pane + document pane."""

    def __init__(self, width=CW, height=42 * mm):
        super().__init__()
        self.width, self.height = width, height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        half = (w - 3 * mm) / 2

        # Left: extracted knowledge
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.7)
        c.roundRect(0, 0, half, h, 1.6 * mm, stroke=1, fill=1)
        c.setFillColor(NAVY)
        c.setFont(FB, 6.4)
        c.drawString(3 * mm, h - 5.4 * mm, "Master Services Agreement")
        c.setFillColor(MUTED)
        c.setFont(F, 5)
        c.drawString(3 * mm, h - 8.6 * mm, "MSA  \u00b7  34 pages  \u00b7  Version 1")
        tabs = ["Overview", "Risks 4", "Obligations 11", "Key dates", "Confidentiality"]
        x = 3 * mm
        for i, t in enumerate(tabs):
            tw = pdfmetrics.stringWidth(t, F, 5) + 3.4 * mm
            c.setFillColor(BLUE if i == 4 else PANEL)
            c.setStrokeColor(LINE)
            c.roundRect(x, h - 14.6 * mm, tw, 4.2 * mm, 1 * mm, stroke=0 if i == 4 else 1, fill=1)
            c.setFillColor(colors.white if i == 4 else SLATE)
            c.setFont(F, 5)
            c.drawString(x + 1.7 * mm, h - 13.3 * mm, t)
            x += tw + 1.4 * mm
        # Clause card
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.roundRect(3 * mm, 3 * mm, half - 6 * mm, h - 20 * mm, 1.4 * mm, stroke=1, fill=1)
        c.setFillColor(NAVY)
        c.setFont(FB, 5.6)
        c.drawString(5 * mm, h - 20.5 * mm, "Confidentiality")
        for lab, fill, tx_col in ((" Page 7 ", PANEL, SLATE), (" 92% ", GREEN_SOFT, GREEN)):
            pass
        c.setFillColor(PANEL)
        c.roundRect(half - 28 * mm, h - 21.4 * mm, 10 * mm, 3.4 * mm, 1 * mm, stroke=0, fill=1)
        c.setFillColor(SLATE)
        c.setFont(F, 4.6)
        c.drawCentredString(half - 23 * mm, h - 20.4 * mm, "Page 7")
        c.setFillColor(GREEN_SOFT)
        c.roundRect(half - 17 * mm, h - 21.4 * mm, 8 * mm, 3.4 * mm, 1 * mm, stroke=0, fill=1)
        c.setFillColor(GREEN)
        c.setFont(FB, 4.6)
        c.drawCentredString(half - 13 * mm, h - 20.4 * mm, "92%")
        c.setFillColor(colors.HexColor("#EEF1F6"))
        for r in range(3):
            c.rect(5 * mm, h - 26.5 * mm - r * 2.8 * mm, half - 12 * mm - r * 6 * mm, 1.4 * mm, stroke=0, fill=1)
        for i, (lab, col, bg) in enumerate((("Evidence", BLUE, BLUE_SOFT),
                                            ("Approve", GREEN, GREEN_SOFT),
                                            ("Reject", ROSE, ROSE_SOFT))):
            bx = 5 * mm + i * 16 * mm
            c.setFillColor(bg)
            c.roundRect(bx, 5 * mm, 14 * mm, 4.4 * mm, 1 * mm, stroke=0, fill=1)
            c.setFillColor(col)
            c.setFont(FB, 5)
            c.drawCentredString(bx + 7 * mm, 6.4 * mm, lab)

        # Right: document viewer
        rx = half + 3 * mm
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.roundRect(rx, 0, half, h, 1.6 * mm, stroke=1, fill=1)
        c.setFillColor(PANEL)
        c.roundRect(rx, h - 7 * mm, half, 7 * mm, 1.6 * mm, stroke=0, fill=1)
        c.setFillColor(SLATE)
        c.setFont(F, 5)
        c.drawString(rx + 3 * mm, h - 4.6 * mm, "Page 7 of 34")
        for i, lab in enumerate(["\u2212", "+", "Fit"]):
            c.setFillColor(colors.white)
            c.setStrokeColor(LINE)
            c.roundRect(rx + half - 22 * mm + i * 7 * mm, h - 5.6 * mm, 6 * mm, 4 * mm, 0.8 * mm, stroke=1, fill=1)
            c.setFillColor(SLATE)
            c.setFont(F, 4.8)
            c.drawCentredString(rx + half - 19 * mm + i * 7 * mm, h - 4.4 * mm, lab)
        # Page canvas with highlight
        c.setFillColor(colors.white)
        c.setStrokeColor(LINE)
        c.rect(rx + 8 * mm, 3 * mm, half - 16 * mm, h - 12 * mm, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#EEF1F6"))
        yy = h - 13 * mm
        for r in range(9):
            wdt = (half - 22 * mm) if r % 3 else (half - 30 * mm)
            c.rect(rx + 11 * mm, yy, wdt, 1.3 * mm, stroke=0, fill=1)
            yy -= 2.9 * mm
        c.setFillColor(colors.HexColor("#FFF3C4"))
        c.setStrokeColor(colors.HexColor("#F0B429"))
        c.setLineWidth(0.8)
        c.rect(rx + 10.4 * mm, h - 24.2 * mm, half - 20 * mm, 6.4 * mm, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#C99A0B"))
        for r in range(2):
            c.rect(rx + 11 * mm, h - 20.4 * mm - r * 2.9 * mm, half - 22 * mm - r * 8 * mm, 1.3 * mm, stroke=0, fill=1)


class FlowDiagram(Flowable):
    """Two rows of numbered stage boxes with connectors."""

    def __init__(self, stages, width=CW, box_h=13 * mm, cols=4):
        super().__init__()
        self.stages = stages
        self.cols = cols
        self.box_h = box_h
        self.width = width
        rows = (len(stages) + cols - 1) // cols
        self.gap_v = 7 * mm
        self.height = rows * box_h + (rows - 1) * self.gap_v

    def draw(self):
        c = self.canv
        cols, bh = self.cols, self.box_h
        gap_h = 5.2 * mm
        bw = (self.width - (cols - 1) * gap_h) / cols
        rows = (len(self.stages) + cols - 1) // cols

        for idx, (num, title, sub) in enumerate(self.stages):
            r = idx // cols
            col = idx % cols
            x = col * (bw + gap_h)
            y = self.height - (r + 1) * bh - r * self.gap_v

            c.setFillColor(colors.white)
            c.setStrokeColor(BLUE if r == 0 else GREEN)
            c.setLineWidth(0.8)
            c.roundRect(x, y, bw, bh, 1.6 * mm, stroke=1, fill=1)
            c.setFillColor(BLUE if r == 0 else GREEN)
            c.roundRect(x, y + bh - 1.2 * mm, bw, 1.2 * mm, 0.4 * mm, stroke=0, fill=1)
            # number badge
            c.setFillColor(BLUE if r == 0 else GREEN)
            c.circle(x + 3.6 * mm, y + bh - 4.8 * mm, 1.9 * mm, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont(FB, 5.4)
            c.drawCentredString(x + 3.6 * mm, y + bh - 5.5 * mm, str(num))
            c.setFillColor(NAVY)
            c.setFont(FB, 6.6)
            c.drawString(x + 6.8 * mm, y + bh - 5.8 * mm, title)
            c.setFillColor(SLATE)
            c.setFont(F, 5.3)
            wrapped = self._wrap(sub, bw - 5 * mm, F, 5.3)
            yy = y + bh - 9 * mm
            for ln in wrapped[:2]:
                c.drawString(x + 2.6 * mm, yy, ln)
                yy -= 3.0 * mm

            # arrow to the next box in the row
            if col < cols - 1 and idx + 1 < len(self.stages):
                ax = x + bw + 0.9 * mm
                ay = y + bh / 2
                c.setStrokeColor(colors.HexColor("#C3C9D4"))
                c.setLineWidth(0.9)
                c.line(ax, ay, ax + gap_h - 3.2 * mm, ay)
                c.setFillColor(colors.HexColor("#C3C9D4"))
                p = c.beginPath()
                p.moveTo(ax + gap_h - 3.4 * mm, ay + 1.1 * mm)
                p.lineTo(ax + gap_h - 1.4 * mm, ay)
                p.lineTo(ax + gap_h - 3.4 * mm, ay - 1.1 * mm)
                p.close()
                c.drawPath(p, stroke=0, fill=1)

        # wrap connector: bottom of the last box in row 1 -> top of the first in row 2
        if rows > 1:
            row1_bottom = self.height - bh
            row2_top = self.height - bh - self.gap_v
            mid = row2_top + self.gap_v / 2
            x_end, x_start = self.width - bw / 2, bw / 2
            c.setStrokeColor(colors.HexColor("#C3C9D4"))
            c.setLineWidth(0.9)
            c.setDash(1.6, 1.6)
            c.line(x_end, row1_bottom, x_end, mid)
            c.line(x_end, mid, x_start, mid)
            c.line(x_start, mid, x_start, row2_top + 1.9 * mm)
            c.setDash()
            c.setFillColor(colors.HexColor("#C3C9D4"))
            p = c.beginPath()
            p.moveTo(x_start - 1.1 * mm, row2_top + 2.0 * mm)
            p.lineTo(x_start, row2_top + 0.2 * mm)
            p.lineTo(x_start + 1.1 * mm, row2_top + 2.0 * mm)
            p.close()
            c.drawPath(p, stroke=0, fill=1)

    @staticmethod
    def _wrap(text, max_w, font, size):
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if pdfmetrics.stringWidth(trial, font, size) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines


class QuickFlow(Flowable):
    """Vertical numbered flow for the quick-reference page."""

    def __init__(self, items, width=CW * 0.52):
        super().__init__()
        self.items = items
        self.width = width
        self.row_h = 8.2 * mm
        self.gap = 3.4 * mm
        self.height = len(items) * self.row_h + (len(items) - 1) * self.gap

    def draw(self):
        c = self.canv
        for i, text in enumerate(self.items):
            y = self.height - (i + 1) * self.row_h - i * self.gap
            c.setFillColor(colors.white)
            c.setStrokeColor(BLUE if i % 2 == 0 else GREEN)
            c.setLineWidth(0.8)
            c.roundRect(0, y, self.width, self.row_h, 1.6 * mm, stroke=1, fill=1)
            c.setFillColor(BLUE if i % 2 == 0 else GREEN)
            c.roundRect(0, y, 1.4 * mm, self.row_h, 0.5 * mm, stroke=0, fill=1)
            c.setFillColor(BLUE if i % 2 == 0 else GREEN)
            c.circle(6.6 * mm, y + self.row_h / 2, 2.4 * mm, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont(FB, 6.4)
            c.drawCentredString(6.6 * mm, y + self.row_h / 2 - 2.2, str(i + 1))
            c.setFillColor(NAVY)
            c.setFont(FB, 7.6)
            c.drawString(11.6 * mm, y + self.row_h / 2 - 2.6, text)
            if i < len(self.items) - 1:
                c.setFillColor(colors.HexColor("#C3C9D4"))
                p = c.beginPath()
                cx = self.width / 2
                p.moveTo(cx - 1.6 * mm, y - 0.9 * mm)
                p.lineTo(cx + 1.6 * mm, y - 0.9 * mm)
                p.lineTo(cx, y - 2.9 * mm)
                p.close()
                c.drawPath(p, stroke=0, fill=1)


# ---------------------------------------------------------------------------
# Page furniture: watermark, header, footer
# ---------------------------------------------------------------------------
def draw_watermark(c):
    c.saveState()
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(38)
    c.setFillColor(WATERMARK)
    c.setFont(FB, 38)
    c.drawCentredString(0, -12, ORG)
    c.setFont(F, 10.5)
    c.setFillColor(colors.HexColor("#F1F4F8"))
    c.drawCentredString(0, -28, "iRIS CLEAR  \u00b7  User Manual")
    c.restoreState()


def cover_page(c, doc):
    c.saveState()
    band_h = 96 * mm
    c.setFillColor(NAVY)
    c.rect(0, PAGE_H - band_h, PAGE_W, band_h, stroke=0, fill=1)
    # subtle diagonal texture in the band
    c.saveState()
    c.setStrokeColor(colors.HexColor("#1B2A4A"))
    c.setLineWidth(0.8)
    for i in range(-8, 26):
        x = i * 12 * mm
        c.line(x, PAGE_H - band_h, x + band_h, PAGE_H)
    c.restoreState()
    # accent bar
    c.setFillColor(BLUE)
    c.rect(0, PAGE_H - band_h, PAGE_W * 0.42, 2.2 * mm, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.rect(PAGE_W * 0.42, PAGE_H - band_h, PAGE_W * 0.18, 2.2 * mm, stroke=0, fill=1)
    c.restoreState()

    draw_watermark(c)

    # Footer rule on the cover
    c.saveState()
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(ML, 18 * mm, PAGE_W - MR, 18 * mm)
    c.setFillColor(MUTED)
    c.setFont(F, 7.2)
    c.drawString(ML, 13.6 * mm, f"{ORG}  \u00b7  Confidential \u2014 for licensed users and internal distribution")
    c.drawRightString(PAGE_W - MR, 13.6 * mm, DOC_DATE)
    c.restoreState()


def body_page(c, doc):
    draw_watermark(c)
    c.saveState()
    # header
    c.setFillColor(BLUE)
    c.setFont(FB, 6.6)
    c.drawString(ML, PAGE_H - 13.6 * mm, "IRIS  REGTECH  SOLUTION")
    c.setFillColor(MUTED)
    c.setFont(F, 7.2)
    c.drawRightString(PAGE_W - MR, PAGE_H - 13.6 * mm, "iRIS CLEAR \u2014 User Manual")
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(ML, PAGE_H - 15.8 * mm, PAGE_W - MR, PAGE_H - 15.8 * mm)
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.4)
    c.line(ML, PAGE_H - 15.8 * mm, ML + 22 * mm, PAGE_H - 15.8 * mm)
    c.restoreState()


class NumberedCanvas:
    """Deferred page numbering: total page count is only known after the build."""

    pass


from reportlab.pdfgen import canvas as _canvas  # noqa: E402


class FooterCanvas(_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        self.saveState()
        self.setStrokeColor(LINE)
        self.setLineWidth(0.6)
        self.line(ML, 12.6 * mm, PAGE_W - MR, 12.6 * mm)
        self.setFillColor(MUTED)
        self.setFont(F, 6.9)
        self.drawString(ML, 9.0 * mm,
                        f"{ORG}  \u00b7  iRIS CLEAR User Manual  \u00b7  Application version {APP_VERSION}")
        self.setFillColor(SLATE)
        self.setFont(FB, 6.9)
        self.drawRightString(PAGE_W - MR, 9.0 * mm, f"Page {self._pageNumber} of {total}")
        self.restoreState()


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
def build_story():
    st = []

    # =====================================================================
    # PAGE 1 - COVER
    # =====================================================================
    st.append(Spacer(1, 6 * mm))
    st.append(Paragraph(
        f'<font name="{FB}" color="#7FA8F5" size="8">I R I S &nbsp; R E G T E C H &nbsp; S O L U T I O N</font>',
        ParagraphStyle("cv0", fontName=FB, fontSize=8, leading=11, textColor=colors.HexColor("#7FA8F5"))))
    st.append(Spacer(1, 16 * mm))
    st.append(Paragraph("iRIS CLEAR",
                        ParagraphStyle("cv1", fontName=FB, fontSize=42, leading=46,
                                       textColor=colors.white)))
    st.append(Spacer(1, 1.5 * mm))
    st.append(Paragraph("Clause Locator &amp; Executive Agreement Review",
                        ParagraphStyle("cv2", fontName=FL, fontSize=13.5, leading=17,
                                       textColor=colors.HexColor("#7FA8F5"))))
    st.append(Spacer(1, 5 * mm))
    st.append(Paragraph(
        "Contract intelligence for executed agreements \u2014 every clause located, "
        "checked and traceable to the page it came from.",
        ParagraphStyle("cv3", fontName=F, fontSize=10, leading=14.5,
                       textColor=colors.HexColor("#A9B4C7"))))
    st.append(Spacer(1, 26 * mm))

    st.append(Paragraph("User Manual",
                        ParagraphStyle("cv4", fontName=FB, fontSize=27, leading=31, textColor=NAVY)))
    st.append(Spacer(1, 1 * mm))
    st.append(Paragraph(
        "A practical guide to uploading contracts, reviewing extracted clauses, searching "
        "the repository and exporting results.",
        ParagraphStyle("cv5", fontName=F, fontSize=9.6, leading=13.6, textColor=SLATE)))
    st.append(Spacer(1, 8 * mm))

    meta = tbl(
        [
            ["Organization", ORG, "Application", "iRIS CLEAR (Contract Intelligence Platform)"],
            ["Document", "User Manual", "Application version", APP_VERSION],
            ["Date", DOC_DATE, "Manual version", DOC_VERSION],
            ["Audience", "Application Users / Administrators", "Classification", "Confidential"],
        ],
        widths=[26 * mm, 52 * mm, 30 * mm, CW - 108 * mm],
        header=False,
    )
    st.append(meta)
    st.append(Spacer(1, 7 * mm))

    logo = Image(LOGO, width=44 * mm, height=44 * mm * 133 / 364)
    logo.hAlign = "LEFT"
    publisher_block = [
        Paragraph("Published by",
                  ParagraphStyle("ab0", fontName=FB, fontSize=8.4, leading=11, textColor=NAVY)),
        Paragraph(ORG,
                  ParagraphStyle("ab1", fontName=FB, fontSize=11, leading=15, textColor=BLUE)),
        Paragraph("Contract Intelligence Platform",
                  ParagraphStyle("ab2", fontName=F, fontSize=8.4, leading=11, textColor=SLATE)),
    ]
    st.append(two_col(logo, publisher_block, ratio=0.42))
    st.append(Spacer(1, 9 * mm))
    st.append(rule(0, 2.6 * mm))
    st.append(Paragraph("CONTENTS",
                        ParagraphStyle("toc0", fontName=FB, fontSize=7, leading=10, textColor=BLUE,
                                       spaceAfter=1.6 * mm)))

    toc_style = ParagraphStyle("toc", fontName=F, fontSize=8.2, leading=12.4, textColor=INK)
    toc_num = ParagraphStyle("tocn", fontName=FB, fontSize=8.2, leading=12.4, textColor=MUTED)

    def toc_rows(entries):
        data = [[Paragraph(page, toc_num), Paragraph(title, toc_style)] for page, title in entries]
        t = Table(data, colWidths=[8 * mm, (CW - 8 * mm) / 2 - 6 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
            ("TOPPADDING", (0, 0), (-1, -1), 0.5 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5 * mm),
        ]))
        return t

    st.append(two_col(
        toc_rows([
            ("02", "Product Overview &amp; Getting Started"),
            ("03", "Dashboard &amp; Navigation"),
            ("04", "Contracts — Adding Documents and the Register"),
            ("05", "The Contract Review Workspace"),
            ("06", "Finding Answers — Search and Copilot"),
            ("", ""),
        ]),
        toc_rows([
            ("07", "Portfolio, Clause Coverage, Alerts and Processing"),
            ("08", "End-to-End Workflow (1 of 2)"),
            ("09", "End-to-End Workflow (2 of 2)"),
            ("10", "Search, Review, Export &amp; Administration"),
            ("11", "Statuses, Troubleshooting &amp; FAQ"),
            ("12", "Quick Reference, Best Practices &amp; About"),
        ]),
        gap=8 * mm, ratio=0.5))

    st.append(NextPageTemplate("body"))
    st.append(PageBreak())

    # =====================================================================
    # PAGE 2 - PRODUCT OVERVIEW & GETTING STARTED
    # =====================================================================
    st.append(P("SECTION 1", "eyebrow"))
    st.append(P("Product Overview &amp; Getting Started", "h1"))
    st.append(P(
        "iRIS CLEAR is the contract intelligence workspace from Iris Regtech Solution. It reads executed "
        "agreements, locates the clauses inside them, checks each against the clause set its agreement type "
        "should contain, and presents the result as a reviewable record \u2014 with the page every finding came from.",
        "lead"))

    st.append(P("What it is for", "h2"))
    st.append(P(
        "Signed agreements usually end up in shared folders. The obligations, renewal deadlines and liability "
        "exposure inside them only surface when somebody opens the file and reads it — which is slow, uneven "
        "and impossible to do across a whole portfolio at once. iRIS CLEAR turns that library into a searchable "
        "register: it extracts the terms, flags what is risky or missing, and keeps a link back to the source text.",
        "body"))

    st.append(P("Key capabilities", "h2"))
    st.append(tbl(
        [
            ["Capability", "What it gives you"],
            ["Automated extraction",
             "Clauses, parties, obligations, key dates and risks are pulled from each uploaded document."],
            ["Clause master checks",
             "Every document is measured against the clauses its agreement type should contain; absences are flagged."],
            ["Evidence-linked review",
             "Each extracted item opens the source page with the passage outlined, and can be approved, corrected or rejected."],
            ["Search and Copilot",
             "Search in plain language or by exact wording, and ask questions answered only from your contracts, "
             "with a numbered citation behind every claim."],
            ["Portfolio, Alerts and Exports",
             "Obligations, key dates, risks and counterparties across the estate; alerts for expiries and deadlines; "
             "Excel workbooks of exactly the view on screen."],
        ],
        widths=[38 * mm, CW - 38 * mm]))

    st.append(P("Getting started", "h2"))
    st.append(two_col(
        [
            P("Before you begin", "h3"),
            *bullets([
                "A current desktop browser (Chrome, Edge or Firefox).",
                "An account created for you by an administrator.",
                "Membership of at least one <b>Business Unit</b>.",
            ]),
        ],
        [
            P("Signing in and out", "h3"),
            *steps([
                "Open the address supplied by your administrator.",
                "Enter your <b>Email</b> and <b>Password</b> and select <b>Sign in</b>. Where your organisation "
                "has enabled it, <b>Sign in with Microsoft</b> is offered instead.",
                "At a first sign-in you are asked to change the temporary password: enter it, then a new one of "
                "at least 8 characters, and confirm.",
                "To leave, open the circular initials button at the top right and choose <b>Sign out</b>. The "
                "same menu switches between <b>Light</b> and <b>Dark</b>.",
            ]),
        ], ratio=0.34))

    st.append(Spacer(1, 1 * mm))
    st.append(callout(
        "The Business Unit is the boundary",
        "Every contract belongs to exactly one Business Unit, and you only ever see the units you are a member "
        "of. The selector in the top bar narrows that view further; it can never widen it. Check it before "
        "reading any figure \u2014 it decides what every number on screen counts."))

    st.append(P("User roles", "h2"))
    st.append(tbl(
        [
            ["Capability", "Admin", "Contract Manager", "Reviewer", "Viewer"],
            ["View contracts, portfolio, alerts; search, Copilot, download", "Y", "Y", "Y", "Y"],
            ["Upload contracts", "-", "Y", "Y", "-"],
            ["Approve, correct or reject extracted clauses", "Y", "Y", "Y", "-"],
            ["Create exports", "Y", "Y", "Y", "-"],
            ["Retry, cancel or reprocess a document", "Y", "Y", "-", "-"],
            ["Manage Business Unit members; view the Activity log", "Y", "Y", "-", "-"],
            ["Manage users, Business Units and Clause Master", "Y", "-", "-", "-"],
        ],
        widths=[CW - 4 * 21 * mm, 21 * mm, 21 * mm, 21 * mm, 21 * mm],
        aligns={1: "CENTER", 2: "CENTER", 3: "CENTER", 4: "CENTER"},
        pad=1.5))
    st.append(P(
        "Roles are granted per Business Unit, so the same person can be a Contract Manager in one and a Viewer in "
        "another. Administrators deliberately cannot upload contracts \u2014 that is project-member work.", "small"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 3 - DASHBOARD & NAVIGATION
    # =====================================================================
    st.append(P("SECTION 2", "eyebrow"))
    st.append(P("Dashboard &amp; Navigation", "h1"))
    st.append(P(
        "The Dashboard is the landing screen and the daily starting point: it answers \u201cwhat needs my "
        "attention in this Business Unit today\u201d. Every headline figure is clickable and opens the "
        "contracts that produced it, so a number is never a dead end.", "lead"))

    st.append(ShellDiagram())
    st.append(P("Interface layout \u2014 sidebar, top bar and the Dashboard content area. "
                "Figures in this manual are layout diagrams, not screen captures.", "caption"))

    st.append(two_col(
        [
            P("Headline figures", "h3"),
            tbl(
                [
                    ["Tile", "What it counts"],
                    ["Contracts", "Everything in view."],
                    ["Expiring in 90 days", "Contracts whose end date falls inside the window."],
                    ["High risk", "Contracts scored in the high risk band."],
                    ["Unlimited liability", "No liability cap, or carve-outs that defeat it."],
                    ["Missing mandatory clauses", "Expected clauses that were not found."],
                    ["Needs review", "Extractions waiting on a human decision."],
                    ["Clauses extracted", "Total clauses located across the view."],
                ],
                widths=[30 * mm, 52 * mm], pad=1.4),
        ],
        [
            P("Panels below the figures", "h3"),
            *bullets([
                "<b>Needs attention</b> \u2014 a ribbon of the non-zero action figures; select one to jump straight to those contracts.",
                "<b>Renewal watchlist</b> \u2014 closest expiry first, with the notice deadline and whether the contract auto-renews.",
                "<b>Agreement types</b> \u2014 what kinds of contract the repository holds.",
                "<b>Risk distribution</b> \u2014 contracts by risk band.",
                "<b>Contract status</b> \u2014 how much of the estate has finished processing.",
            ]),
        ], ratio=0.49))

    st.append(P("Moving between modules", "h2"))
    st.append(two_col(
        tbl(
            [
                ["Screen", "Purpose"],
                ["Dashboard", "Headline figures and what needs attention."],
                ["Upload", "Add documents to a Business Unit."],
                ["Contracts", "The contract register, filtered and sorted."],
                ["Portfolio", "The same corpus by obligation, date, risk and counterparty."],
                ["Search", "Find passages by meaning or exact wording."],
                ["Copilot", "Ask questions across the repository."],
                ["Processing", "Watch documents move through the six stages."],
            ],
            widths=[24 * mm, 58 * mm], pad=1.3),
        tbl(
            [
                ["Screen", "Purpose"],
                ["Clause Coverage", "How much of each type's expected clause set was found."],
                ["Alerts", "Expiries, notice windows and obligation deadlines."],
                ["Exports", "Workbooks you have requested."],
                ["Business Unit \u00b7 <font color='#8B96AC'>Admin</font>", "Create Business Units and manage their members."],
                ["Users \u00b7 <font color='#8B96AC'>Admin</font>", "Create accounts and assign them to Business Units."],
                ["Clause Master \u00b7 <font color='#8B96AC'>Admin</font>", "Which clauses each agreement type is checked for."],
                ["Activity", "Who did what, and when."],
            ],
            widths=[27 * mm, 55 * mm], pad=1.3),
        ratio=0.49))

    st.append(Spacer(1, 1.4 * mm))
    st.append(P(
        "The sidebar shows only the screens your role can use, so an administrator sees the Administration "
        "group and no Upload link, while a member sees the reverse. Collapse the sidebar with the chevron at "
        "its foot to widen the working area.", "small"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 4 - UPLOAD + CONTRACTS REGISTER
    # =====================================================================
    st.append(P("SECTION 3", "eyebrow"))
    st.append(P("Contracts \u2014 Adding Documents and Working the Register", "h1"))

    st.append(P("3.1  Upload contracts", "h2"))
    st.append(P(
        "<b>Purpose.</b> Bring executed agreements into a Business Unit. Processing starts on its own the "
        "moment a file is accepted \u2014 there is no separate \u201cprocess\u201d step to remember.", "body"))
    st.append(two_col(
        [
            P("How to use", "h3"),
            *steps([
                "Select <b>Upload</b> in the sidebar.",
                "Under <b>Destination</b>, choose the <b>Project</b> these documents join. This is required, and it "
                "decides who can see them.",
                "Drag files onto the drop area, or select it to browse. Several at once is fine.",
                "Select <b>Upload <i>n</i> files</b>.",
                "When the list shows the files as accepted, select <b>Watch processing</b> to follow them.",
            ]),
        ],
        [
            P("Key actions", "h3"),
            tbl(
                [
                    ["Action", "Description"],
                    ["Choose destination", "Sets the Business Unit the documents belong to."],
                    ["Add files", "PDF, Word (.doc, .docx) or a .zip of them, up to 50 MB each."],
                    ["Upload", "Stores the files and queues them for processing."],
                    ["Clear list", "Empties the local list; it does not delete anything uploaded."],
                    ["Open", "Opens a finished file, or the contract a duplicate matched."],
                ],
                widths=[24 * mm, 58 * mm], pad=1.3),
        ], ratio=0.49))
    st.append(Spacer(1, 0.6 * mm))
    st.append(P(
        "<b>Expected result.</b> Each row turns green and reads <i>Uploaded \u2014 processing queued</i>. A ZIP "
        "expands into one contract per document inside it. A file already in the repository is marked as a "
        "duplicate and links to the existing contract rather than creating a second copy. Word documents are "
        "converted to PDF automatically, because evidence highlighting needs a page to point at.", "body"))
    st.append(callout(
        "Before you upload",
        "Check the Project field first. A contract placed in the wrong Business Unit is visible to the wrong "
        "people, and moving it afterwards is not a self-service action. Administrators do not see this screen "
        "at all \u2014 uploading is done by Contract Managers and Reviewers.", "amber"))

    st.append(P("3.2  The Contracts register", "h2"))
    st.append(P(
        "<b>Purpose.</b> One row per contract, with the status, risk, value and expiry that were extracted from it. "
        "This is where most reviewing days start and where exports are produced.", "body"))
    st.append(two_col(
        [
            P("How to use", "h3"),
            *steps([
                "Select <b>Contracts</b> in the sidebar.",
                "Type in the search box to narrow by title, party or file name.",
                "Refine with the <b>Status</b> and <b>Risk</b> chips and the three toggles.",
                "Sort with the dropdown, or by selecting any column heading.",
                "Select a row to open the contract.",
            ]),
        ],
        [
            P("Key actions", "h3"),
            tbl(
                [
                    ["Action", "Description"],
                    ["Search", "Matches title, counterparty and original file name."],
                    ["Filter", "Status and risk chips; toggles for Needs review, Unlimited liability and Missing mandatory clauses."],
                    ["Sort", "Newest, oldest, highest risk, expiring soonest or title A\u2013Z."],
                    ["Export", "Builds an Excel workbook of exactly this filtered view."],
                    ["Clear all", "Removes every active filter at once."],
                ],
                widths=[20 * mm, 62 * mm], pad=1.3),
        ], ratio=0.49))
    st.append(Spacer(1, 0.6 * mm))
    st.append(P(
        "<b>Expected result.</b> The strip above the table reports the total, how many filters are active and how "
        "many rows of the total are showing. Filters are held in the page address, so a filtered view can be "
        "bookmarked or sent to a colleague and will open the same way for them.", "body"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 5 - CONTRACT DETAIL
    # =====================================================================
    st.append(P("SECTION 4", "eyebrow"))
    st.append(P("The Contract Review Workspace", "h1"))
    st.append(P(
        "<b>Purpose.</b> Read what was extracted from one agreement beside the document it came from. The left "
        "pane holds the extracted record; the right pane is the source document. Selecting <b>Evidence</b> on any "
        "item moves the viewer to that page and outlines the passage.", "lead"))

    st.append(SplitDiagram())
    st.append(P("Interface layout \u2014 the extracted record (left) beside the source document with the "
                "cited passage outlined (right).", "caption"))

    st.append(two_col(
        [
            P("How to use", "h3"),
            *steps([
                "Open a contract from the register, the Dashboard or a search result.",
                "Work through the tabs on the left pane.",
                "Select <b>Evidence</b> on a clause, risk or obligation to see it in the document.",
                "Correct the text or fields if the extraction is wrong, then select <b>Approve</b>; "
                "select <b>Reject</b> if the item does not belong.",
                "Use <b>Export</b> to download this contract's extracted record as a CSV report, or "
                "<b>Copilot</b> to ask questions about this agreement alone.",
            ]),
        ],
        [
            P("What each tab holds", "h3"),
            tbl(
                [
                    ["Tab", "Contents"],
                    ["Overview", "Attention items, summary, commercial terms, source document and downloads."],
                    ["Risks", "Identified risks with severity and evidence."],
                    ["Obligations", "Who owes what, and by when."],
                    ["Key dates", "Effective, execution, expiry and notice dates."],
                    ["Parties", "The counterparties named in the agreement."],
                    ["Clause tabs", "One per clause category. A mandatory clause that was not found still gets a tab, marked as missing."],
                    ["Graph", "How this contract's extracted facts relate to one another."],
                    ["Processing", "Every stage this document ran, with timings and any error."],
                ],
                widths=[22 * mm, 60 * mm], pad=1.25),
        ], ratio=0.47))

    st.append(Spacer(1, 0.8 * mm))
    st.append(two_col(
        [
            P("Reviewing an extraction", "h3"),
            tbl(
                [
                    ["Action", "Description"],
                    ["Evidence", "Jumps the viewer to the page and outlines the passage."],
                    ["Approve", "Accepts the item. If you edited it first, it is recorded as a correction, not an overwrite."],
                    ["Reject", "Marks the item as not applicable to this contract."],
                    ["Download", "The processed PDF, and the original file where it was converted."],
                ],
                widths=[20 * mm, 62 * mm], pad=1.3),
        ],
        [
            P("Reading the badges", "h3"),
            *bullets([
                "<b>Page <i>n</i></b> \u2014 where in the document the item was found.",
                "<b>Confidence</b> \u2014 how sure the extraction is. Green is high, amber is worth checking, red should be verified before you rely on it.",
                "<b>Needs review</b> \u2014 the contract carries at least one item a person has not yet decided on; the banner names the reasons.",
                "<b>Missing</b> \u2014 a clause the agreement type should contain was not located. Each absence contributes to the risk score.",
            ]),
        ], ratio=0.49))

    st.append(Spacer(1, 0.8 * mm))
    st.append(callout(
        "Always verify what matters against the source",
        "Extracted clauses, summaries, risk scores and Copilot answers are produced automatically and are not "
        "guaranteed to be correct or complete. Before relying on any finding for a decision, an obligation or "
        "external advice, open its <b>Evidence</b> and read the passage in the document itself. The source "
        "document is the record; the extraction is a fast route to it.", "rose"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 6 - SEARCH & COPILOT
    # =====================================================================
    st.append(P("SECTION 5", "eyebrow"))
    st.append(P("Finding Answers \u2014 Search and Copilot", "h1"))

    st.append(P("5.1  Search", "h2"))
    st.append(P(
        "<b>Purpose.</b> Find the passages that answer a question, across every contract in view. Search returns "
        "the matching contracts and the passages themselves, so you can judge relevance before opening anything.",
        "body"))
    st.append(two_col(
        [
            P("How to use", "h3"),
            *steps([
                "Select <b>Search</b> in the sidebar.",
                "Type a question or a phrase, or pick one of the suggested examples.",
                "Choose a mode, then select <b>Search</b>.",
                "Open <b>Retrieval plan</b> if the results look wrong \u2014 it shows how the question was read.",
                "Select a result to open that contract at the passage.",
            ]),
        ],
        [
            P("Search modes", "h3"),
            tbl(
                [
                    ["Mode", "Use it when"],
                    ["Hybrid", "The default. Fuses meaning and exact wording; best for most questions."],
                    ["Semantic", "You want paraphrases \u2014 the idea, however it is worded."],
                    ["Keyword", "You need a literal phrase, a defined term or a reference number."],
                ],
                widths=[20 * mm, 62 * mm], pad=1.4),
            Spacer(1, 1.2 * mm),
            P("<b>Expected result.</b> A list of matching contracts, then the passages, each showing the contract, "
              "page and how it was retrieved. When nothing matches, the retrieval plan is shown instead of an empty "
              "screen \u2014 rephrasing, or switching to Keyword, usually fixes it.", "small"),
        ], ratio=0.44))

    st.append(P("5.2  Copilot", "h2"))
    st.append(P(
        "<b>Purpose.</b> Ask a question in plain language and get a written answer assembled from your contracts, "
        "with a numbered citation behind every claim. Copilot answers only from the documents in view; when they "
        "do not say, it says so rather than guessing.", "body"))
    st.append(two_col(
        [
            P("How to use", "h3"),
            *steps([
                "Select <b>Copilot</b>, or open the <b>Copilot</b> button on a contract to ask about that agreement alone.",
                "Type a question, or select one of the suggested starters.",
                "Optionally choose an answer format.",
                "Select <b>Ask</b>. The answer streams in; <b>Stop</b> ends it early.",
                "Select any citation to open the contract at the cited page.",
            ]),
            Spacer(1, 0.8 * mm),
            P("Past conversations are listed alongside, so a thread can be reopened and followed up.", "small"),
        ],
        [
            P("Answer formats", "h3"),
            tbl(
                [
                    ["Format", "Shape of the answer"],
                    ["Automatic", "Copilot chooses to suit the question."],
                    ["Executive summary", "A short narrative for a non-specialist reader."],
                    ["Risk report", "Exposures, grouped and ranked."],
                    ["Action items", "What has to be done, by whom."],
                    ["Timeline", "Dates and deadlines in order."],
                ],
                widths=[26 * mm, 56 * mm], pad=1.3),
        ], ratio=0.47))

    st.append(Spacer(1, 1 * mm))
    st.append(P("Three answers that are not errors", "h3"))
    st.append(tbl(
        [
            ["What you see", "What it means", "What to do"],
            ["\u201cThe supplied contracts do not say\u2026\u201d",
             "The documents in view contain no evidence for the question.",
             "Widen the Business Unit selector, or confirm the relevant contract has finished processing."],
            ["<b>This answer needs review</b>",
             "A citation could not be tied back to the retrieved evidence.",
             "Read the answer as a lead only, and verify it in the source document before relying on it."],
            ["<b>No citations</b>",
             "An answer was produced without evidence attached to it.",
             "Treat it as unverified. Re-ask more specifically, or search for the passage directly."],
        ],
        widths=[42 * mm, 55 * mm, CW - 97 * mm], pad=1.6))

    st.append(Spacer(1, 1 * mm))
    st.append(callout(
        None,
        "Copilot and Search read only the contracts that have finished processing in the Business Units you belong "
        "to. A contract still in Processing is not yet searchable, and its clauses are not yet part of any answer.",
        "blue"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 7 - PORTFOLIO, COVERAGE, ALERTS, PROCESSING
    # =====================================================================
    st.append(P("SECTION 6", "eyebrow"))
    st.append(P("Portfolio, Clause Coverage, Alerts and Processing", "h1"))
    st.append(P(
        "These four screens read the same corpus from four different angles: by commitment, by completeness, by "
        "deadline, and by what the platform is currently doing to each document.", "lead"))

    st.append(P("6.1  Portfolio \u2014 the cross-contract registers", "h2"))
    st.append(two_col(
        [
            P("<b>Purpose.</b> Everything the estate commits you to, listed independently of which document it came "
              "from. Four tabs: <b>Obligations</b>, <b>Key dates</b>, <b>Risks</b> and <b>Counterparties</b>.", "body"),
            P("<b>How to use.</b> Select <b>Portfolio</b>, choose a tab, filter and sort, then page through the list. "
              "Every row links back to the contract it came from.", "body"),
        ],
        [
            P("<b>Expected result.</b> A working list \u2014 obligations with their owner and due date, key dates in "
              "order, risks by severity, counterparties with the contracts they appear in.", "body"),
            P("Use this when the question is \u201cwhat is due next month\u201d rather than \u201cwhat does this "
              "contract say\u201d.", "small"),
        ], ratio=0.5))

    st.append(P("6.2  Clause Coverage \u2014 how complete the extraction is", "h2"))
    st.append(two_col(
        [
            P("<b>Purpose.</b> How much of each document type's expected clause set was actually located, and which "
              "clauses were never found in any document of that type.", "body"),
            P("<b>How to use.</b> Select <b>Clause Coverage</b> and read the four figures \u2014 <b>Documents</b>, "
              "<b>Clauses located</b>, <b>Average coverage</b> and <b>Embedded</b> \u2014 then work down the tables.", "body"),
        ],
        [
            P("<b>Expected result.</b> A per-document coverage percentage, a count of the documents each clause type "
              "was found in, and a <b>Never located</b> list \u2014 clauses the taxonomy expects that no document has "
              "yet produced.", "body"),
            P("A low coverage figure is a prompt to check the document itself, not proof that a clause is absent.", "small"),
        ], ratio=0.5))

    st.append(P("6.3  Alerts \u2014 deadlines and exposures", "h2"))
    st.append(two_col(
        tbl(
            [
                ["Alert type", "Raised when"],
                ["Contract expiring", "The end date falls inside the warning window."],
                ["Auto renewal notice", "A notice deadline is approaching on an auto-renewing contract."],
                ["Obligation due", "An obligation's due date is near, or it has been missed."],
                ["High risk", "The risk score reaches the configured threshold."],
                ["Missing mandatory clause", "Expected clauses were not found in a document."],
                ["Review required", "Items are waiting on a human decision."],
                ["Processing failed", "A document could not be processed."],
            ],
            widths=[32 * mm, 50 * mm], pad=1.25),
        [
            P("How to use", "h3"),
            *steps([
                "Select <b>Alerts</b>. Open alerts show first, most severe at the top.",
                "Use the status chips to include <b>Acknowledged</b>, <b>Resolved</b> or <b>Dismissed</b>.",
                "Select <b>Acknowledge</b> to take ownership, <b>Resolve</b> once handled, or <b>Dismiss</b> if it "
                "does not apply. A note can be added.",
                "Administrators can open the <b>Rules</b> tab to set the warning windows, thresholds and severities "
                "each alert type uses.",
            ]),
        ], ratio=0.49))

    st.append(P("6.4  Processing \u2014 what the platform is doing", "h2"))
    st.append(two_col(
        [
            P("<b>Purpose.</b> Follow each document through the six stages of processing, and act when one fails. "
              "Every stage checkpoints, so a retry resumes from the failed stage rather than starting again.", "body"),
            P("<b>How to use.</b> Select <b>Processing</b>, filter by state if the list is long, and expand a document "
              "to see its stage table \u2014 stage, status, attempt, duration and notes.", "body"),
        ],
        tbl(
            [
                ["Action", "Description"],
                ["Retry", "Offered when a retry could plausibly help; resumes from the failing stage."],
                ["Cancel", "Stops a run that is still in progress."],
                ["Reprocess from", "Re-runs from a chosen stage onwards \u2014 useful after a Clause Master change."],
            ],
            widths=[24 * mm, 58 * mm], pad=1.4),
        ratio=0.5))
    st.append(P(
        "The figures at the top of the screen \u2014 <b>In flight</b>, <b>Stalled</b>, <b>Dead letter</b> and "
        "<b>Stages registered</b> \u2014 describe the platform rather than any one document. A non-zero "
        "<b>Dead letter</b> count, or a <b>Stages registered</b> figure below the full set, is a platform issue: "
        "report it to your administrator rather than retrying documents individually.", "small"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 8 - END-TO-END WORKFLOW (1)
    # =====================================================================
    st.append(P("SECTION 7", "eyebrow"))
    st.append(P("End-to-End Workflow \u2014 From Signed PDF to Exported Register", "h1"))
    st.append(P(
        "This is the journey the product is built around. It begins with a signed agreement on disk and ends "
        "with a reviewed, exportable record that anyone can trace back to the page it came from.", "lead"))

    st.append(FlowDiagram([
        (1, "Sign in", "Choose the Business Unit you are working in"),
        (2, "Upload", "Add PDF, Word or ZIP to a chosen project"),
        (3, "Processing", "Six automatic stages, watched on Processing"),
        (4, "Open", "The contract opens beside its source document"),
        (5, "Review", "Check clauses against the evidence; approve or reject"),
        (6, "Search / Ask", "Query the repository or ask Copilot"),
        (7, "Verify", "Confirm anything material in the source text"),
        (8, "Export", "Build the workbook and download it"),
    ]))
    st.append(P("The primary workflow. Stages 1\u20134 bring a document in; stages 5\u20138 turn it into a "
                "decision you can defend.", "caption"))

    st.append(P("Stages 1 to 4 \u2014 getting the document in", "h2"))
    st.append(tbl(
        [
            ["Stage", "Your action", "What the system does", "Expected result"],
            ["1. Sign in",
             "Enter your email and password, or use Sign in with Microsoft. Set the Business Unit in the top bar.",
             "Restores your session and limits everything on screen to the Business Units you belong to.",
             "The Dashboard opens, showing figures for the selected Business Unit."],
            ["2. Upload",
             "Select <b>Upload</b>, choose the destination project, add the files and select <b>Upload</b>.",
             "Checks the file type and size, stores it, converts Word to PDF where needed, and queues it. "
             "A ZIP becomes one contract per document; a repeat file is flagged as a duplicate.",
             "Each row reads <i>Uploaded \u2014 processing queued</i>, and <b>Watch processing</b> appears."],
            ["3. Processing",
             "Select <b>Watch processing</b>, or open <b>Processing</b> later. No action is needed while it runs.",
             "Runs the six stages in order, checkpointing each one. The contract's status moves from "
             "<b>Uploaded</b> to <b>Processing</b> and then to <b>Ready</b> or <b>Needs review</b>.",
             "A progress bar with the current stage; the document becomes readable before extraction finishes."],
            ["4. Open the contract",
             "Select the contract from <b>Processing</b>, the register or the Dashboard.",
             "Loads the extracted record beside the source document.",
             "The review workspace, with tabs for risks, obligations, key dates, parties and each clause category."],
        ],
        widths=[22 * mm, 46 * mm, 55 * mm, CW - 123 * mm], pad=1.7))

    st.append(P("What happens during processing", "h2"))
    st.append(P(
        "The six stages below are what the <b>Processing</b> screen names. You do not start them \u2014 they run in "
        "order automatically \u2014 but knowing what each one produces makes a failure much easier to interpret.", "body"))
    st.append(tbl(
        [
            ["Stage", "What it produces", "If it fails"],
            ["Validation", "Confirms the file really is a supported document and is readable.",
             "The file is corrupt, empty or not the type its name claims. Re-save it and upload again."],
            ["Parser", "Reads the pages, including scanned ones, and recovers the text and layout.",
             "The scan may be too poor to read. Try a higher-quality copy of the document."],
            ["Clause Detection", "Identifies the agreement type and locates the clauses it should contain.",
             "Usually recoverable \u2014 select <b>Retry</b>, which resumes from this stage."],
            ["Extraction", "Turns the located text into clauses, parties, obligations, risks and key dates.",
             "Retry. If it fails repeatedly, report the document to your administrator."],
            ["Embedding", "Makes the document's text searchable by meaning.",
             "The contract is readable but will not appear in semantic search or Copilot answers until this succeeds."],
            ["Indexing", "Links the extracted facts to one another so related items can be found together.",
             "Search still works; the relationship graph on the contract will be thinner."],
        ],
        widths=[26 * mm, 62 * mm, CW - 88 * mm], pad=1.6))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 9 - END-TO-END WORKFLOW (2)
    # =====================================================================
    st.append(P("SECTION 7 \u00b7 CONTINUED", "eyebrow"))
    st.append(P("End-to-End Workflow \u2014 Review, Verify and Export", "h1"))

    st.append(tbl(
        [
            ["Stage", "Your action", "What the system does", "Expected result"],
            ["5. Review",
             "Work the tabs. Select <b>Evidence</b> on each material clause, correct anything wrong, then "
             "<b>Approve</b> or <b>Reject</b>.",
             "Moves the viewer to the cited page and outlines the passage. An edited item is recorded as a "
             "correction against the original rather than replacing it.",
             "The item shows its decision, and the <b>Needs review</b> banner clears once nothing is outstanding."],
            ["6. Search or ask",
             "Use <b>Search</b> for passages, or <b>Copilot</b> for a written answer. Use the Copilot button on a "
             "contract to confine the question to that agreement.",
             "Retrieves the passages that match, and assembles an answer citing them.",
             "Results with contract and page, or an answer with numbered citations that open the cited page."],
            ["7. Verify",
             "Open the citation or the evidence for anything you intend to act on.",
             "Opens the source document at the cited page with the passage outlined.",
             "You have read the actual contract wording, not only the extracted summary of it."],
            ["8. Export",
             "Set the filters you want on <b>Contracts</b>, select <b>Export</b>, choose the record types and "
             "confirm.",
             "Builds an Excel workbook of exactly that filtered view, one worksheet per record type, outside "
             "your session.",
             "The job appears on <b>Exports</b> as <b>Queued</b>, then <b>Running</b>, then <b>Completed</b> with "
             "a download link."],
        ],
        widths=[22 * mm, 46 * mm, 55 * mm, CW - 123 * mm], pad=1.7))

    st.append(P("Following a document through processing", "h2"))
    st.append(two_col(
        [
            P("States you will see on the Processing screen", "h3"),
            tbl(
                [
                    ["State", "Meaning"],
                    ["Queued", "Waiting for capacity."],
                    ["Validating · Parsing", "Reading the file and its pages."],
                    ["AI extraction", "Locating clauses and extracting the terms."],
                    ["Embedding · Indexing", "Making the document searchable."],
                    ["Ready", "Every stage completed."],
                    ["Retrying · Failed", "A stage is being reattempted, or could not be recovered."],
                    ["Cancelled · Paused", "Stopped by a person."],
                ],
                widths=[26 * mm, 56 * mm], pad=1.2, dense=True),
        ],
        [
            P("While a document is processing", "h3"),
            *bullets([
                "The screen updates on its own — do not refresh, and do not upload the file again.",
                "The source document is readable as soon as parsing finishes; the extracted tabs fill in later.",
                "A failed stage names the error. <b>Retry</b> resumes from that stage; earlier stages are not repeated.",
                "<b>Reprocess from</b> re-runs from a stage you choose — use it after a Clause Master change.",
            ]),
        ], ratio=0.5))

    st.append(P("Before you call a contract done", "h2"))
    st.append(two_col(
        tbl(
            [
                ["Check", "Where to confirm it"],
                ["The contract reads <b>Ready</b>, not <b>Needs review</b> or <b>Failed</b>.",
                 "Contracts, or the banner at the top of the contract."],
                ["No mandatory clause is still flagged as missing.",
                 "The clause tabs, and <b>Attention</b> on Overview."],
                ["Every low-confidence item you rely on has been read in the document.",
                 "The confidence badge, then <b>Evidence</b>."],
                ["Expiry, notice period and auto-renewal are right, and anything requiring action has an owner.",
                 "Overview \u203a Commercial terms, then Portfolio \u203a Obligations and Alerts."],
            ],
            widths=[52 * mm, 46 * mm], pad=1.4),
        [
            P("Why this step exists", "h3"),
            P("The platform makes a contract reviewable in minutes; it does not replace the review. Extraction is "
              "confident on well-structured agreements and less so on poor scans or unusual drafting.", "body"),
            P("The checks opposite separate \u201cprocessing completed\u201d from \u201csomeone has read it\u201d, and are what makes "
              "the register safe to circulate and rely on.", "body"),
        ], ratio=0.6))

    st.append(Spacer(1, 1 * mm))
    st.append(callout(
        "The workflow is not finished at \u201cCompleted\u201d",
        "An export is a snapshot of extracted data at the moment it was built. Where the workbook will be "
        "circulated, relied on in negotiation or used as the basis for advice, verify the material rows against "
        "the source documents first \u2014 the platform records confidence and evidence precisely so that this "
        "check takes seconds rather than an afternoon.", "rose"))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 10 - SEARCH, REVIEW, EXPORT & ADMINISTRATION
    # =====================================================================
    st.append(P("SECTION 8", "eyebrow"))
    st.append(P("Search, Review, Export &amp; Administration \u2014 Reference", "h1"))

    st.append(P("8.1  Searching, filtering and sorting", "h2"))
    st.append(tbl(
        [
            ["Screen", "Search", "Filters", "Sort &amp; paging"],
            ["Contracts", "Title, counterparty or file name.",
             "Status and risk chips; Needs review, Unlimited liability, Missing mandatory clauses.",
             "Five sort orders, or select any column heading. Paged."],
            ["Portfolio", "Within the selected tab.", "Per-tab filters on each register.", "Paged."],
            ["Search", "The question or phrase itself.", "Hybrid, Semantic or Keyword mode.",
             "Ranked by relevance."],
            ["Alerts \u00b7 Exports \u00b7 Activity", "\u2014",
             "Alert status; export status; action type and outcome.", "Most severe or newest first. Paged."],
        ],
        widths=[24 * mm, 32 * mm, 62 * mm, CW - 118 * mm], pad=1.5))

    st.append(P("8.2  Reviewing and verifying", "h2"))
    st.append(two_col(
        tbl(
            [
                ["Action", "Where", "Effect"],
                ["Evidence", "Any clause, risk or obligation", "Opens the source page with the passage outlined."],
                ["Approve", "Clause card", "Accepts the item; an edit is recorded as a correction."],
                ["Reject", "Clause card", "Marks the item as not applicable."],
                ["Citation", "Copilot answer", "Opens the cited contract at the cited page."],
            ],
            widths=[20 * mm, 30 * mm, 52 * mm], pad=1.3),
        [
            P("Download and read the source", "h3"),
            *bullets([
                "<b>Download document</b> on the Overview tab gives the processed PDF; where a Word file was "
                "converted, <b>Download original</b> gives the file as uploaded.",
                "Evidence is positioned against the converted PDF, so that is what the viewer shows.",
                "Page controls and zoom sit above the document; pages carrying evidence are indicated.",
            ]),
        ], ratio=0.62))

    st.append(P("8.3  Exports", "h2"))
    st.append(two_col(
        [
            P("From the register \u2014 an Excel workbook", "h3"),
            *steps([
                "Set the filters you want on <b>Contracts</b>, then select <b>Export</b>.",
                "Choose the record types to include \u2014 <b>Contracts</b>, <b>Clauses</b>, <b>Obligations</b>, "
                "<b>Risks</b>, <b>Key dates</b>, <b>Parties</b>.",
                "Confirm. The workbook is built outside your session, so you can carry on working.",
                "Collect it from <b>Exports</b> when it reads <b>Completed</b>.",
            ]),
        ],
        [
            P("What you get", "h3"),
            *bullets([
                "An <b>Excel workbook (.xlsx)</b> of exactly the filtered view, one worksheet per record type.",
                "Files are kept for a limited window shown on the export dialog, then removed. Download promptly; "
                "an expired job must be run again. <b>Exports</b> only ever lists your own exports.",
                "<b>Export</b> on one contract is different: it downloads a <b>CSV report</b> of that contract "
                "straight away and never reaches the Exports screen.",
            ]),
        ], ratio=0.5))

    st.append(P("8.4  Administration", "h2"))
    st.append(tbl(
        [
            ["Screen", "Who", "What it does"],
            ["Business Unit", "Admin",
             "Create a Business Unit with a name, optional client and description; add, edit and remove members, "
             "each with a role."],
            ["Users", "Admin",
             "Create an account with a full name and email and assign it to Business Units with a role. The starting "
             "password is set by the platform and must be changed at first sign-in \u2014 no password is typed here."],
            ["Clause Master", "Admin",
             "Define which clauses each agreement type is checked for. Changes apply to new uploads; already-processed "
             "contracts keep what they were extracted with, so use <b>Reprocess from</b> to re-run them."],
            ["Alerts \u203a Rules", "Admin",
             "Set the warning windows, thresholds and severity for each alert type, and switch a type off."],
            ["Activity", "Admin, Contract Manager",
             "Who did what and when, filtered by action and outcome. Scoped to the Business Units you can see."],
        ],
        widths=[26 * mm, 34 * mm, CW - 60 * mm], pad=1.6))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 11 - STATUSES, TROUBLESHOOTING & FAQ
    # =====================================================================
    st.append(P("SECTION 9", "eyebrow"))
    st.append(P("Statuses, Troubleshooting &amp; Frequently Asked Questions", "h1"))

    st.append(P("9.1  Contract statuses", "h2"))
    st.append(tbl(
        [
            ["Status", "Meaning", "Recommended action"],
            ["Uploaded", "Stored and queued; processing has not started.", "Wait."],
            ["Processing", "Moving through the six stages.", "Wait \u2014 the document is already readable."],
            ["Ready", "Processing finished; nothing is outstanding.", "Review the extracted record."],
            ["Needs review", "Finished, but items await a human decision.",
             "Open the contract and work through the flagged items."],
            ["Failed", "A stage did not complete.",
             "Open the <b>Processing</b> tab for the failing stage and error, then <b>Retry</b>."],
            ["Conversion failed", "Stored, but could not be turned into a PDF, so processing never started.",
             "Upload a different copy of the document; a retry will not help."],
            ["Archived", "Retained for reference, kept out of the working list.",
             "Include the <b>Archived</b> chip on Contracts to see these."],
        ],
        widths=[24 * mm, 60 * mm, CW - 84 * mm], pad=1.25, dense=True))

    st.append(two_col(
        [
            P("9.2  Export statuses", "h2"),
            tbl(
                [
                    ["Status", "Meaning"],
                    ["Queued", "Requested, not started."],
                    ["Running", "The workbook is being built."],
                    ["Completed", "Ready to download."],
                    ["Failed", "Could not be built \u2014 run it again."],
                    ["Expired", "Past its retention window; run it again."],
                ],
                widths=[22 * mm, 60 * mm], pad=1.2, dense=True),
        ],
        [
            P("9.3  Alert statuses", "h2"),
            tbl(
                [
                    ["Status", "Meaning"],
                    ["Open", "Raised and not yet actioned."],
                    ["Acknowledged", "Someone has taken ownership of it."],
                    ["Resolved", "The underlying issue has been handled."],
                    ["Dismissed", "Judged not to apply to this contract."],
                ],
                widths=[24 * mm, 58 * mm], pad=1.2, dense=True),
            Spacer(1, 1.2 * mm),
            P("The states a document passes through while it is being processed are on page 9.", "small"),
        ], ratio=0.5))

    st.append(P("9.4  Common issues", "h2"))
    st.append(tbl(
        [
            ["Issue", "Likely cause", "What to do"],
            ["Cannot sign in", "Wrong credentials, or the account does not exist yet.",
             "Re-enter your email and password; use <b>Sign in with Microsoft</b> where your organisation offers it. "
             "Otherwise ask an administrator to check the account."],
            ["A file will not upload", "Unsupported type, or larger than 50 MB.",
             "Use PDF, .doc, .docx or a .zip of them, under 50 MB each. Re-save or split an oversized scan."],
            ["A contract is stuck on Processing", "It is queued behind other work, or a stage failed.",
             "Open <b>Processing</b>. If a stage shows an error, select <b>Retry</b> \u2014 it resumes rather than restarts."],
            ["No contracts, or empty figures", "The Business Unit selector is narrowed, or filters are active.",
             "Set the selector to <b>All my business units</b> and select <b>Clear all</b> on the filters."],
            ["Search or Copilot returns nothing",
             "The contracts have not finished processing, or the question was read differently than intended.",
             "Check the contract is <b>Ready</b>, open the <b>Retrieval plan</b>, rephrase, or switch to <b>Keyword</b>."],
            ["An export cannot be downloaded", "The job expired, or it failed.",
             "Run the export again from the Contracts screen and download it promptly."],
        ],
        widths=[34 * mm, 46 * mm, CW - 80 * mm], pad=1.25, dense=True))

    st.append(P("9.5  Frequently asked questions", "h2"))
    st.append(tbl(
        [
            ["Question", "Answer"],
            ["Can I trust what was extracted?",
             "Treat it as a fast, well-evidenced first pass, not as a legal conclusion. Every item carries a "
             "confidence badge and an <b>Evidence</b> link to the exact passage \u2014 verify anything material there "
             "before relying on it."],
            ["Why can I not see the Upload screen?",
             "Uploading requires a Contract Manager or Reviewer role in a Business Unit. Administrators and Viewers "
             "do not have it."],
            ["Can I move a contract to another Business Unit?",
             "No. The destination is chosen at upload and is the security boundary. Ask an administrator if a "
             "document was placed wrongly."],
            ["What happens to a Word document?",
             "It is converted to PDF automatically, because evidence highlighting needs a rendered page. Both the "
             "processed PDF and the original remain downloadable."],
            ["I changed the Clause Master \u2014 why did nothing change?",
             "Changes apply to new uploads. Existing contracts keep the clauses they were extracted with; use "
             "<b>Reprocess from</b> on the Processing screen to re-run them."],
        ],
        widths=[48 * mm, CW - 48 * mm], pad=1.3, dense=True))

    st.append(PageBreak())

    # =====================================================================
    # PAGE 12 - QUICK REFERENCE & ABOUT
    # =====================================================================
    st.append(P("SECTION 10", "eyebrow"))
    st.append(P("Quick Reference, Best Practices &amp; About", "h1"))

    st.append(two_col(
        [
            P("The workflow at a glance", "h2"),
            QuickFlow([
                "Sign in and set the Business Unit",
                "Select Upload",
                "Choose the destination project, add files",
                "Upload \u2014 processing starts on its own",
                "Watch Processing until the contract is Ready",
                "Review the extraction against its Evidence",
                "Search, ask Copilot, verify in the source",
                "Export the workbook from Contracts",
            ], width=(CW - 5 * mm) * 0.47),
        ],
        [
            P("Where to go for what", "h2"),
            tbl(
                [
                    ["If you need to\u2026", "Go to"],
                    ["See what needs attention today", "Dashboard"],
                    ["Add a signed agreement", "Upload"],
                    ["Find one contract", "Contracts"],
                    ["See what is due next month", "Portfolio \u203a Key dates"],
                    ["Find a specific clause wording", "Search \u203a Keyword"],
                    ["Ask a question across the estate", "Copilot"],
                    ["Check why a document failed", "Processing"],
                    ["See what the extraction missed", "Clause Coverage"],
                    ["Act on a deadline", "Alerts"],
                    ["Get the data into Excel", "Contracts \u203a Export"],
                    ["Add a colleague", "Users <font color='#8B96AC'>(Admin)</font>"],
                ],
                widths=[46 * mm, 36 * mm], pad=1.2),
        ], ratio=0.5))

    st.append(P("Best practices", "h2"))
    st.append(two_col(
        bullets([
            "<b>Set the Business Unit first.</b> It decides what every figure on screen counts.",
            "<b>Upload to the right project.</b> The destination is the security boundary and is not a "
            "self-service change afterwards.",
            "<b>Review before you circulate.</b> Approve or reject the flagged items so that <b>Needs review</b> "
            "means something to the next person.",
        ]),
        bullets([
            "<b>Open the Evidence for anything material.</b> Confidence badges rank the extraction; only the "
            "document settles it.",
            "<b>Filter, then export.</b> The workbook is exactly the view on screen \u2014 set the filters first "
            "and download it promptly.",
            "<b>Report platform-level problems.</b> A non-zero <b>Dead letter</b> count is for your administrator, "
            "not for individual retries.",
        ]),
        ratio=0.5))

    st.append(Spacer(1, 2 * mm))
    st.append(rule())

    st.append(P("About Iris Regtech Solution", "h2"))
    logo2 = Image(LOGO, width=38 * mm, height=38 * mm * 133 / 364)
    logo2.hAlign = "LEFT"
    st.append(two_col(
        [
            logo2,
            Spacer(1, 2 * mm),
            Paragraph(ORG, ParagraphStyle("ab3", fontName=FB, fontSize=10, leading=13, textColor=NAVY)),
            Paragraph("Contract Intelligence Platform",
                      ParagraphStyle("ab4", fontName=FB, fontSize=8.6, leading=11.4, textColor=BLUE)),
            Paragraph(f"iRIS CLEAR {APP_VERSION} \u00b7 User Manual {DOC_VERSION}<br/>{DOC_DATE}",
                      ParagraphStyle("ab5", fontName=F, fontSize=7.8, leading=10.4, textColor=SLATE)),
        ],
        [
            P("iRIS CLEAR \u2014 Clause Locator &amp; Executive Agreement Review \u2014 is the contract "
              "intelligence platform built by Iris Regtech Solution for teams whose obligations, deadlines and "
              "liability sit inside agreements that nobody has time to re-read.", "body"),
            P("The product's guiding rule is that a finding is only useful if it can be checked. Every clause, "
              "risk, obligation and Copilot answer in this manual is presented alongside the document, page and "
              "passage it came from, so a reviewer can move from a headline figure to the contract wording in a "
              "few seconds \u2014 and can say, afterwards, exactly where the answer came from.", "body"),
            P("For access, roles, Business Unit membership or platform issues, contact your platform "
              "administrator.", "small"),
        ], ratio=0.34))

    st.append(Spacer(1, 14 * mm))
    st.append(ClosingStrip())

    return st


class ClosingStrip(Flowable):
    """The document-identity band that closes the manual."""

    def __init__(self, width=CW, height=18 * mm):
        super().__init__()
        self.width, self.height = width, height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        c.setFillColor(NAVY)
        c.roundRect(0, 0, w, h, 2 * mm, stroke=0, fill=1)
        c.setFillColor(BLUE)
        c.rect(0, 0, w * 0.42, 1.4 * mm, stroke=0, fill=1)
        c.setFillColor(GREEN)
        c.rect(w * 0.42, 0, w * 0.18, 1.4 * mm, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#7FA8F5"))
        c.setFont(FB, 6.4)
        c.drawString(5 * mm, h - 6.4 * mm, "END OF DOCUMENT")
        c.setFillColor(colors.white)
        c.setFont(FB, 8.6)
        c.drawString(5 * mm, h - 11.4 * mm, f"{ORG}  ·  iRIS CLEAR User Manual")
        c.setFillColor(colors.HexColor("#A9B4C7"))
        c.setFont(F, 7.4)
        c.drawRightString(w - 5 * mm, h - 11.4 * mm,
                          f"iRIS CLEAR {APP_VERSION}  ·  Manual {DOC_VERSION}  ·  {DOC_DATE}")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
AVAIL_H = PAGE_H - MT - MB - 4 * mm


def report_fit(flat):
    """Split the story on PageBreaks and report how each page block measures.

    The build itself is the authority, but this makes over-long pages obvious
    before rendering rather than after counting pages.
    """
    blocks, current = [], []
    for item in flat:
        if isinstance(item, PageBreak):
            blocks.append(current)
            current = []
        elif not isinstance(item, NextPageTemplate):
            current.append(item)
    blocks.append(current)

    print("--- fit report (frame height %.0f mm) ---" % (AVAIL_H / mm))
    for i, block in enumerate(blocks, 1):
        total = 0.0
        for f in block:
            try:
                _, h = f.wrap(CW, 10 * AVAIL_H)
            except Exception:
                h = 0.0
            total += h
            style = getattr(f, "style", None)
            if style is not None:
                total += getattr(style, "spaceBefore", 0) + getattr(style, "spaceAfter", 0)
        flag = "OVER" if total > AVAIL_H else "ok"
        print("  page %2d: %6.1f mm  (%+6.1f)  %s" % (i, total / mm, (AVAIL_H - total) / mm, flag))
    print("--- %d page blocks ---" % len(blocks))


def main():
    doc = BaseDocTemplate(
        OUT,
        pagesize=A4,
        leftMargin=ML, rightMargin=MR, topMargin=MT, bottomMargin=MB,
        title=f"{APP} \u2014 User Manual",
        author=ORG,
        subject=f"{ORG} \u00b7 {APP} (Contract Intelligence Platform) User Manual, version {DOC_VERSION}",
        creator=f"{ORG}",
        keywords=[
            "Iris Regtech Solution", "iRIS CLEAR", "Contract Intelligence",
            "User Manual", f"Application {APP_VERSION}",
        ],
    )

    cover_frame = Frame(ML, MB + 6 * mm, CW, PAGE_H - MB - 6 * mm - 24 * mm,
                        id="cover", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    body_frame = Frame(ML, MB + 4 * mm, CW, PAGE_H - MT - MB - 4 * mm,
                       id="body", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=cover_page),
        PageTemplate(id="body", frames=[body_frame], onPage=body_page),
    ])

    report_fit(build_story())
    doc.build(build_story(), canvasmaker=FooterCanvas)
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
