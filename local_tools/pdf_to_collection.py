"""
Local-only utility: turn a scanned/photographed PDF worksheet into a collection
JSON file ready to paste into the admin dashboard's "Filled-in template (JSON)"
textarea, plus any oral-picture/listening-practice pages exported as images.

The source PDFs are scanned images with no text layer, so this sends pages to
Claude as images (Claude reads them visually, which is far more reliable for
Devanagari script than a separate OCR step).

Two modes:
- Scoped (--section "Name:pages", repeatable): you tell it which pages belong
  to which of the 4 sections (and optionally --oral / --listening for those
  page ranges). No categorization guessing — each call is told exactly what
  it's looking at. Use this once you've reviewed the pages yourself; it's
  much more reliable than auto mode.
- Auto (no --section given): sends the whole PDF and asks Claude to both
  extract the 4 sections AND classify oral/listening pages itself. Simpler,
  but categorization can be unreliable on complex worksheets — verify the
  output before trusting it.

Runs entirely on your own machine — never touches Railway, never calls the
production server. Requires an Anthropic API key in the ANTHROPIC_API_KEY
environment variable (get one at https://console.anthropic.com/).

Usage:
    pip install -r local_tools/requirements.txt
    set ANTHROPIC_API_KEY=sk-ant-...

    # Auto mode
    python local_tools/pdf_to_collection.py "My Worksheet.pdf" --title "Collection 15"

    # Scoped mode (recommended once you know the page layout)
    python local_tools/pdf_to_collection.py "My Worksheet1.pdf" --title "Collection 16" \\
        --section "Language Use:5,6,15" \\
        --section "Cloze Comprehension:7-8" \\
        --section "Comprehension:9-13" \\
        --oral 22-24
"""
import argparse
import base64
import io
import json
import os
import sys

import anthropic
import pypdfium2 as pdfium

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from proxy import _validate_collection_template, REQUIRED_COLLECTION_SECTIONS  # noqa: E402

MAX_PDF_BYTES = 24 * 1024 * 1024  # base64 inflates ~4/3; keeps the request under the 32MB API limit

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "passage": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "opts": {"type": "array", "items": {"type": "string"}},
                    "ans": {"type": "integer"},
                },
                "required": ["q", "opts", "ans"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["passage", "questions"],
    "additionalProperties": False,
}

SECTIONS_SYSTEM_PROMPT = """You are extracting structured practice-question data from a scanned \
PDF worksheet — read the page images directly, the same way you would read a photograph. The \
source is a Hindi-language PSLE practice worksheet with up to 4 sections named exactly: \
"Language Use", "Cloze Comprehension", "Comprehension", "Vocabulary". "Cloze Comprehension" is a \
single passage with numbered blanks in it (e.g. ___11___, ___12___, ...) and answer options for \
each blank — treat any such passage as Cloze Comprehension even if the page itself doesn't label \
it that way, and extract every numbered blank in it as its own question.

For each section, extract:
- passage: the reading passage / cloze text, exactly as written
- questions: a list of {q, opts, ans} where ans is the 0-indexed position of the correct answer \
within opts

Critical rules:
- Never invent, fabricate, or guess content not present in the source. If a section is not present \
in the scan at all, or you cannot confidently read complete data for it (e.g. a blurry or cut-off \
page), output that section with an empty passage and an empty questions array — do not fill it in.
- Transcribe the original Hindi text exactly, including punctuation and matras. Take particular \
care with visually similar Devanagari characters when the scan quality is poor. Never rewrite or \
correct the language.
- Use answer markings visible in the scan (a circled/underlined option, an asterisk, a separate \
answer key page) to set ans where present. If no marking is visible anywhere for a question, prefer \
leaving that question out over guessing.
- Extract only the 4 named sections above; ignore cover pages and instructions, except to read an \
answer key page."""

PAGES_SYSTEM_PROMPT = """You are classifying pages in a scanned PDF worksheet — read the page \
images directly, the same way you would read a photograph. Identify:
- oral_picture_pages: page numbers (1-indexed) showing a photo/illustration for the student to \
describe aloud, not a written question
- listening_practice_pages: page numbers (1-indexed) that are a listening-practice exercise (an \
audio-transcript/script, or comprehension questions tied to an audio recording rather than a \
reading passage)

Only report pages that clearly match one of these two categories. If neither category appears \
anywhere in the document, return empty lists for both."""


def parse_page_spec(spec):
    """Parse "5,6,15" or "7-8" or "1-3,5" into a sorted list of page numbers."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            pages.update(range(int(start), int(end) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


def build_sections_schema():
    names = sorted(REQUIRED_COLLECTION_SECTIONS)
    return {
        "type": "object",
        "properties": {name: SECTION_SCHEMA for name in names},
        "required": names,
        "additionalProperties": False,
    }


def build_pages_schema():
    return {
        "type": "object",
        "properties": {
            "oral_picture_pages": {"type": "array", "items": {"type": "integer"}},
            "listening_practice_pages": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["oral_picture_pages", "listening_practice_pages"],
        "additionalProperties": False,
    }


def render_page_images(pdf_path, page_numbers):
    pdf = pdfium.PdfDocument(pdf_path)
    blocks = []
    for page_num in page_numbers:
        if not (1 <= page_num <= len(pdf)):
            continue
        page = pdf[page_num - 1]
        bitmap = page.render(scale=2.0)
        img = bitmap.to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(buf.getvalue()).decode("utf-8"),
            },
        })
    return blocks


def _call_claude(content_blocks, system_prompt, schema):
    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model="claude-sonnet-5",
            max_tokens=64000,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            system=system_prompt,
            messages=[{"role": "user", "content": content_blocks}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.BadRequestError as e:
        print(f"Error: request rejected by the API — {e.message}", file=sys.stderr)
        sys.exit(1)

    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        print(f"Error: Claude declined to process this content (category: {category}).",
              file=sys.stderr)
        sys.exit(1)
    if response.stop_reason == "max_tokens":
        print("Error: content too large to fully process — try scoping to fewer pages.",
              file=sys.stderr)
        sys.exit(1)

    text = next(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: Claude's response wasn't valid JSON — {e}", file=sys.stderr)
        sys.exit(1)


def extract_sections_auto(pdf_b64):
    return _call_claude(
        [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
            },
            {"type": "text", "text": "Extract the practice questions from this scanned worksheet."},
        ],
        SECTIONS_SYSTEM_PROMPT, build_sections_schema(),
    )


def classify_special_pages_auto(pdf_b64):
    return _call_claude(
        [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
            },
            {
                "type": "text",
                "text": "Identify the oral-picture and listening-practice pages in this scanned "
                        "worksheet.",
            },
        ],
        PAGES_SYSTEM_PROMPT, build_pages_schema(),
    )


def extract_section_scoped(pdf_path, page_numbers, section_name):
    image_blocks = render_page_images(pdf_path, page_numbers)
    system_prompt = f"""You are extracting structured practice-question data from specific pages \
of a scanned Hindi-language PSLE worksheet. Every page given to you in this request belongs to \
ONE section: "{section_name}". Do not try to categorize the content — everything shown IS \
"{section_name}".

Extract:
- passage: the reading/cloze passage text, exactly as written across all provided pages (empty \
string if there's no shared passage)
- questions: a list of {{q, opts, ans}} for every question found across all pages, where ans is \
the 0-indexed position of the correct answer in opts

If the pages contain numbered blanks (e.g. ___11___, ___12___, ...), extract every blank as its \
own question — don't stop partway through.

Rules:
- Extract only what's actually on the pages — never invent content.
- Transcribe Hindi text exactly, including matras. Don't rewrite or correct the language.
- Use answer markings visible in the scan (a circled/underlined option, an asterisk, a separate \
answer key page) to set ans. If no marking exists for a specific question, omit that one question \
rather than guessing — but still include every other question you can read."""
    text_block = {
        "type": "text",
        "text": f"Extract the '{section_name}' content from these pages.",
    }
    return _call_claude(image_blocks + [text_block], system_prompt, SECTION_SCHEMA)


VOCAB_WORDBANK_SYSTEM_PROMPT = """You are converting a handwritten word-matching worksheet page \
into multiple-choice questions. The page is a Hindi vocabulary exercise laid out as a table: each \
row has an SN, an English word, a Hindi word ("शब्द"), and a handwritten Hindi synonym answer \
("समानार्थी") that a student wrote in by matching it from a printed word bank shown elsewhere on \
the page. The word bank gives the canonical spelling — cross-reference it against the handwriting \
to resolve any ambiguity, since handwriting can be messy or corrected (crossed out, circled, \
rewritten). A checkmark next to a word bank entry confirms that match was graded correct.

For every row on this page, produce one multiple-choice question:
- q: the Hindi "शब्द" column value for that row (not the English word)
- opts: 4 options — the correct synonym (the resolved handwritten answer) plus 3 distractors drawn \
from OTHER rows' correct synonyms on this SAME page (this page's own word bank pool only — never \
invent a word that isn't actually in this page's word bank)
- ans: the 0-indexed position of the correct synonym within opts

Rules:
- Only include a row if you can confidently resolve its handwritten answer (using the word bank to \
disambiguate). Skip a row entirely rather than guessing if the handwriting is illegible and no \
checkmark/word-bank cross-reference resolves it.
- Never invent vocabulary not present on the page.
- Preserve exact Hindi spelling and matras."""

VOCAB_WORDBANK_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "q": {"type": "string"},
                    "opts": {"type": "array", "items": {"type": "string"}},
                    "ans": {"type": "integer"},
                },
                "required": ["q", "opts", "ans"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def extract_vocab_wordbank_page(pdf_path, page_num):
    image_blocks = render_page_images(pdf_path, [page_num])
    text_block = {
        "type": "text",
        "text": "Convert this word-matching page into multiple-choice questions.",
    }
    result = _call_claude(image_blocks + [text_block], VOCAB_WORDBANK_SYSTEM_PROMPT, VOCAB_WORDBANK_SCHEMA)
    return result["questions"]


def export_pages(pdf_path, page_numbers, stem, label):
    if not page_numbers:
        return []
    pdf = pdfium.PdfDocument(pdf_path)
    written = []
    for page_num in page_numbers:
        if not (1 <= page_num <= len(pdf)):
            continue
        page = pdf[page_num - 1]
        bitmap = page.render(scale=2.0)
        img = bitmap.to_pil()
        out_path = f"{stem}_{label}_page{page_num}.png"
        img.save(out_path)
        written.append(out_path)
    return written


def run_scoped(args, stem):
    empty_section = {"passage": "", "questions": []}
    sections = {name: dict(empty_section) for name in REQUIRED_COLLECTION_SECTIONS}

    for spec in args.section:
        if ":" not in spec:
            print(f"Error: --section must be 'Name:pages', got {spec!r}", file=sys.stderr)
            sys.exit(1)
        name, page_spec = spec.split(":", 1)
        name = name.strip()
        if name not in REQUIRED_COLLECTION_SECTIONS:
            print(f"Error: unknown section {name!r} — must be one of "
                  f"{sorted(REQUIRED_COLLECTION_SECTIONS)}", file=sys.stderr)
            sys.exit(1)
        pages = parse_page_spec(page_spec)
        print(f"Extracting '{name}' from page(s) {pages}...")
        sections[name] = extract_section_scoped(args.pdf_path, pages, name)

    oral_pages = parse_page_spec(args.oral) if args.oral else []
    listening_pages = parse_page_spec(args.listening) if args.listening else []
    return sections, oral_pages, listening_pages


def run_auto(args):
    with open(args.pdf_path, "rb") as f:
        pdf_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    print("Extracting the 4 written sections (this can take a minute)...")
    sections = extract_sections_auto(pdf_b64)

    print("Classifying oral-picture / listening-practice pages...")
    pages_result = classify_special_pages_auto(pdf_b64)
    oral_pages = pages_result.get("oral_picture_pages") or []
    listening_pages = pages_result.get("listening_practice_pages") or []
    return sections, oral_pages, listening_pages


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf_path", help="Path to the scanned PDF worksheet")
    parser.add_argument("--title", help="Collection title (defaults to the PDF filename)")
    parser.add_argument("-o", "--output", help="Output JSON path (defaults to <pdf-stem>.json)")
    parser.add_argument(
        "--section", action="append", default=[],
        help='Scope one section to specific pages, e.g. --section "Cloze Comprehension:7-8". '
             "Repeatable. If any --section is given, runs in scoped mode.")
    parser.add_argument("--oral", help="Page(s) that are oral-picture prompts, e.g. 22-24")
    parser.add_argument("--listening", help="Page(s) that are listening-practice, e.g. 18-21")
    parser.add_argument(
        "--vocab-wordbank",
        help="Convert handwritten word-matching pages into MCQ vocabulary questions. Each page is "
             "treated as its own self-contained exercise (own word bank) — writes one "
             "<stem>_vocab_page<N>.json fragment per page (just {\"questions\": [...]}, not a full "
             "collection template, since these are meant to be combined with other sections later). "
             "e.g. --vocab-wordbank 1-8")
    args = parser.parse_args()

    if not os.path.isfile(args.pdf_path):
        print(f"Error: file not found — {args.pdf_path}", file=sys.stderr)
        sys.exit(1)

    size = os.path.getsize(args.pdf_path)
    if size > MAX_PDF_BYTES:
        print(f"Error: PDF is {size / 1024 / 1024:.1f}MB (max {MAX_PDF_BYTES / 1024 / 1024:.0f}MB) "
              f"— split it into smaller files and run this on each.", file=sys.stderr)
        sys.exit(1)

    stem = os.path.splitext(os.path.basename(args.pdf_path))[0]
    title = args.title or stem
    output_path = args.output or f"{stem}.json"

    if args.vocab_wordbank:
        pages = parse_page_spec(args.vocab_wordbank)
        for page_num in pages:
            print(f"Converting page {page_num} word bank to MCQs...")
            questions = extract_vocab_wordbank_page(args.pdf_path, page_num)
            frag_path = f"{stem}_vocab_page{page_num}.json"
            with open(frag_path, "w", encoding="utf-8") as f:
                json.dump({"questions": questions}, f, ensure_ascii=False, indent=2)
            print(f"  Wrote {frag_path} ({len(questions)} questions)")
        return

    if args.section:
        sections, oral_pages, listening_pages = run_scoped(args, stem)
    else:
        sections, oral_pages, listening_pages = run_auto(args)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"title": title, "sections": sections}, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {output_path}")
    for name in sorted(sections):
        print(f"  {name}: {len(sections[name]['questions'])} questions")

    if oral_pages:
        oral_files = export_pages(args.pdf_path, oral_pages, stem, "oral")
        print(f"\nExported oral-picture page(s) {oral_pages} to:")
        for path in oral_files:
            print(f"  {path}")
        print("Review/crop these before attaching them via the admin dashboard's "
              "'Oral practice pictures' file input.")

    if listening_pages:
        listening_files = export_pages(args.pdf_path, listening_pages, stem, "listening")
        print(f"\nExported listening-practice page(s) {listening_pages} to:")
        for path in listening_files:
            print(f"  {path}")
        print("These aren't part of the JSON template yet (no listening section in the app) — "
              "review them by hand for now.")

    error = _validate_collection_template({"sections": sections})
    if error:
        print(f"\nWarning: this extraction failed validation — {error}", file=sys.stderr)
        print("The file above still has whatever extracted cleanly — open it, check the section "
              "the error names, fix or fill it in by hand, then paste it into the admin dashboard.",
              file=sys.stderr)
        sys.exit(1)

    print("\nPaste the JSON file's contents into the admin dashboard's "
          "'Filled-in template (JSON)' box and upload.")


if __name__ == "__main__":
    main()
