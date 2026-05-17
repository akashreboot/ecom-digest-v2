"""
scripts/render_docs.py — Convert all study/deliverable markdown to print-ready PDFs.

Run with:
    python scripts/render_docs.py

Writes one PDF per source markdown into docs_pdf/. The styling matches
PRESENTATION.pdf — Inter font, navy headers, dark-mode code blocks,
paginated with page numbers in the footer.

Idempotent — safe to re-run after editing any source doc.
"""
from __future__ import annotations

import sys
from pathlib import Path

import markdown
from weasyprint import HTML, CSS

ROOT      = Path(__file__).resolve().parent.parent
OUT_DIR   = ROOT / "docs_pdf"
OUT_DIR.mkdir(exist_ok=True)

# Source docs and their human-friendly titles for the footer
DOCS = [
    ("INTERVIEW_PREP.md",      "Interview Prep — Complete Study Guide"),
    ("PRESENTATION.md",        "Presentation Pack — Answers + Script"),
    ("OPTIMIZATION_JOURNEY.md", "Optimization Journey — v1 to v2 Evolution"),
    ("design_doc.md",          "Product & System Design"),
    ("tradeoffs.md",           "Trade-offs & What's Next"),
]


CSS_STR = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

@page {
    size: A4;
    margin: 18mm 18mm 20mm 18mm;
    @bottom-right {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Inter', Helvetica, Arial, sans-serif;
        font-size: 9pt;
        color: #888;
    }
    @bottom-left {
        content: var(--doc-footer);
        font-family: 'Inter', Helvetica, Arial, sans-serif;
        font-size: 9pt;
        color: #888;
    }
}

body {
    font-family: 'Inter', "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.55;
    color: #222;
}

h1 {
    font-size: 20pt;
    font-weight: 700;
    color: #1a1a1a;
    border-bottom: 2px solid #2563eb;
    padding-bottom: 6px;
    margin-top: 20px;
    margin-bottom: 14px;
    page-break-before: always;
}
h1:first-of-type { page-break-before: avoid; }

h2 {
    font-size: 15pt;
    font-weight: 700;
    color: #1e3a8a;
    margin-top: 22px;
    margin-bottom: 10px;
    border-bottom: 1px solid #ddd;
    padding-bottom: 4px;
    page-break-after: avoid;
}

h3 {
    font-size: 12pt;
    font-weight: 700;
    color: #2563eb;
    margin-top: 16px;
    margin-bottom: 6px;
    page-break-after: avoid;
}

h4 {
    font-size: 11pt;
    font-weight: 700;
    color: #1e3a8a;
    margin-top: 12px;
    margin-bottom: 4px;
    page-break-after: avoid;
}

p {
    margin: 6px 0 10px 0;
    text-align: justify;
}

blockquote {
    border-left: 3px solid #2563eb;
    padding: 6px 12px;
    margin: 10px 0 14px 0;
    background: #f1f5fb;
    color: #333;
    font-style: italic;
}

code {
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 9pt;
    background: #f5f5f7;
    padding: 1px 4px;
    border-radius: 3px;
    color: #b91c1c;
}

pre {
    background: #1f2937;
    color: #f1f5f9;
    font-family: "SF Mono", Menlo, Consolas, monospace;
    font-size: 8.5pt;
    line-height: 1.45;
    padding: 10px 14px;
    border-radius: 4px;
    overflow-x: auto;
    page-break-inside: avoid;
    margin: 8px 0 14px 0;
}
pre code {
    background: transparent;
    color: inherit;
    padding: 0;
}

ul, ol {
    margin: 4px 0 10px 22px;
    padding: 0;
}
li { margin-bottom: 4px; }

table {
    border-collapse: collapse;
    width: 100%;
    margin: 8px 0 14px 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
}
th {
    background: #1e3a8a;
    color: white;
    padding: 6px 8px;
    text-align: left;
    font-weight: 600;
}
td {
    border-bottom: 1px solid #e5e7eb;
    padding: 5px 8px;
    vertical-align: top;
}
tr:nth-child(even) td { background: #f9fafb; }

hr {
    border: none;
    border-top: 1px dashed #cbd5e1;
    margin: 18px 0;
}

strong { color: #1a1a1a; }
em     { color: #4b5563; }
"""


def render_one(md_path: Path, title: str, out_path: Path) -> int:
    """Render a single markdown file to PDF. Returns size in KB."""
    md_text = md_path.read_text(encoding="utf-8")

    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "toc", "attr_list"],
        output_format="html5",
    )

    # Per-doc footer text via a CSS variable
    css = CSS(string=f":root {{ --doc-footer: 'Akash Dixit — {title}'; }}\n{CSS_STR}")

    html_full = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title></head><body>{html_body}</body></html>"""

    HTML(string=html_full).write_pdf(str(out_path), stylesheets=[css])
    return out_path.stat().st_size // 1024


def main() -> None:
    print(f"Rendering {len(DOCS)} docs → {OUT_DIR}/")
    print("─" * 60)
    total_kb = 0
    for md_name, title in DOCS:
        src = ROOT / md_name
        if not src.exists():
            print(f"  SKIP  {md_name} (not found)")
            continue
        out = OUT_DIR / (src.stem + ".pdf")
        size_kb = render_one(src, title, out)
        total_kb += size_kb
        print(f"  OK    {src.stem + '.pdf':<35} {size_kb:>5} KB")
    print("─" * 60)
    print(f"  TOTAL                              {total_kb:>5} KB")
    print(f"\nWrote PDFs to: {OUT_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    sys.exit(main())
