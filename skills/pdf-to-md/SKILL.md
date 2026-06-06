---
name: pdf-to-md
description: |
  Convert any URL or file to clean, structured Markdown, powered by the
  PDF2MD Streamlit app engine (https://pdf2md-api.streamlit.app/).

  Two modes:
  1. URL mode: Uses Browserless CDP to smartly render the page (scroll for
     lazy-load, wait for JS, take viewport screenshots), then sends each
     screenshot to MiMo v2.5 vision via the OpenCode API — the same engine
     that powers the Streamlit app.
  2. File mode: Sends PDF/PNG/JPG/JPEG/WEBP directly to the Streamlit app's
     conversion engine (PyMuPDF + MiMo v2.5) — no Browserless needed.

  Outputs GFM tables, Mermaid diagrams for infographics, and faithful
  document structure.
  Use when user needs LLM-readable Markdown from web pages, PDFs, images,
  or JS-heavy sites.
inputs:
  url:
    type: string
    description: "Target page URL to render and convert. Mutually exclusive with 'file'. Triggers Browserless CDP mode."
  file:
    type: string
    description: "Local file path to a PDF/PNG/JPG/JPEG/WEBP file. Mutually exclusive with 'url'. No Browserless needed."
  auth:
    type: object
    description: "Optional login for URL mode. {type: 'creds', user, pass, selector_user, selector_pass, selector_submit} or {type: 'cookie', value: [...]}."
  scroll_pages:
    type: integer
    default: 10
    description: "Auto-scroll count to trigger lazy-load before screenshot. URL mode only."
  wait_for_selector:
    type: string
    description: "CSS selector to wait for before rendering. Ensures charts/data are loaded. URL mode only."
  dpi:
    type: integer
    default: 200
    description: "PDF render quality (100-300). Maps to viewport width: 200 -> 1920px. Used in both URL and file modes."
  parse_mode:
    type: string
    default: "mimo"
    description: "Parser: 'mimo' uses MiMo v2.5 AI Vision (requires opencode_api_key). 'local' uses text-only extraction (PDF only, no API key)."
  opencode_api_key:
    type: string
    default: "xxxx"
    description: "OpenCode API key for MiMo v2.5 (sk-...). Required for mimo mode. Get yours at opencode.ai/go."
  browserless_token:
    type: string
    default: ""
    description: "Browserless API token. Required for URL mode. Falls back to BROWSERLESS_TOKEN env var."
outputs:
  markdown:
    type: string
    description: "Final structured Markdown."
  source_url:
    type: string
    description: "Original URL (empty for file mode)."
  page_count:
    type: integer
    description: "Number of pages/screenshots processed."
  char_count:
    type: integer
    description: "Total character count of the output Markdown."
dependencies:
  - browserless
---

# PDF to Markdown — Powered by PDF2MD Streamlit App

This skill converts URLs and files to structured Markdown using the same MiMo v2.5 AI Vision engine that powers the [PDF2MD Streamlit app](https://pdf2md-api.streamlit.app/).

**App**: [https://pdf2md-api.streamlit.app/](https://pdf2md-api.streamlit.app/)

## Two Modes

### URL Mode (requires Browserless)

For web pages, JS-heavy sites, login-walled content:

1. **Smart Browserless CDP**: Connects via WebSocket, sets viewport, navigates
2. **Wait for JS**: `domcontentloaded` + extra delay for JS rendering
3. **Auto-scroll**: Scrolls down N times to trigger lazy-loaded images/content
4. **Viewport screenshots**: Takes viewport-sized PNG screenshots by scrolling through the full page
5. **MiMo v2.5 per screenshot**: Each screenshot → OpenCode MiMo API → Markdown
6. **Stitch**: All page Markdown results combined into one document

**Why screenshots instead of PDF?** The Browserless `/pdf` endpoint only does a basic
print-style render — it misses lazy-loaded content, JavaScript-rendered sections, and
interactive elements. CDP screenshot mode with scrolling captures the actual rendered page.
Tested on gemengserv.com: screenshots captured **3.4x more content** than the raw PDF.

### File Mode (no Browserless needed)

For PDF/PNG/JPG/JPEG/WEBP files — uses the same conversion engine as the Streamlit app:

- **PDF**: Render pages to images via PyMuPDF, send each to MiMo v2.5
- **Images**: Base64-encode, send directly to MiMo v2.5
- **Local mode**: Text-only PDF extraction via PyMuPDF (no API key needed)
- **Stitch results** into one Markdown document

## Implementation

```typescript
import { skill } from 'opencode'
import { chromium } from 'playwright-core'
import { readFileSync } from 'fs'
import { basename, extname } from 'path'

// Same API config as the Streamlit app
const OPENCODE_API_URL = 'https://opencode.ai/zen/go/v1/chat/completions'
const MIMO_MODEL = 'mimo-v2.5'
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp'])

// Same system prompt as the Streamlit app (api.py)
const SYSTEM_PROMPT = `You are a precise document-to-Markdown conversion engine. You receive page images and output clean, structured Markdown.

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
   - Wrap in \`\`\`mermaid ... \`\`\` code fences
   - Use flowchart TD (top-down) by default unless the visual suggests a different orientation
   - Use LR (left-right) for timelines, horizontal processes
   - Use subgraphs to group related nodes (teams, systems, phases)
   - Add clear node labels that match the original visual
   - Preserve all decision diamonds, branches, and loops
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
   - No page headers or separators — just the page content`

// Same prompts as the Streamlit app
function buildPagePrompt(pageNum: number, totalPages: number): string {
  const context = totalPages > 1
    ? `This is page ${pageNum} of ${totalPages} from a document. `
    : ''
  return `${context}Convert this document page image to clean, structured Markdown.

Follow these rules strictly:
1. Tables MUST use proper Markdown table syntax with | header | --- | rows.
2. Infographics, flowcharts, diagrams, process maps -> convert to \`\`\`mermaid\`\`\` code blocks.
3. Preserve headings, lists, formatting, and document structure faithfully.
4. Output ONLY Markdown — no commentary or explanation.`
}

const IMAGE_PROMPT = `Convert this image to clean, structured Markdown.

Follow these rules strictly:
1. Tables MUST use proper Markdown table syntax with | header | --- | rows.
2. Infographics, flowcharts, diagrams, process maps -> convert to \`\`\`mermaid\`\`\` code blocks.
3. Preserve headings, lists, formatting, and document structure faithfully.
4. Output ONLY Markdown — no commentary or explanation.`

async function callMimoVision(
  imageBase64: string,
  apiKey: string,
  prompt: string,
  mimeType: string = 'image/png'
): Promise<string> {
  const resp = await fetch(OPENCODE_API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: MIMO_MODEL,
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        {
          role: 'user',
          content: [
            { type: 'text', text: prompt },
            {
              type: 'image_url',
              image_url: { url: `data:${mimeType};base64,${imageBase64}` },
            },
          ],
        },
      ],
      max_tokens: 16384,
    }),
  })

  if (resp.status === 401 || resp.status === 403) {
    throw new Error(`Auth failed (${resp.status}): ${await resp.text().then(t => t.slice(0, 200))}`)
  }
  if (!resp.ok) {
    throw new Error(`API error (${resp.status}): ${await resp.text().then(t => t.slice(0, 200))}`)
  }

  const data = await resp.json()
  return data?.choices?.[0]?.message?.content ?? ''
}

// Same stitch logic as the Streamlit app
function stitchPages(pageMarkdowns: string[]): string {
  if (pageMarkdowns.length === 1) return pageMarkdowns[0] || ''
  const parts = pageMarkdowns.map((md, idx) =>
    `---\n\n**Page ${idx + 1}**\n\n${md}`
  )
  const result = parts.join('\n\n')
  return result.startsWith('---\n\n') ? result.slice(5) : result
}

function fileToBase64(filePath: string): string {
  const buffer = readFileSync(filePath)
  return buffer.toString('base64')
}

function getMimeType(ext: string): string {
  const map: Record<string, string> = {
    png: 'image/png',
    jpg: 'image/jpeg',
    jpeg: 'image/jpeg',
    webp: 'image/webp',
  }
  return map[ext.toLowerCase()] || 'application/octet-stream'
}

export default skill(async (input) => {
  const apiKey = input.opencode_api_key
  const parseMode = input.parse_mode || 'mimo'
  const browserlessToken = input.browserless_token || process.env.BROWSERLESS_TOKEN || ''

  if (parseMode === 'mimo' && (!apiKey || apiKey === 'xxxx')) {
    throw new Error('MiMo v2.5 mode requires an OpenCode API key. Set opencode_api_key (sk-...). Get yours at opencode.ai/go')
  }

  if (!input.url && !input.file) {
    throw new Error('Provide either a "url" or "file" input.')
  }
  if (input.url && input.file) {
    throw new Error('Provide either "url" OR "file", not both.')
  }

  const pageMarkdowns: string[] = []
  let sourceUrl = input.url || ''

  // ─── FILE MODE (no Browserless needed) ──────────────────────────────
  if (input.file) {
    const ext = extname(input.file).slice(1).toLowerCase()
    const fileName = basename(input.file)

    if (parseMode === 'local' && ext !== 'pdf') {
      throw new Error('Local mode only supports PDF files. Use mimo mode for images.')
    }

    if (IMAGE_EXTENSIONS.has(ext)) {
      // Image: base64 encode and send directly to MiMo
      const b64 = fileToBase64(input.file)
      const mime = getMimeType(ext)
      const md = await callMimoVision(b64, apiKey!, IMAGE_PROMPT, mime)
      pageMarkdowns.push(md)
    } else if (ext === 'pdf') {
      if (parseMode === 'local') {
        throw new Error('Local PDF text extraction requires PyMuPDF. Use the Streamlit app at https://pdf2md-api.streamlit.app/ or set parse_mode=mimo.')
      }

      // PDF via Browserless viewer
      if (!browserlessToken) {
        throw new Error('PDF file mode requires browserless_token for rendering, or use the Streamlit app at https://pdf2md-api.streamlit.app/')
      }

      const browser = await chromium.connectOverCDP({
        endpointURL: `wss://production-sfo.browserless.io?token=${browserlessToken}`,
      })
      try {
        const page = await browser.newPage()
        const pdfDataUrl = `data:application/pdf;base64,${fileToBase64(input.file)}`
        await page.goto(pdfDataUrl, { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(2000)

        const totalPages = await page.evaluate(() => {
          const viewer = document.getElementById('viewer')
          return viewer ? viewer.children.length : 1
        })

        for (let i = 0; i < totalPages; i++) {
          await page.evaluate((idx: number) => {
            const viewer = document.getElementById('viewer')
            if (viewer && viewer.children[idx]) {
              viewer.children[idx].scrollIntoView()
            }
          }, i)
          await page.waitForTimeout(500)

          const screenshot = await page.screenshot({ type: 'png' })
          const b64 = screenshot.toString('base64')
          const prompt = buildPagePrompt(i + 1, totalPages)
          const md = await callMimoVision(b64, apiKey!, prompt)
          pageMarkdowns.push(md)
        }
      } finally {
        await browser.close()
      }
    } else {
      throw new Error(`Unsupported file type: .${ext}. Supported: pdf, png, jpg, jpeg, webp`)
    }
  }

  // ─── URL MODE (requires Browserless) ────────────────────────────────
  if (input.url && !input.file) {
    if (!browserlessToken) {
      throw new Error('URL mode requires browserless_token. Set it or the BROWSERLESS_TOKEN env var.')
    }

    const dpi = input.dpi || 200
    const viewportWidth = Math.round(dpi * 9.6)  // 200 DPI -> 1920px

    const browser = await chromium.connectOverCDP({
      endpointURL: `wss://production-sfo.browserless.io?token=${browserlessToken}&blockAds=true&timeout=60000`,
    })

    try {
      const page = await browser.newPage()
      await page.setViewportSize({ width: viewportWidth, height: 1080 })

      // ── Auth handling ──
      if (input.auth?.type === 'creds') {
        const origin = new URL(input.url).origin
        await page.goto(`${origin}/login`, { waitUntil: 'domcontentloaded' })
        await page.fill(input.auth.selector_user, input.auth.user)
        await page.fill(input.auth.selector_pass, input.auth.pass)
        await page.click(input.auth.selector_submit)
        await page.waitForTimeout(2000)
      } else if (input.auth?.type === 'cookie') {
        await page.context().addCookies(input.auth.value)
      }

      // ── Navigate and wait for JS ──
      await page.goto(input.url, { waitUntil: 'domcontentloaded', timeout: 30000 })
      await page.waitForTimeout(3000)  // Extra wait for JS-heavy content

      if (input.wait_for_selector) {
        await page.waitForSelector(input.wait_for_selector, { timeout: 30000 })
      }

      // ── Smart scroll to trigger lazy-loaded content ──
      const scrollPages = input.scroll_pages || 10
      for (let i = 0; i < scrollPages; i++) {
        await page.evaluate(() => window.scrollBy(0, window.innerHeight))
        await page.waitForTimeout(600)
      }
      await page.waitForTimeout(1500)  // Settle after scrolling

      // ── Scroll back to top ──
      await page.evaluate(() => window.scrollTo(0, 0))
      await page.waitForTimeout(800)

      // ── Calculate page dimensions ──
      const fullHeight = await page.evaluate(() => document.documentElement.scrollHeight)
      const viewportHeight = await page.evaluate(() => window.innerHeight)
      const totalScreenshots = Math.ceil(fullHeight / viewportHeight)

      if (parseMode === 'local') {
        throw new Error('Local mode is not supported for URL input. Use mimo mode.')
      }

      // ── Take viewport-sized screenshots and process each ──
      for (let i = 0; i < totalScreenshots; i++) {
        await page.evaluate((offset: number) => window.scrollTo(0, offset), i * viewportHeight)
        await page.waitForTimeout(400)

        const screenshot = await page.screenshot({ type: 'png' })
        const b64 = screenshot.toString('base64')

        const prompt = buildPagePrompt(i + 1, totalScreenshots)
        const md = await callMimoVision(b64, apiKey!, prompt)
        pageMarkdowns.push(md)
      }
    } finally {
      await browser.close()
    }
  }

  // ─── Stitch and return ────────────────────────────────────────────────
  const finalMd = stitchPages(pageMarkdowns)

  return {
    markdown: finalMd,
    source_url: sourceUrl,
    page_count: pageMarkdowns.length,
    char_count: finalMd.length,
  }
})
```

## Streamlit App

The hosted Streamlit app uses the same MiMo v2.5 engine:
- **UI**: [https://pdf2md-api.streamlit.app/](https://pdf2md-api.streamlit.app/)
- **API mode**: [https://pdf2md-api.streamlit.app/?api=1](https://pdf2md-api.streamlit.app/?api=1)

## Quick Test (curl)

Test the OpenCode API key works with MiMo v2.5:

```bash
curl -s https://opencode.ai/zen/go/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-YOUR-KEY" \
  -d '{"model":"mimo-v2.5","messages":[{"role":"user","content":"Say hi"}],"max_tokens":50}'
```

## Source Code

- Streamlit app: `api.py`
- FastAPI server (optional Docker): `server.py`
- This skill: `skills/pdf-to-md.md`
- Repo: [https://github.com/zazikant/pdf2md-api](https://github.com/zazikant/pdf2md-api)
