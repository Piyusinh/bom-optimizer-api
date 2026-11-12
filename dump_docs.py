"""Dump structure + samples of every xlsx/pptx in docs/Project DIC teams."""
import sys
import zipfile
from xml.etree import ElementTree as ET

import pandas as pd

PPT_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def dump_xlsx(path: str) -> None:
    print("=" * 100)
    print("XLSX:", path.rsplit("\\", 1)[-1])
    xl = pd.ExcelFile(path)
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        print(f"--- sheet: {sheet}  ({len(df)} rows x {len(df.columns)} cols)")
        print("columns:", list(df.columns))
        print(df.head(3).to_string())


def dump_pptx(path: str) -> None:
    print("=" * 100)
    print("PPTX:", path.rsplit("\\", 1)[-1])
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")],
            key=lambda s: int("".join(c for c in s.split("slide")[-1] if c.isdigit())),
        )
        for s in slides:
            root = ET.fromstring(z.read(s))
            texts = [t.text for t in root.iter(PPT_NS + "t") if t.text]
            print(f"--- {s.rsplit('/', 1)[-1]}:")
            print("  " + " | ".join(texts))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        try:
            if p.lower().endswith(".xlsx"):
                dump_xlsx(p)
            elif p.lower().endswith(".pptx"):
                dump_pptx(p)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {p}: {e}")
