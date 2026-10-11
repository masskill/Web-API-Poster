"""User documents from docs/ (instruction, changelog) rendered for the web UI.

A deliberately small Markdown subset, enough for these files: headings, lists, code blocks,
`code`, **bold**, links and paragraphs. The text is escaped first, so HTML in a file never runs.
"""
import re

from markupsafe import Markup, escape

from app import config
from app.config import slugify

DOCS = {"guide": "INSTRUCTION", "updates": "CHANGELOG"}
SAFE_LINK = re.compile(r"^(https?://|/|#|[\w./-]+$)")


def load_doc(name: str, lang: str) -> str:
    base = DOCS[name]
    path = config.DOCS_DIR / (f"{base}.ru.md" if lang == "ru" else f"{base}.md")
    if not path.exists():
        path = config.DOCS_DIR / f"{base}.md"
    return path.read_text(encoding="utf-8")


def _inline(text: str) -> str:
    """Escaped text -> inline HTML: `code`, **bold**, [label](url)."""
    parts = str(escape(text)).split("`")
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{part}</code>")
            continue
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)

        def link(m):
            label, url = m.group(1), m.group(2)
            if not SAFE_LINK.match(url):
                return label
            external = ' target="_blank" rel="noopener"' if url.startswith("http") else ""
            return f'<a href="{url}"{external}>{label}</a>'
        out.append(re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, part))
    return "".join(out)


def render_markdown(text: str) -> tuple[Markup, list[tuple[str, str]]]:
    """Returns (html, contents) where contents lists (anchor, title) of the ## headings."""
    html, contents = [], []
    lines = text.splitlines()
    i, para, list_tag = 0, [], None

    def flush_para():
        if para:
            html.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            flush_para()
            close_list()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            html.append(f'<pre class="prompt">{escape(chr(10).join(block))}</pre>')
        elif m := re.match(r"^(#{1,3})\s+(.*)", line):
            flush_para()
            close_list()
            level, title = len(m.group(1)), m.group(2).strip()
            anchor = slugify(title)
            if level == 2:
                contents.append((anchor, title))
            html.append(f'<h{level} id="{anchor}">{_inline(title)}</h{level}>')
        elif m := re.match(r"^(?:[-*]|(\d+)\.)\s+(.*)", line):
            flush_para()
            tag = "ol" if m.group(1) else "ul"
            if list_tag != tag:
                close_list()
                # a numbered list split by a code block continues from its number
                html.append(f'<ol start="{m.group(1)}">' if tag == "ol" else "<ul>")
                list_tag = tag
            html.append(f"<li>{_inline(m.group(2))}</li>")
        elif not line.strip():
            flush_para()
            close_list()
        else:
            close_list()
            para.append(line.strip())
        i += 1
    flush_para()
    close_list()
    return Markup("\n".join(html)), contents
