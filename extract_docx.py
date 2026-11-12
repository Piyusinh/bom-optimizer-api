"""Extract plain text from a .docx file (zipfile + XML, no external deps)."""
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    lines = []
    for para in root.iter(NS + "p"):
        texts = [t.text or "" for t in para.iter(NS + "t")]
        line = "".join(texts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1]
    text = extract(path)
    out = path.rsplit(".", 1)[0] + "_extracted.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"{path} -> {out} ({len(text)} chars)")
