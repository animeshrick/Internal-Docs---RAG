"""
parsing_v2.py - read HR .docx files into ordered, labelled blocks plus
document-level metadata.

parse_docx(doc, name)  -> one dict per document (metadata + blocks)
parse_folder(folder)   -> (list of parsed docs, list of skipped files)
validate(info)         -> list of problems/warnings for one parsed doc
chunk_metadata(info)   -> fields the chunker should copy onto every chunk
"""
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

# ---------------------------------------------------------------- constants
HEADING_LEVELS = {"Title": 0, "Heading 1": 1, "Heading 2": 2, "Heading 3": 3}
DOC_ID_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z]+)+-\d{3}\b")  # HR-LEAVE-001, IT-SEC-008, FIN-ADMIN-011
CALLOUT_RE = re.compile(r"^(IMPORTANT|NOTE|WARNING|CRITICAL)\b", re.I)
FOOTER_VERSION_RE = re.compile(r"\bv(\d+(?:\.\d+)+)\b")   # "v1.0" in a footer
PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][^\]]{2,40}\]")  # e.g. [Company Name]


# ---------------------------------------------------------- small utilities
def clean_text(s):
    """Join lines, drop blank ones, collapse runs of spaces."""
    s = " ".join(part.strip() for part in s.split("\n") if part.strip())
    return re.sub(r" {2,}", " ", s)


'''OLD'''
def unique_cells(row):
    """Cells of a table row, skipping the repeats Word creates for merged cells."""
    '''OLD CODE'''
    # out, last = [], None
    # for c in row.cells:
    #     print(f"unique_cells _tc: {c._tc}")
    #     if c._tc is not last:
    #         out.append(c.text.strip())
    #         last = c._tc
    # return out
    out, seen = [], set()

    for c in row.cells:
        if c._tc not in seen:
            out.append(c.text.strip())
            seen.add(c._tc)
    return out


DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")
def normalize_date(s):
    """Day-first dates (Indian convention) -> ISO 'YYYY-MM-DD'. Unknown format: unchanged."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return s


BULLET_RE = re.compile(r"^[\u2022\u25CF\u25AA\u25E6\-\u2013]\s+")   # typed bullets: "• text"
NUMBERED_RE = re.compile(r"^(?:\d{1,2}[.)]|\(?[a-z][.)])\s+\S", re.I)   # 1.  1)  a)  (a)

# def is_list_item_old(p, text):
#     pPr = p._p.pPr
#     return (p.style.name.startswith("List")
#             or (pPr is not None and pPr.numPr is not None)
#             or bool(BULLET_RE.match(text)))

def is_list_item(p, text):
    pPr = p._p.pPr
    return (p.style.name.startswith("List")
            or (pPr is not None and pPr.numPr is not None and pPr.numPr.numId is not None
                and pPr.numPr.numId.val != 0) # numId 0 = numbering off
            or bool(BULLET_RE.match(text))
            or bool(NUMBERED_RE.match(text)))


def has_header_row(table):
    """True if Word marks the first row as a repeating header row."""
    trPr = table.rows[0]._tr.find(qn("w:trPr"))
    return trPr is not None and trPr.find(qn("w:tblHeader")) is not None


def is_metadata_table(rows):
    """2 rows (keys + values); keys include STATUS and OWNER."""
    if len(rows) != 2 or len(rows[0]) < 2:
        return False
    keys = [h.upper() for h in rows[0]]
    return any(k.startswith("STATUS") for k in keys) and any("OWNER" in k for k in keys)


def read_metadata(fields):
    out = {}
    for k, v in fields.items():
        key = k.upper()
        if key.startswith("STATUS"):
            out["status"] = v
        elif key.startswith("VERSION"):
            out["version"] = v
        elif "EFFECTIVE" in key:
            out["effective_date"] = normalize_date(v)
        elif "OWNER" in key:
            out["owner"] = v
    return out


def guess_doc_type(doc_id):
    code = doc_id or ""
    if "-FORM-" in code:
        return "form"
    if "-GOV-" in code:
        return "guide"
    if code.startswith("ORG-"):
        return "reference"          # company profile, org structure, etc.
    return "policy"


def table_lines(layout, header, rows):
    """One searchable sentence per row, so column names never get lost."""
    if layout == "kv":
        return [f"{r[0]}: {' '.join(r[1:])}" for r in rows if r]
    return ["; ".join(f"{h}: {c}" for h, c in zip(header, r) if c) for r in rows]

# counts what the parser can’t read and stores it as warnings
def find_unsupported(doc, blocks):
    x = doc.element.xml
    w = {
        "nested tables":    len(re.findall(r"<w:tc>(?:(?!</w:tc>).)*?<w:tbl>", x, re.S)),
        "text boxes":       x.count("<w:txbxContent"),
        "content controls": x.count("<w:sdt>") + x.count("<w:sdt "),
        "tracked changes":  x.count("<w:ins ") + x.count("<w:del "),
        "images/drawings":  x.count("<w:drawing") + x.count("<w:pict"),
    }
    return [f"{n} {k} not parsed" for k, n in w.items() if n]


# -------------------------------------------------------------- main parser
def parse_docx(doc, doc_name):
    info = {
        "source": doc_name, 
        "doc_id": None, 
        "doc_type": None, 
        "title": None,
        "summary": None, 
        "status": None, 
        "version": None,
        "effective_date": None, 
        "owner": None
        }
    # Initialize tracking variables
    blocks, path, seen_table = [], [], False

    # Iterate through document elements
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[1]

        # ------------------------------------------------------ paragraphs
        if tag == "p":
            p = Paragraph(child, doc)
            text = clean_text(p.text)
            if not text:
                continue
            style = p.style.name

            # Extract document ID and summary from specific styles
            if style in ("SmallTag", "Eyebrow"):  # tag line above the title, e.g. "... POLICY HR-LEAVE-001"
                m = DOC_ID_RE.search(text)
                print(f"parse_docx: {m}")
                if m:
                    info["doc_id"] = m.group()
                continue
            if style == "Meta":                  # one-line subtitle
                info["summary"] = text
                continue

            # Process headings and build the heading path
            if style in HEADING_LEVELS:
                if style == "Title":
                    depth = 0
                    info["title"] = text
                else:                            # Title is the root when present
                    depth = HEADING_LEVELS[style] if info["title"] else HEADING_LEVELS[style] - 1
                path = path[:depth] + [text]
                kind = "heading"
            elif is_list_item(p, text):
                kind = "list_item"
            else:
                kind = "text"
            blocks.append({"type": kind, "text": text,
                           "heading_path": list(path), "source": doc_name})

        # ----------------------------------------------------------- tables
        elif tag == "tbl":
            table = Table(child, doc)
            rows = [[clean_text(c) for c in unique_cells(r)] for r in table.rows]
            rows = [r for r in rows if any(r)]
            is_first = not seen_table
            seen_table = True
            if not rows:
                continue

            if len(rows) == 1 and len(rows[0]) == 1:          # callout box
                text = rows[0][0]
                m = CALLOUT_RE.match(text)
                blocks.append({"type": "callout", "label": m.group(1).upper() if m else None,
                               "text": text, "heading_path": list(path), "source": doc_name})
                continue

            if is_first and is_metadata_table(rows):          # STATUS / VERSION / ...
                info.update(read_metadata(dict(zip(rows[0], rows[1]))))
                continue

            if has_header_row(table):
                layout, header, body = "grid", rows[0], rows[1:]
            else:                                             # key -> value table
                layout, header, body = "kv", None, rows
            blocks.append({"type": "table", "layout": layout, "header": header,
                           "rows": body, "lines": table_lines(layout, header, body),
                           "heading_path": list(path), "source": doc_name})

    # doc_id fallback from the footer, and placeholders from body + header/footer
    hf_text = " ".join(p.text for s in doc.sections
                       for part in (s.header, s.footer) for p in part.paragraphs)
    if not info["doc_id"]:
        m = DOC_ID_RE.search(hf_text)
        info["doc_id"] = m.group() if m else None
    info["doc_type"] = guess_doc_type(info["doc_id"])
    if not info["version"]:
        m = FOOTER_VERSION_RE.search(hf_text)
        info["version"] = m.group(1) if m else None

    body_text = " ".join(b.get("text", "") + " ".join(b.get("lines", [])) for b in blocks)
    info["placeholders"] = sorted({m.lower() for m in PLACEHOLDER_RE.findall(body_text + " " + hf_text)})
    info["warnings"] = find_unsupported(doc, blocks)       # content the parser could not read
    info["blocks"] = blocks
    return info


# ----------------------------------------------------------------- folder
def _parse_folder(folder, strict, label):
    """Shared loop. strict=True also requires a STATUS table (the old rule)."""
    docs, skipped = [], []
    for path in sorted(Path(folder).iterdir()):
        if path.name.startswith("~$"):
            continue                                           # Word lock files
        if path.suffix.lower() != ".docx":
            skipped.append((path.name, "not a .docx (no loader yet)"))
            continue
        info = parse_docx(Document(str(path)), path.name)
        if not info["doc_id"]:
            skipped.append((path.name, "no doc code in the tag line or footer - not part of the knowledge base"))
            continue
        if strict and not info["status"]:
            skipped.append((path.name, "no HR doc code / metadata table - not an HR policy file"))
            continue
        docs.append(info)
    print(f"{label}: {len(docs)} docs, {len(skipped)} skipped[{', '.join(why for _, why in skipped)}]  from {folder}")
    return docs, skipped


def parse_folder(folder):
    """Accept any .docx that has a doc code. Status/owner are optional."""
    return _parse_folder(folder, strict=False, label="parse_folder")


def chunk_metadata(info):
    keys = ("source", "doc_id", "doc_type", "title", "status", "version",
            "effective_date", "owner")
    return {k: info[k] for k in keys if info.get(k) is not None}   # drop empty fields (Pinecone rejects null)


def validate(info):
    problems = []
    required = ("doc_id", "title") if info["doc_type"] == "reference" else \
               ("doc_id", "title", "status", "version", "effective_date", "owner")
    ref_optional = ("status", "effective_date", "owner")      # not expected on a reference document
    for k in ("doc_id", "title", "status", "version", "effective_date", "owner"):
        if not info.get(k):
            if info["doc_type"] == "reference" and k in ref_optional:
                continue
            problems.append(("ERROR" if k in required else "WARN") + f" missing {k}")
    for i, b in enumerate(info["blocks"]):
        if not b["heading_path"]:
            problems.append(f"ERROR block {i} ({b['type']}) has no heading_path")
        if b["type"] == "table" and not b["rows"]:
            problems.append(f"WARN block {i} table has a header but no rows")
    if info["status"] and "draft" in info["status"].lower():
        problems.append(f"WARN status is '{info['status']}' - answers should say so")
    if info["placeholders"]:
        problems.append(f"WARN unfilled placeholders: {info['placeholders']}")
    for w in info.get("warnings", []):
        problems.append(f"WARN {w}")
    return problems


if __name__ == "__main__":
    import collections
    raw = Path(__file__).resolve().parent.parent / "data" / "raw"
    docs, skipped = parse_folder(raw)
    for d in docs:
        counts = dict(collections.Counter(b["type"] for b in d["blocks"]))
        print(f"\n{d['source']}  [{d['doc_id']} | {d['doc_type']} | v{d['version']} | "
              f"{d['status']} | effective {d['effective_date']}]")
        print("  blocks:", counts)
        for p in validate(d):
            print("  ", p)
    print("\nSkipped:")
    for name, why in skipped:
        print(f"  {name}: {why}")
