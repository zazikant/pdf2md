#!/usr/bin/env python3
"""
PDF/Image to Markdown — Same engine as https://pdf2md-api.streamlit.app/
Uses MiMo v2.5 AI Vision + PyMuPDF via OpenCode API.

Usage:
  # Parse a PDF file
  python pdf_to_md.py --file document.pdf --api-key sk-YOUR-KEY

  # Parse an image
  python pdf_to_md.py --file screenshot.png --api-key sk-YOUR-KEY

  # Parse a URL (requires Browserless)
  python pdf_to_md.py --url https://example.com --api-key sk-YOUR-KEY --browserless-token YOUR-TOKEN

  # Local mode (PDF text extraction, no API key)
  python pdf_to_md.py --file document.pdf --mode local

  # Specify DPI
  python pdf_to_md.py --file document.pdf --api-key sk-YOUR-KEY --dpi 300
"""

import argparse
import base64
import io
import os
import sys
import time

try:
    import fitz  # PyMuPDF
    import requests
    from PIL import Image
except ImportError:
    print("Install dependencies: pip install PyMuPDF Pillow requests")
    sys.exit(1)


# ── Config ──────────────────────────────────────────────────────────────

OPENCODE_API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MIMO_MODEL = "mimo-v2.5"
SUPPORTED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


# ── Same prompts as the Streamlit app ──────────────────────────────────

SYSTEM_PROMPT = """You are a precise document-to-Markdown conversion engine. You receive page images and output clean, structured Markdown.

CRITICAL RULES — YOU MUST FOLLOW ALL OF THESE:

1. TABLES — Always use proper GitHub-flavored Markdown table syntax:
   - Header row with pipe-separated column names
   - Separator row with | --- | (one --- per column)
   - Data rows with pipe-separated values
   - EVERY row must have the same number of pipe-separated columns
   - NEVER output tables as plain text, tab-separated values, or visual ASCII art
   - NEVER skip the separator row (| --- |)
   - If a cell is empty, just leave it blank between pipes: | text |  |

2. INFOGRAPHICS, FLOWCHARTS, DIAGRAMS, ILLUSTRATIONS — When you see any visual
   diagram, flowchart, org chart, process map, architecture diagram, decision tree,
   workflow, pipeline, sequence diagram, state machine, or infographic that shows
   relationships between elements, you MUST convert it to a Mermaid diagram code block.
   - Wrap in ```mermaid ... ``` code fences
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


# ── Core Logic (same as Streamlit app) ─────────────────────────────────

def pdf_to_images(pdf_bytes: bytes, dpi: int = 200) -> list:
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
    buffer = io.BytesIO()
    img.save(buffer, format=fmt)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def call_mimo_vision(image: Image.Image, api_key: str, prompt: str, mime_type: str = "image/png") -> str:
    fmt = "PNG" if "png" in mime_type else "JPEG"
    b64 = image_to_base64(image, fmt=fmt)
    user_content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
    ]
    resp = requests.post(
        OPENCODE_API_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": MIMO_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 16384,
        },
        timeout=180,
    )
    if resp.status_code in (401, 403):
        raise PermissionError(f"Auth error ({resp.status_code}): {resp.text[:200]}")
    if not resp.ok:
        raise RuntimeError(f"API error ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def parse_page_with_mimo(image: Image.Image, api_key: str, page_num: int, total_pages: int) -> str:
    context = f"This is page {page_num} of {total_pages} from a document. " if total_pages > 1 else ""
    prompt = PAGE_PROMPT_TEMPLATE.format(context=context)
    return call_mimo_vision(image, api_key, prompt, mime_type="image/png")


def parse_standalone_image_with_mimo(image: Image.Image, api_key: str, mime_type: str = "image/png") -> str:
    return call_mimo_vision(image, api_key, IMAGE_PROMPT, mime_type=mime_type)


def extract_text_local(pdf_bytes: bytes) -> str:
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


def stitch_pages(page_markdowns: list) -> str:
    if len(page_markdowns) == 1:
        return page_markdowns[0] or ""
    parts = []
    for idx, md in enumerate(page_markdowns):
        parts.append(f"---\n\n**Page {idx + 1}**\n\n{md}")
    result = "\n\n".join(parts)
    if result.startswith("---\n\n"):
        result = result[5:]
    return result


# ── URL Mode (requires Browserless + Playwright) ───────────────────────

def parse_url_with_browserless(url: str, api_key: str, browserless_token: str,
                                dpi: int = 200, scroll_pages: int = 10,
                                wait_for_selector: str = None) -> str:
    """Smart Browserless CDP: scroll, wait, screenshot each viewport, parse with MiMo."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError("URL mode requires playwright: pip install playwright")

    viewport_width = int(dpi * 9.6)
    cdp_url = f"wss://production-sfo.browserless.io?token={browserless_token}&blockAds=true&timeout=60000"

    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            page = await browser.new_page()
            await page.set_viewport_size({"width": viewport_width, "height": 1080})

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            if wait_for_selector:
                await page.wait_for_selector(wait_for_selector, timeout=30000)

            # Smart scroll to trigger lazy-load
            for i in range(scroll_pages):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await page.wait_for_timeout(600)
            await page.wait_for_timeout(1500)

            # Back to top
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(800)

            # Calculate screenshots needed
            full_height = await page.evaluate("document.documentElement.scrollHeight")
            vp_height = await page.evaluate("window.innerHeight")
            total = (full_height + vp_height - 1) // vp_height

            page_markdowns = []
            for i in range(total):
                await page.evaluate(f"window.scrollTo(0, {i * vp_height})")
                await page.wait_for_timeout(400)

                screenshot = await page.screenshot(type="png")
                img = Image.open(io.BytesIO(screenshot))
                prompt = PAGE_PROMPT_TEMPLATE.format(
                    context=f"This is screenshot {i+1} of {total} from a website. "
                )
                md = call_mimo_vision(img, api_key, prompt)
                page_markdowns.append(md)
                print(f"  Screenshot {i+1}/{total}: {len(md):,} chars", flush=True)

            await browser.close()
            return stitch_pages(page_markdowns)

    return asyncio.run(_run())


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PDF/Image to Markdown — https://pdf2md-api.streamlit.app/")
    parser.add_argument("--file", "-f", help="PDF/PNG/JPG/JPEG/WEBP file path")
    parser.add_argument("--url", "-u", help="URL to render and convert (requires Browserless)")
    parser.add_argument("--api-key", "-k", help="OpenCode API key (sk-...)")
    parser.add_argument("--mode", "-m", choices=["mimo", "local"], default="mimo", help="Parse mode")
    parser.add_argument("--dpi", "-d", type=int, default=200, help="PDF render DPI (100-300)")
    parser.add_argument("--browserless-token", "-b", help="Browserless API token (for URL mode)")
    parser.add_argument("--scroll-pages", "-s", type=int, default=10, help="Scroll count for URL mode")
    parser.add_argument("--output", "-o", help="Output .md file path (default: auto)")
    args = parser.parse_args()

    if not args.file and not args.url:
        parser.error("Provide --file or --url")
    if args.file and args.url:
        parser.error("Provide --file OR --url, not both")
    if args.mode == "mimo" and not args.api_key:
        parser.error("MiMo mode requires --api-key (sk-...)")
    if args.url and not args.browserless_token:
        parser.error("URL mode requires --browserless-token")

    t0 = time.time()

    if args.url:
        print(f"🌐 URL Mode: {args.url}")
        final_md = parse_url_with_browserless(
            args.url, args.api_key, args.browserless_token,
            dpi=args.dpi, scroll_pages=args.scroll_pages,
        )
    elif args.file:
        file_ext = args.file.rsplit(".", 1)[-1].lower()
        if file_ext not in SUPPORTED_EXTENSIONS:
            print(f"❌ Unsupported: .{file_ext}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
            sys.exit(1)

        with open(args.file, "rb") as f:
            file_bytes = f.read()

        if file_ext == "pdf" and args.mode == "mimo":
            print(f"📄 PDF → MiMo v2.5 (DPI={args.dpi})")
            page_images = pdf_to_images(file_bytes, dpi=args.dpi)
            total = len(page_images)
            print(f"  {total} page(s) extracted")
            all_md = []
            for i, img in enumerate(page_images):
                print(f"  Page {i+1}/{total}...", end=" ", flush=True)
                md = parse_page_with_mimo(img, args.api_key, i+1, total)
                print(f"✅ {len(md):,} chars")
                all_md.append(md)
            final_md = stitch_pages(all_md)

        elif file_ext in IMAGE_EXTENSIONS and args.mode == "mimo":
            print(f"🖼️  Image → MiMo v2.5")
            img = Image.open(io.BytesIO(file_bytes))
            mime = f"image/{file_ext if file_ext != 'jpg' else 'jpeg'}"
            final_md = parse_standalone_image_with_mimo(img, args.api_key, mime_type=mime)

        elif file_ext == "pdf" and args.mode == "local":
            print(f"📄 PDF → Local text extraction")
            final_md = extract_text_local(file_bytes)

        else:
            print(f"❌ Unsupported: {file_ext} + {args.mode}")
            sys.exit(1)

    elapsed = time.time() - t0

    # Output
    output_path = args.output
    if not output_path and args.file:
        output_path = args.file.rsplit(".", 1)[0] + ".md"
    elif not output_path:
        output_path = "output.md"

    with open(output_path, "w") as f:
        f.write(final_md)

    print(f"\n✅ Done in {elapsed:.1f}s — {len(final_md):,} chars → {output_path}")


if __name__ == "__main__":
    main()
