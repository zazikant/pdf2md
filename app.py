"""
PDF to Markdown Converter — Powered by MiMo v2.5 + PyMuPDF
==========================================================
A Streamlit app that converts PDFs to Markdown using AI vision.

Pipeline:
  1. PDF -> page images (PyMuPDF/fitz, pure Python, no system deps)
  2. Each page image -> MiMo v2.5 vision API -> structured Markdown
  3. All pages stitched together into a single .md file

No database needed. No system binaries needed. Deploys anywhere.
"""

import io
import base64
import time
import html as html_mod

import fitz  # PyMuPDF
import requests
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image


# -- Config -------------------------------------------------------------------

OPENCODE_API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5"
SUPPORTED_EXTENSIONS = ["pdf", "png", "jpg", "jpeg", "webp", "docx", "xlsx", "pptx"]
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

# Thresholds for large-document handling
LARGE_DOC_CHARS = 50_000       # warn above this
TRUNCATE_PREVIEW_CHARS = 5_000 # chars shown in rendered preview for huge docs


# -- AI Extraction Prompts ---------------------------------------------------

SYSTEM_PROMPT = """You are a precise document-to-Markdown conversion engine. You receive page images and output clean, structured Markdown.

CRITICAL RULES — YOU MUST FOLLOW ALL OF THESE:

1. TABLES — Always use proper GitHub-flavored Markdown table syntax:
   - Header row with pipe-separated column names
   - Separator row with | --- | (one --- per column)
   - Data rows with pipe-separated values
   - EVERY row must have the same number of pipe-separated columns
   - Example:
     | Action | Windows | Mac |
     | --- | --- | --- |
     | Razor Tool | C | C |
     | Selection Tool | V | V |
     | Add Edit / Cut | Ctrl+K | Cmd+K |
   - NEVER output tables as plain text, tab-separated values, or visual ASCII art
   - NEVER skip the separator row (| --- |)
   - If a cell is empty, just leave it blank between pipes: | text |  |

2. INFOGRAPHICS, FLOWCHARTS, DIAGRAMS, ILLUSTRATIONS — When you see any visual
   diagram, flowchart, org chart, process map, architecture diagram, decision tree,
   workflow, pipeline, sequence diagram, state machine, or infographic that shows
   relationships between elements, you MUST convert it to a Mermaid diagram code block.
   - Wrap in ```mermaid ... ``` code fences
   - Use flowchart TD (top-down) by default unless the visual suggests a different orientation
   - Use LR (left-right) for timelines, horizontal processes
   - Use subgraphs to group related nodes (teams, systems, phases)
   - Add clear node labels that match the original visual
   - Preserve all decision diamonds, branches, and loops
   - Example conversion:
     If you see a flowchart showing: Start -> Review -> Approved? -> Yes -> Deploy / No -> Revise -> Review
     Output:
     ```mermaid
     flowchart TD
        A[Start] --> B[Review]
        B --> C{Approved?}
        C -->|Yes| D[Deploy]
        C -->|No| E[Revise]
        E --> B
     ```
   - For org charts / hierarchy: use flowchart TD with subgraphs
   - For timelines: use flowchart LR with descriptive nodes
   - For system architecture: use flowchart LR with subgraphs for each system/component
   - ALWAYS convert visual diagrams to Mermaid — do NOT describe them in prose

3. GENERAL FORMATTING:
   - Preserve all headings (use # ## ### etc. matching original hierarchy)
   - Preserve lists (bulleted and numbered)
   - Preserve bold, italic, and other inline formatting
   - Code blocks get proper language tags
   - Mathematical formulas use LaTeX: inline $...$ and display $$...$$
   - Preserve document structure faithfully

4. OUTPUT FORMAT:
   - Output ONLY the Markdown content
   - No commentary, no explanations, no "Here is the converted text" preamble
   - No page headers or separators — just the page content
"""

PAGE_PROMPT_TEMPLATE = (
    "{context}"
    "Convert this document page image to clean, structured Markdown.\n\n"
    "Follow these rules strictly:\n"
    "1. Tables MUST use proper Markdown table syntax with | header | --- | rows.\n"
    "2. Infographics, flowcharts, diagrams, process maps -> convert to ```mermaid``` code blocks.\n"
    "3. Preserve headings, lists, formatting, and document structure faithfully.\n"
    "4. Output ONLY Markdown — no commentary or explanation."
)

IMAGE_PROMPT = (
    "Convert this image to clean, structured Markdown.\n\n"
    "Follow these rules strictly:\n"
    "1. Tables MUST use proper Markdown table syntax with | header | --- | rows.\n"
    "2. Infographics, flowcharts, diagrams, process maps -> convert to ```mermaid``` code blocks.\n"
    "3. Preserve headings, lists, formatting, and document structure faithfully.\n"
    "4. Output ONLY Markdown — no commentary or explanation."
)


# -- Clipboard Helper (from Dspy-app pattern) ---------------------------------

def copy_button(text: str, label: str = "Copy to Clipboard", key_suffix: str = ""):
    """Clipboard copy that works for arbitrarily large outputs (32k+).

    Key technique: the full text is written into a hidden <textarea> via
    html.escape() at render time. The JS reads from that DOM node — not a JS
    string literal — so there is no browser template-literal size cap or
    Streamlit iframe serialization truncation regardless of output length.

    Falls back from navigator.clipboard.writeText() to
    document.execCommand('copy') for older browsers.
    """
    encoded = html_mod.escape(text, quote=True)
    uid = abs(hash(text + key_suffix)) % 1_000_000
    btn_id = f"copy-btn-{uid}"
    ta_id = f"copy-ta-{uid}"

    components.html(
        f"""
        <textarea id="{ta_id}"
            style="position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;"
        >{encoded}</textarea>
        <button id="{btn_id}"
            style="background:#1f77b4;color:white;border:none;padding:10px 20px;
                   border-radius:6px;font-size:14px;cursor:pointer;width:100%;margin-top:4px;">
            {label}
        </button>
        <script>
        (function() {{
            var btn = document.getElementById('{btn_id}');
            var ta  = document.getElementById('{ta_id}');
            btn.addEventListener('click', function() {{
                var txt = ta.value;
                function markDone() {{
                    btn.innerText = 'Copied!';
                    btn.style.background = '#2d6a2d';
                    setTimeout(function() {{
                        btn.innerText = '{label}';
                        btn.style.background = '#1f77b4';
                    }}, 2000);
                }}
                if (navigator.clipboard && navigator.clipboard.writeText) {{
                    navigator.clipboard.writeText(txt).then(markDone).catch(function() {{
                        ta.style.cssText = 'position:static;width:100%;height:2px;';
                        ta.select();
                        document.execCommand('copy');
                        ta.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;';
                        markDone();
                    }});
                }} else {{
                    ta.style.cssText = 'position:static;width:100%;height:2px;';
                    ta.select();
                    document.execCommand('copy');
                    ta.style.cssText = 'position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;';
                    markDone();
                }}
            }});
        }})();
        </script>
        """,
        height=60,
    )


# -- Scrollable Preview Container --------------------------------------------

PREVIEW_HEIGHT_PX = 480  # fixed height for the preview pane


def safe_markdown_display(text: str, view_mode: str = "Rendered"):
    """Display markdown output inside a fixed-height scrollable container.

    The container has its own scrollbar so the page doesn't grow endlessly.
    Download / copy buttons live OUTSIDE this container so they are always
    visible without scrolling.

    For very large documents (> LARGE_DOC_CHARS), rendered view is truncated
    to avoid rendering 100k+ DOM nodes inside the container.
    """
    char_count = len(text)
    is_large = char_count > LARGE_DOC_CHARS

    with st.container(height=PREVIEW_HEIGHT_PX):
        if is_large:
            st.warning(
                f"Large document ({char_count:,} chars). "
                "Preview truncated for performance. Use Download or Copy for full output."
            )

        if view_mode == "Rendered":
            if is_large:
                preview = text[:TRUNCATE_PREVIEW_CHARS]
                st.markdown(
                    f"{preview}\n\n> ... *(truncated — {char_count - TRUNCATE_PREVIEW_CHARS:,} more chars)*"
                )
            else:
                st.markdown(text)
        else:  # Raw Markdown view
            st.code(text, language="markdown")


# -- PDF -> Images using PyMuPDF ----------------------------------------------

def pdf_to_images(pdf_bytes: bytes, dpi: int = 200) -> list:
    """Convert PDF bytes to a list of PIL Images, one per page."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    zoom = dpi / 72

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        images.append(img)

    doc.close()
    return images


def image_to_base64(img: Image.Image, fmt: str = "PNG") -> str:
    """Convert a PIL Image to a base64 string."""
    buffer = io.BytesIO()
    img.save(buffer, format=fmt)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# -- MiMo v2.5 API ------------------------------------------------------------

def call_mimo_vision(image: Image.Image, api_key: str, prompt: str, mime_type: str = "image/png") -> str:
    """Send an image to MiMo v2.5 vision and get text back."""
    fmt = "PNG" if "png" in mime_type else "JPEG"
    b64 = image_to_base64(image, fmt=fmt)

    user_content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
    ]

    resp = requests.post(
        OPENCODE_API_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": MIMO_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 16384,
        },
        timeout=120,
    )

    if resp.status_code in (401, 403):
        raise PermissionError(f"Auth error ({resp.status_code}): {resp.text[:200]}")
    if not resp.ok:
        raise RuntimeError(f"API error ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def parse_page_with_mimo(image: Image.Image, api_key: str, page_num: int, total_pages: int) -> str:
    """Send a PDF page image to MiMo v2.5 and get Markdown back."""
    context = f"This is page {page_num} of {total_pages} from a document. " if total_pages > 1 else ""
    prompt = PAGE_PROMPT_TEMPLATE.format(context=context)
    return call_mimo_vision(image, api_key, prompt, mime_type="image/png")


def parse_standalone_image_with_mimo(image: Image.Image, api_key: str, mime_type: str = "image/png") -> str:
    """Send a standalone image (PNG/JPG) to MiMo v2.5 for OCR."""
    return call_mimo_vision(image, api_key, IMAGE_PROMPT, mime_type=mime_type)


# -- Local text extraction (PyMuPDF) ------------------------------------------

def extract_text_local(pdf_bytes: bytes) -> str:
    """Extract text from PDF using PyMuPDF locally (no API needed)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_text = []

    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if doc.page_count > 1:
            pages_text.append(f"---\n\n**Page {i + 1}**\n\n{text}")
        else:
            pages_text.append(text)

    doc.close()
    result = "\n\n".join(pages_text)
    if result.startswith("---\n\n"):
        result = result[5:]
    return result


# -- Stitch pages --------------------------------------------------------------

def stitch_pages(page_markdowns: list) -> str:
    """Combine per-page Markdown results into a single document."""
    if len(page_markdowns) == 1:
        return page_markdowns[0] or ""
    parts = []
    for idx, md in enumerate(page_markdowns):
        parts.append(f"---\n\n**Page {idx + 1}**\n\n{md}")
    result = "\n\n".join(parts)
    if result.startswith("---\n\n"):
        result = result[5:]
    return result


# -- Streamlit UI --------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="PDF to Markdown",
        page_icon="📄",
        layout="wide",
    )

    st.title("📄 PDF to Markdown")
    st.caption("MiMo v2.5 AI Vision + PyMuPDF — Smart multi-page handling, no system deps needed")

    # -- Sidebar ---------------------------------------------------------------
    with st.sidebar:
        st.header("⚙️ Settings")

        api_key = st.text_input(
            "OpenCode Go API Key",
            type="password",
            placeholder="sk-... (from opencode.ai/go)",
            help="Required for MiMo v2.5. Get yours at opencode.ai/go",
        )

        st.markdown("---")

        parse_mode = st.radio(
            "Parser",
            options=["mimo", "local"],
            format_func=lambda x: "🤖 MiMo v2.5 (AI Vision)" if x == "mimo" else "💻 Local (PyMuPDF, no API key)",
            help="MiMo: AI-powered vision for any file type. Local: Text extraction only, no API key needed.",
        )

        if parse_mode == "mimo" and not api_key:
            st.warning("⚠️ MiMo v2.5 requires an API key above.")

        st.markdown("---")

        dpi = st.slider(
            "PDF Render DPI",
            min_value=100,
            max_value=300,
            value=200,
            step=25,
            help="Higher DPI = better quality but larger images & slower API calls. 200 is a good balance.",
        )

        st.markdown("---")
        st.markdown(
            """
            **How it works:**
            1. PDF → page images (PyMuPDF)
            2. Each page → MiMo v2.5 vision
            3. Results stitched into one .md

            **No database. No system binaries. Pure Python.**
            """
        )

    # -- Main Area -------------------------------------------------------------
    col_upload, col_output = st.columns([1, 1.3])

    with col_upload:
        st.subheader("📤 Upload")

        uploaded_file = st.file_uploader(
            "Choose a file",
            type=SUPPORTED_EXTENSIONS,
            label_visibility="collapsed",
        )

        if uploaded_file:
            file_ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
            file_size_mb = uploaded_file.size / (1024 * 1024)

            st.success(f"**{uploaded_file.name}** ({file_size_mb:.1f} MB)")

            can_parse = True
            if parse_mode == "mimo" and not api_key:
                st.error("MiMo v2.5 requires an API key. Add it in the sidebar.")
                can_parse = False
            if parse_mode == "local" and file_ext in IMAGE_EXTENSIONS:
                st.error("Local mode can't process images. Switch to MiMo v2.5.")
                can_parse = False

            parse_btn = st.button(
                "🚀 Parse",
                type="primary",
                use_container_width=True,
                disabled=not can_parse,
            )
        else:
            parse_btn = False
            st.info("Upload a PDF, DOCX, XLSX, PPTX, or image file.")

            fmt_cols = st.columns(6)
            for i, fmt in enumerate(["PDF", "DOCX", "XLSX", "PPTX", "PNG", "JPG"]):
                with fmt_cols[i]:
                    st.markdown(
                        f'<div style="text-align:center; background:var(--secondary-background); '
                        f'border-radius:9999px; padding:0.25rem 0.5rem; font-size:0.75rem; '
                        f'font-weight:500;">{fmt}</div>',
                        unsafe_allow_html=True,
                    )

    # -- Parse Logic -----------------------------------------------------------
    if parse_btn and uploaded_file:
        file_bytes = uploaded_file.read()
        file_ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
        file_basename = uploaded_file.name.rsplit(".", 1)[0]

        with col_output:
            st.subheader("📝 Markdown Output")

        # -- PDF with MiMo: Smart multi-page ----------------------------------
        if file_ext == "pdf" and parse_mode == "mimo":
            with st.status("Converting PDF to page images...", expanded=True) as status:
                t0 = time.time()
                try:
                    page_images = pdf_to_images(file_bytes, dpi=dpi)
                except Exception as e:
                    st.error(f"Failed to convert PDF: {e}")
                    st.stop()

                total = len(page_images)
                status.update(label=f"Processing {total} page{'s' if total != 1 else ''} with MiMo v2.5...")

                progress_bar = st.progress(0, text=f"Page 0/{total}")

                num_cols = min(total, 25)
                if num_cols > 0:
                    page_cols = st.columns(num_cols)
                    page_indicators = []
                    for i in range(num_cols):
                        with page_cols[i]:
                            if i < total:
                                page_indicators.append(st.empty())

                all_markdown = []
                error_count = 0

                for i, img in enumerate(page_images):
                    try:
                        md = parse_page_with_mimo(img, api_key, page_num=i + 1, total_pages=total)
                        all_markdown.append(md)
                        if i < len(page_indicators):
                            page_indicators[i].markdown("✅")
                    except PermissionError as e:
                        st.error(f"🔑 Auth failed: {e}")
                        st.stop()
                    except Exception as e:
                        all_markdown.append(f"> **Error on page {i + 1}:** {e}")
                        error_count += 1
                        if i < len(page_indicators):
                            page_indicators[i].markdown("❌")

                    progress_bar.progress(
                        (i + 1) / total,
                        text=f"Page {i + 1}/{total} done",
                    )

                final_md = stitch_pages(all_markdown)
                elapsed = time.time() - t0

                method = f"MiMo v2.5 Vision — {total} page{'s' if total != 1 else ''}"
                if error_count > 0:
                    method += f" ({error_count} errors)"

                status.update(
                    label=f"✅ Done in {elapsed:.1f}s — {method}",
                    state="complete",
                )

        # -- Standalone image with MiMo ----------------------------------------
        elif file_ext in IMAGE_EXTENSIONS and parse_mode == "mimo":
            with st.status("Processing image with MiMo v2.5...", expanded=True) as status:
                t0 = time.time()
                mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
                mime = mime_map.get(file_ext, "image/png")

                img = Image.open(io.BytesIO(file_bytes))
                try:
                    final_md = parse_standalone_image_with_mimo(img, api_key, mime_type=mime)
                except PermissionError as e:
                    st.error(f"🔑 Auth failed: {e}")
                    st.stop()
                except Exception as e:
                    st.error(f"Failed: {e}")
                    st.stop()

                elapsed = time.time() - t0
                method = "MiMo v2.5 Vision (OpenCode Go)"
                status.update(label=f"✅ Done in {elapsed:.1f}s", state="complete")

        # -- Local PDF extraction -----------------------------------------------
        elif file_ext == "pdf" and parse_mode == "local":
            with st.status("Extracting text locally with PyMuPDF...", expanded=True) as status:
                t0 = time.time()
                try:
                    final_md = extract_text_local(file_bytes)
                except Exception as e:
                    st.error(f"Failed: {e}")
                    st.stop()

                if not final_md.strip():
                    st.warning("No text found. It might be scanned — try MiMo v2.5 for AI vision.")
                    st.stop()

                elapsed = time.time() - t0
                method = "PyMuPDF (local, no API)"
                status.update(label=f"✅ Done in {elapsed:.1f}s", state="complete")

        else:
            st.error("This file type + mode combination isn't supported yet.")
            st.stop()

        # -- Output Display (buttons first, scrollable preview below) ----------
        with col_output:
            if not final_md or not final_md.strip():
                st.warning("No content could be extracted.")
                st.stop()

            char_count = len(final_md)
            st.caption(f"🔧 {method} · {char_count:,} chars")

            # Action buttons FIRST — always visible, no scrolling needed
            dl_cols = st.columns(2)
            with dl_cols[0]:
                st.download_button(
                    "📥 Download .md",
                    data=final_md,
                    file_name=f"{file_basename}.md",
                    mime="text/markdown",
                    use_container_width=True,
                )
            with dl_cols[1]:
                copy_button(final_md, "📋 Copy Full Markdown", key_suffix=file_basename)

            # View toggle (compact, right next to buttons)
            view = st.radio("Preview", ["Rendered", "Raw Markdown"], horizontal=True, key="view_toggle", label_visibility="collapsed")

            # Scrollable preview — fixed height, own scrollbar
            safe_markdown_display(final_md, view_mode=view)

    else:
        # Show placeholder when no file is parsed yet
        with col_output:
            st.subheader("📝 Markdown Output")
            st.info("Parsed Markdown will appear here after you upload and parse a file.")

            with st.expander("💡 How smart multi-page works"):
                st.markdown(
                    """
                    1. **PDF → Page Images**: Each page is rendered to a high-quality PNG using PyMuPDF (pure Python, no system deps)
                    2. **Per-Page AI Vision**: Each page image is sent to MiMo v2.5 for intelligent text extraction & structuring
                    3. **Stitch**: All page Markdown results are combined into a single document with page separators

                    **Why page-by-page?**
                    - Better accuracy — each page gets focused AI attention
                    - Real-time progress — see results as each page completes
                    - Graceful error handling — one bad page doesn't break the whole document
                    - No size limits — handle 100+ page PDFs

                    **No database needed. No system binaries. Pure Python. Deploys anywhere.**
                    """
                )


if __name__ == "__main__":
    main()
