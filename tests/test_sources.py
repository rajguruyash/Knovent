"""FAQ tables with other people's column names, documents cut into passages, and several files as one knowledge base."""
import json
import zipfile

import pytest

from faqengine import sources
from faqengine.kb import KB, load_kb, write_kb
from faqengine.runtime import evidence as ev


def write(tmp_path, name, text, mode="w"):
    p = tmp_path / name
    p.write_text(text) if mode == "w" else p.write_bytes(text)
    return p


def test_other_peoples_column_names_just_work(tmp_path):
    for header, row in [("Question,Answer", "How do I pay?,By card."), ("Q,A", "How do I pay?,By card."), ("FAQ,Response,Tags", "How do I pay?,By card.,billing")]:
        kb = load_kb(write(tmp_path, "f.csv", f"{header}\n{row}\n"))
        assert kb.entries[0].question == "How do I pay?" and kb.entries[0].answer == "By card." and kb.entries[0].kind == "faq"
    kb = load_kb(write(tmp_path, "g.json", json.dumps({"data": [{"Questions": ["How do I pay?", "pay how"], "Reply": "By card.", "Paraphrases": ["ways to pay"]}]})))
    assert kb.entries[0].questions == ["How do I pay?", "pay how", "ways to pay"] and kb.entries[0].answer == "By card."


def test_a_table_with_no_question_column_is_a_table_of_documents(tmp_path):
    kb = load_kb(write(tmp_path, "docs.csv", "Title,Content\nShipping,We ship in two days. Tracking is emailed.\nReturns,Send it back within thirty days.\n"))
    assert [e.kind for e in kb.entries] == ["passage", "passage"] and [e.question for e in kb.entries] == ["Shipping", "Returns"]
    assert kb.entries[0].source == "docs.csv" and "Tracking" in kb.entries[0].answer
    odd = load_kb(write(tmp_path, "odd.csv", "alpha,beta\n1,2\n"))  # nothing to recognise: each row is still searchable, as "Column: value"
    assert [e.kind for e in odd.entries] == ["passage"] and "alpha: 1." in odd.entries[0].answer and "beta: 2." in odd.entries[0].answer
    with pytest.raises(ValueError, match="no rows"):
        load_kb(write(tmp_path, "empty.csv", "alpha,beta\n"))


def test_documents_are_cut_at_headings_and_at_the_size_limit(tmp_path):
    long_para = " ".join(f"word{i}." for i in range(200))
    text = f"# Shipping\n\nWe ship in two days.\n\nTracking is emailed.\n\n# Returns\n\nReturn within thirty days.\n\n{long_para}\n"
    kb = load_kb(write(tmp_path, "handbook.md", text))
    assert all(e.kind == "passage" and e.source == "handbook.md" for e in kb.entries)
    assert kb.entries[0].question == "Shipping" and "two days" in kb.entries[0].answer and "Tracking" in kb.entries[0].answer  # short paragraphs share a passage
    returns = [e for e in kb.entries if e.question == "Returns"]
    assert len(returns) >= 3 and all(len(e.answer.split()) <= sources.CHUNK_WORDS for e in kb.entries)  # the 200-word paragraph was split
    assert [e.id for e in kb.entries] == [f"handbook-{i:03d}" for i in range(1, len(kb) + 1)]
    plain = load_kb(write(tmp_path, "notes.txt", "Opening hours are nine to five.\n\nWe close on public holidays.\n"))
    assert plain.entries[0].question.startswith("Opening hours are nine")  # no heading: the first words stand in


def test_html_docx_and_pdf(tmp_path):
    h = load_kb(write(tmp_path, "page.html", "<html><head><title>x</title><style>p{}</style></head><body><h1>Delivery</h1><p>We deliver in two days.</p><script>bad()</script></body></html>"))
    assert h.entries[0].question == "Delivery" and "two days" in h.entries[0].answer and "bad()" not in h.entries[0].answer

    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = (f'<w:document {ns}><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Warranty</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Two years on the frame.</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(tmp_path / "terms.docx", "w") as z:
        z.writestr("word/document.xml", xml)
    d = load_kb(tmp_path / "terms.docx")
    assert d.entries[0].question == "Warranty" and "frame" in d.entries[0].answer
    with zipfile.ZipFile(tmp_path / "evil.docx", "w") as z:
        z.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "b">]><w:document ' + ns + "/>")
    with pytest.raises(ValueError, match="entities"):
        load_kb(tmp_path / "evil.docx")
    with pytest.raises(ValueError, match="not a readable"):
        load_kb(write(tmp_path, "fake.docx", "not a zip"))

    pytest.importorskip("pypdf")
    stream = b"BT /F1 12 Tf 72 720 Td (Opening hours are nine to five.) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offsets) + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % xref
    kb = load_kb(write(tmp_path, "hours.pdf", pdf, "wb"))
    assert "nine to five" in kb.entries[0].answer and kb.entries[0].source == "hours.pdf"
    with pytest.raises(ValueError, match="PDF"):
        load_kb(write(tmp_path, "broken.pdf", b"%PDF-1.4 garbage", "wb"))


def test_several_files_make_one_knowledge_base_with_unique_ids(tmp_path):
    a = write(tmp_path, "faq1.csv", "question,answer\nHow do I pay?,By card.\nWhere are you?,In Pune.\n")
    b = write(tmp_path, "faq2.csv", "question,answer\nCan I return it?,Yes within thirty days.\n")
    doc = write(tmp_path, "terms.md", "# Warranty\n\nTwo years on the frame.\n")
    kb = load_kb([a, b, doc])
    assert [e.id for e in kb.entries] == ["faq-001", "faq-002", "faq-003", "terms-001"]  # the FAQ counter carries on across files
    assert [e.kind for e in kb.entries] == ["faq", "faq", "faq", "passage"] and [e.source for e in kb.entries][2:] == ["faq2.csv", "terms.md"]
    # the same explicit id in two files: the later one is prefixed with its file; in one file it is an error
    x = write(tmp_path, "x.csv", "id,question,answer\nhours,When open?,9 to 5.\n")
    y = write(tmp_path, "y.csv", "id,question,answer\nhours,Opening times?,9 to 5.\n")
    assert [e.id for e in load_kb([x, y]).entries] == ["hours", "y:hours"]
    with pytest.raises(ValueError, match="duplicate id"):
        load_kb(write(tmp_path, "z.csv", "id,question,answer\nh,A?,x\nh,B?,y\n"))
    with pytest.raises(ValueError, match="unsupported"):
        load_kb(write(tmp_path, "pic.png", "x"))


def test_faq_only_view_and_a_saved_knowledge_base_reloads_as_it_was(tmp_path):
    kb = load_kb([write(tmp_path, "f.csv", "question,answer\nHow do I pay?,By card.\n"), write(tmp_path, "d.md", "# Warranty\n\nTwo years on the frame.\n")])
    faq, keep = kb.faq_only()
    assert len(faq) == 1 and keep == [0] and faq.entries[0].id == "faq-001"
    assert load_kb(tmp_path / "d.md").faq_only() == (None, [])
    write_kb(kb.entries, tmp_path / "kb.json")
    again = load_kb(tmp_path / "kb.json")
    assert [(e.id, e.kind, e.source, e.answer) for e in again.entries] == [(e.id, e.kind, e.source, e.answer) for e in kb.entries]
    assert sources.kind_of(tmp_path / "f.csv") == "faq" and sources.kind_of(tmp_path / "d.md") == "document"


def test_tables_saved_by_other_programs_in_other_languages_are_read(tmp_path):
    # what European Excel saves: semicolons, the Windows code page, and headers in the writer's own language
    spanish = write(tmp_path, "es.csv", "Pregunta;Respuesta;Categoría\n¿Cuál es el horario?;De nueve a cinco, de lunes a viernes.;tienda\n".encode("cp1252"), "wb")
    kb = load_kb(spanish)
    assert kb.entries[0].question == "¿Cuál es el horario?" and kb.entries[0].answer.startswith("De nueve") and kb.entries[0].category == "tienda"
    tabs = load_kb(write(tmp_path, "t.tsv", "id\tquestion\tanswer\n7.0\tHow do I pay?\tBy card.\n"))
    assert tabs.entries[0].id == "7" and tabs.entries[0].answer == "By card."  # a spreadsheet stores 7 as 7.0
    hindi = load_kb(write(tmp_path, "hi.csv", "प्रश्न,उत्तर\nआपकी दुकान कब खुलती है?,सुबह नौ बजे से शाम पाँच बजे तक।\n"))
    assert hindi.entries[0].question.startswith("आपकी") and hindi.entries[0].answer.startswith("सुबह")
    utf16 = tmp_path / "u16.csv"
    utf16.write_bytes("question,answer\nHow do I pay?,By card.\n".encode("utf-16"))
    assert load_kb(utf16).entries[0].answer == "By card."
    long_cell = load_kb(write(tmp_path, "long.csv", "question,answer\nWhy?," + "x " * 200_000 + "\n"))  # beyond the csv module's default field limit
    assert len(long_cell.entries[0].answer) > 300_000


def test_json_from_a_cms_or_a_web_page_and_html_inside_cells(tmp_path):
    wrapped = {"data": [{"id": 5, "attributes": {"question": "How do I pay?", "answer": "<p>By <b>card</b> &amp; cash.</p><ul><li>No cheques</li></ul>"}}]}
    kb = load_kb(write(tmp_path, "cms.json", json.dumps(wrapped)))
    assert kb.entries[0].question == "How do I pay?" and kb.entries[0].answer == "By card & cash. No cheques"
    schema = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": "Do you deliver?", "acceptedAnswer": {"@type": "Answer", "text": "Yes, within ten miles."}},
        {"@type": "Question", "name": "Can I return it?", "acceptedAnswer": {"@type": "Answer", "text": "Within 30 days."}}]}
    kb = load_kb(write(tmp_path, "faqpage.json", json.dumps(schema)))
    assert [(e.question, e.answer) for e in kb.entries] == [("Do you deliver?", "Yes, within ten miles."), ("Can I return it?", "Within 30 days.")]
    graph = {"@graph": [{"@type": "WebSite", "name": "x"}, schema]}
    assert len(load_kb(write(tmp_path, "graph.json", json.dumps(graph))).entries) == 2


def _xlsx(path, sheets):
    """A minimal real .xlsx: shared strings, inline strings and numbers, one sheet per (name, rows)."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    shared, parts = [], {}
    for n, (_, rows) in enumerate(sheets, start=1):
        out = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                if isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                elif c % 2:
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>')
                else:
                    shared.append(value)
                    cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        parts[f"xl/worksheets/sheet{n}.xml"] = f"<worksheet {ns}><sheetData>{''.join(out)}</sheetData></worksheet>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, (name, _) in enumerate(sheets, start=1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"/>' for n in range(1, len(sheets) + 1)) + "</Relationships>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for name, xml in parts.items():
            z.writestr(name, xml)


def test_excel_workbooks_are_read_one_sheet_per_category(tmp_path):
    _xlsx(tmp_path / "faq.xlsx", [("Billing", [["Question", "Answer", "id"], ["How do I pay?", "By card.", 1], ["Can I get an invoice?", "Yes, by email.", 2]]),
                                  ("Notes", [["Remark", "Who"], ["internal only", "me"]]),
                                  ("Shipping", [["Question", "Answer", "id"], ["Do you ship abroad?", "Yes.", 3]])])
    kb = load_kb(tmp_path / "faq.xlsx")
    faq = [e for e in kb.entries if e.kind == "faq"]
    assert [e.question for e in faq] == ["How do I pay?", "Can I get an invoice?", "Do you ship abroad?"]  # the sheets with questions are FAQ entries...
    assert [e.id for e in faq] == ["1", "2", "3"] and faq[0].answer == "By card."
    notes = [e for e in kb.entries if e.kind == "passage"]  # ...and the sheet of notes is knowledge too, read as records, not thrown away
    assert len(notes) == 1 and notes[0].source == "faq.xlsx" and "internal only" in notes[0].answer and notes[0].question.startswith("Notes")
    with pytest.raises(ValueError, match="not a readable Excel"):
        load_kb(write(tmp_path, "fake.xlsx", "not a zip"))


def test_messy_web_pages_do_not_break_the_reader():
    from faqengine.sources import read_html

    messy = ["<p>unclosed <b>bold <i>italic", "<nav><ul><li>menu", "</div></div></p>stray ends", "<<<>>><p>ok</p>", "\x00\x01 binary \xff", "<form><p>inside a form</p>",
             "<main>" + "<p>word </p>" * 5000 + "</main>", "<p>caf&eacute; &amp; cr&egrave;me &#8364;5 &unknown;</p>", "<table><tr><td>a<td>b<tr><td>c</table>", "<ul>" + "<li>x" * 3000 + "</ul>"]
    for page in messy:
        assert isinstance(read_html(page).body_lines(), list)  # whatever the page is, the reader returns text and does not raise or hang
    assert " ".join(read_html("<script>if (a<b) { x = '<p>no</p>' }</script><p>kept</p>").body_lines()) == "kept"
    assert read_html("<p>a</p><footer>skip me").body_lines() == ["a"]  # an unclosed footer hides the rest, but nothing breaks
    assert read_html("<p>café &amp; crème &#8364;5</p>").body_lines() == ["café & crème €5"]


# ---- headers that are not the usual names: columns are recognised by what they hold, and tables of values are read row by row
PRICING = "Plan,Monthly price (USD),Seats,Includes\nStarter,9,1,Email support\nPro,20,5,\"SSO, API access and priority support\"\nBusiness,45,25,Dedicated manager\n"


def test_a_table_of_values_is_read_row_by_row_and_a_price_question_can_be_answered(tmp_path):
    kb = load_kb(write(tmp_path, "pricing_plans.csv", PRICING))
    assert [e.kind for e in kb.entries] == ["passage"] * 3 and [e.question for e in kb.entries] == ["Starter", "Pro", "Business"]
    pro = kb.entries[1]
    assert pro.answer == "Pro plan, monthly price: 20 USD. Pro plan, seats: 5. Pro plan, includes: SSO, API access and priority support."  # the unit in the header goes with the bare number; each sentence names the row
    assert pro.source == "pricing_plans.csv" and sources.kind_of(tmp_path / "pricing_plans.csv") == "records"
    hit = {"entry_id": pro.id, "question": pro.question, "answer": pro.answer, "score": 0.6, "matched": pro.question, "matched_kind": "q", "kind": "passage", "source": pro.source}
    r = ev.respond("How much is the Pro plan?", [], [hit])
    assert r["outcome"] == "grounded" and "20 USD" in r["answer"]
    seats = ev.respond("How many seats does the Pro plan have?", [], [hit])
    assert seats["outcome"] == "grounded" and "seats: 5" in seats["answer"]
    elsewhere = ev.respond("How much is the Enterprise plan?", [], [hit])
    assert elsewhere["outcome"] != "grounded" or "Enterprise" not in (elsewhere["answer"] or "")  # nothing invented for a plan that is not there


def test_a_question_column_with_an_unusual_name_is_found_by_what_it_holds(tmp_path):
    kb = load_kb(write(tmp_path, "help.csv", "Customer asks,Our reply,Team\nHow do I reset my password?,Use the link on the sign-in page and follow the e-mail.,Support\n"
                                              "Can I change my plan later?,Yes, from the billing page at any time.,Billing\n"))
    assert [e.kind for e in kb.entries] == ["faq", "faq"] and kb.entries[0].question == "How do I reset my password?" and kb.entries[0].answer.startswith("Use the link")
    only_questions = load_kb(write(tmp_path, "q.csv", "What we are asked,Notes\nWhere is your shop?,Near the station and open every day of the week from nine\n"))
    assert only_questions.entries[0].kind == "faq" and only_questions.entries[0].question == "Where is your shop?" and only_questions.entries[0].answer.startswith("Near the station")


def test_a_table_of_prose_with_unusual_names_is_a_table_of_documents(tmp_path):
    long = "This article explains in some detail how deliveries are scheduled, how the courier is chosen and what to do when nobody is at home to receive the parcel."
    kb = load_kb(write(tmp_path, "articles.csv", f"Heading line,Article body\nDeliveries,{long}\nReturns,{long.replace('deliveries', 'returns')}\n"))
    assert [e.kind for e in kb.entries] == ["passage", "passage"] and [e.question for e in kb.entries] == ["Deliveries", "Returns"]


def test_names_that_are_recognised_are_still_used_first(tmp_path):
    kb = load_kb(write(tmp_path, "usual.csv", "question,answer,notes\nWhat is it?,A thing.,A much longer note that is not the answer but is longer than it by a good margin.\n"))
    assert kb.entries[0].answer == "A thing."


def test_test_questions_are_still_refused_when_the_question_column_is_only_guessed(tmp_path):
    with pytest.raises(ValueError, match="test questions"):
        load_kb(write(tmp_path, "t.csv", "Customer asks,expected_behavior\nHow do I reset my password?,answer_from_retrieved_source\nWhat is your return window?,answer_from_retrieved_source\n"))


def test_the_columns_can_be_chosen_by_hand_and_everything_downstream_just_reads_them(tmp_path):
    path = write(tmp_path, "weird.csv", "Col A,Col B,Col C\nWhat time do you open?,Nine to five.,Hours\nWhere are you?,Main Street.,Place\n")
    info = sources.describe_table(path)
    assert info["columns"] == ["col_a", "col_b", "col_c"] and info["mode"] == "faq" and info["detected"]["question"] == "col_a" and info["rows"] == 2
    # the guess is overridden: read column C as the questions' category and B as the answer; or read each row as a record
    data = sources.remap_table(path, {"as": "faq", "question": "col_a", "answer": "col_b", "category": "col_c"})
    mapped = tmp_path / "mapped.csv"
    mapped.write_bytes(data)
    kb = load_kb(mapped)
    assert kb.entries[0].question == "What time do you open?" and kb.entries[0].answer == "Nine to five." and kb.entries[0].category == "Hours"
    rec = tmp_path / "rec.csv"
    rec.write_bytes(sources.remap_table(path, {"as": "records", "title": "col_c"}))
    kb = load_kb(rec)
    assert [e.kind for e in kb.entries] == ["passage", "passage"] and kb.entries[0].question == "Hours" and "col a: What time do you open?." in kb.entries[0].answer
    docs = tmp_path / "docs.csv"
    docs.write_bytes(sources.remap_table(path, {"as": "documents", "title": "col_c", "text": "col_b"}))
    assert load_kb(docs).entries[0].answer == "Nine to five."
    for bad, msg in (({"as": "faq"}, "questions"), ({"as": "faq", "question": "nope"}, "no column"), ({"as": "faq", "question": "col_a", "answer": "col_a"}, "cannot be the same"),
                     ({"as": "documents"}, "text"), ({"as": "sideways"}, "how each row")):
        with pytest.raises(ValueError, match=msg):
            sources.remap_table(path, bad)
