from docx.text.paragraph import Paragraph
from docx.table import Table


def parse_docx(doc, doc_name):
    blocks = []
    current_section = ""

    for child in doc.element.body.iterchildren():
        # print(f"~~ child.tag: {child.tag} ~~")
        tag = child.tag.split("}")[1]

        if tag == "p":
            p = Paragraph(child, doc)
            if not p.text.strip():
                continue
            is_heading = p.style.name.startswith("Heading")
            if is_heading:
                current_section = p.text
            blocks.append({
                "type": "heading" if is_heading else "text",
                "text": p.text,
                "section": current_section,
                "source": doc_name,
            })

        elif tag == "tbl":
            table = Table(child, doc)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            blocks.append({
                "type": "table",
                "header": rows[0],
                "rows": rows[1:],
                "section": current_section,
                "source": doc_name,
            })

    return blocks


HEADING_LEVELS = {"Title": 0, "Heading 1": 1, "Heading 2": 2, "Heading 3": 3}

def is_metadata_table(rows):
    flat = " ".join(rows[0]).upper()
    return "STATUS" in flat and ("OWNER" in flat or "VERSION" in flat)

def unique_cells(row):
    out, last = [], None
    for c in row.cells:
        if c._tc is not last:
            out.append(c.text.strip())
            last = c._tc
    return out

def parse_docx_v2(doc, doc_name):
    blocks = []
    path = []  # e.g. ["Leave & Holiday Policy", "Sick leave"]

    '''doc.element.body is the document’s main content area. Its children are paragraphs and tables, 
    in the order they appear in the file. Looping over them keeps the real order. The alternative, 
    doc.paragraphs, returns only paragraphs and drops tables, and doc.tables loses their position. 
    Iterating the body is the reason this function is needed at all.'''
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[1]

        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = p.style.name
            if style in HEADING_LEVELS:
                level = HEADING_LEVELS[style]
                path = path[:level - 1] + [text] if level > 0 else [text]
                blocks.append({
                    "type": "heading", 
                    "text": text,
                    "heading_path": list(path), 
                    "source": doc_name
                    })
            else:
                blocks.append({
                    "type": "text", 
                    "text": text,
                    "heading_path": list(path), 
                    "source": doc_name
                    })

        elif tag == "tbl":
            table = Table(child, doc)
            # rows = [[c.text.strip() for c in r.cells] for r in table.rows]
            # rows = [r for r in rows if any(r)]
            rows = [unique_cells(r) for r in table.rows]
            # if len(rows) < 2:
            #     continue
            # header, body = rows[0], rows[1:]
            # if header == ["STATUS", "VERSION", "EFFECTIVE DATE", "POLICY OWNER"]:
            #     continue  # document metadata, not policy content
            # blocks.append({"type": "table", "header": header, "rows": body,
            #                "heading_path": list(path), "source": doc_name})
            if not rows:
                continue
            if len(rows) == 1 and len(rows[0]) == 1:    # callout box: keep the text
                blocks.append({
                    "type": "text", 
                    "text": rows[0][0],
                    "header": is_metadata_table(rows),
                    "heading_path": list(path), 
                    "source": doc_name
                })
                continue

    return blocks

def _clean_text(s):
    # Join lines, drop blank ones, keep tabs inside a line (used for "Company\tLocation").
    return " ".join(part.strip() for part in s.split("\n") if part.strip())


def _is_metadata_v3(rows):
    # Metadata table: exactly 2 rows (header + values), header has STATUS and OWNER.
    if len(rows) != 2 or len(rows[0]) < 2:
        return False
    keys = [h.upper() for h in rows[0]]
    return any(k.startswith("STATUS") for k in keys) and any("OWNER" in k for k in keys)


def parse_docx_v3(doc, doc_name):
    blocks = []
    path = []  # heading chain, e.g. ["Leave & Holiday Policy", "Sick leave"]
    seen_table = False

    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[1]

        if tag == "p":
            p = Paragraph(child, doc)
            text = _clean_text(p.text)
            if not text:
                continue
            style = p.style.name
            if style in HEADING_LEVELS:
                level = HEADING_LEVELS[style]
                path = path[:level - 1] + [text] if level > 0 else [text]
                kind = "heading"
            else:
                kind = "text"
            blocks.append({"type": kind, "text": text,
                           "heading_path": list(path), "source": doc_name})

        elif tag == "tbl":
            table = Table(child, doc)
            rows = [[_clean_text(c) for c in unique_cells(r)] for r in table.rows]
            rows = [r for r in rows if any(r)]
            is_first_table = not seen_table
            seen_table = True
            if not rows:
                continue

            # Single-cell box (e.g. "IMPORTANT ..." callouts): keep its text as content.
            if len(rows) == 1 and len(rows[0]) == 1:
                blocks.append({"type": "text", "text": rows[0][0],
                               "heading_path": list(path), "source": doc_name})
                continue

            # Document metadata (status, version, owner): keep separate, not policy text.
            if is_first_table and _is_metadata_v3(rows):
                blocks.append({"type": "metadata",
                               "fields": dict(zip(rows[0], rows[1])),
                               "source": doc_name})
                continue

            # Regular table, including header-only tables (column names are kept).
            blocks.append({"type": "table", "header": rows[0], "rows": rows[1:],
                           "heading_path": list(path), "source": doc_name})

    return blocks
