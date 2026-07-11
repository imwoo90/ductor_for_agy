"""Convert standard Markdown to Telegram-compatible HTML.

Telegram HTML supports: <b>, <i>, <s>, <u>, <code>, <pre>, <a href>,
<blockquote>, <tg-spoiler>.  This module converts Claude's Markdown output
into that subset so messages render properly in Telegram clients.
"""

from __future__ import annotations

import html
import re

from ductor_bot.messenger.telegram.buttons import strip_button_syntax

TELEGRAM_MSG_LIMIT = 4096
"""Maximum characters per Telegram message."""

_SENTINEL = "\x00"


def _placeholder(kind: str, idx: int) -> str:
    return f"{_SENTINEL}{kind}{idx}{_SENTINEL}"


def _parse_table_row(line: str) -> list[str]:
    """Split a Markdown table row into stripped cell values."""
    stripped = line.strip().removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(line: str) -> bool:
    """Check if a line is a Markdown table separator (e.g. |---|---|)."""
    return bool(re.match(r"^\s*\|?[\s:]*-{2,}[\s:]*(\|[\s:]*-{2,}[\s:]*)*\|?\s*$", line))


def _format_table(lines: list[str]) -> str:
    """Convert parsed Markdown table lines into a column-aligned monospace block."""
    rows: list[list[str]] = []
    for line in lines:
        if _is_separator_row(line):
            continue
        rows.append(_parse_table_row(line))

    if not rows:
        return "\n".join(lines)

    n_cols = max(len(r) for r in rows)
    for row in rows:
        row.extend("" for _ in range(n_cols - len(row)))

    widths = [max(len(row[c]) for row in rows) for c in range(n_cols)]

    out: list[str] = []
    for i, row in enumerate(rows):
        cells = [cell.ljust(widths[c]) for c, cell in enumerate(row)]
        out.append("  ".join(cells))
        if i == 0 and len(rows) > 1:
            out.append("  ".join("\u2500" * w for w in widths))
    return "\n".join(out)


def _convert_blockquotes(text: str) -> str:
    """Wrap consecutive ``> `` or ``>! `` lines in ``<blockquote>`` or ``<blockquote expandable>`` tags."""
    lines = text.split("\n")
    result: list[str] = []
    quote_buf: list[str] = []
    escaped_gt = "&gt; "
    escaped_gt_exp = "&gt;! "
    is_expandable = False

    for line in lines:
        if line.startswith(escaped_gt) or line.startswith(escaped_gt_exp):
            if line.startswith(escaped_gt_exp):
                is_expandable = True
                quote_buf.append(line[len(escaped_gt_exp) :])
            else:
                quote_buf.append(line[len(escaped_gt) :])
        else:
            if quote_buf:
                tag = "<blockquote expandable>" if is_expandable else "<blockquote>"
                result.append(tag + "\n".join(quote_buf) + "</blockquote>")
                quote_buf = []
                is_expandable = False
            result.append(line)
    if quote_buf:
        tag = "<blockquote expandable>" if is_expandable else "<blockquote>"
        result.append(tag + "\n".join(quote_buf) + "</blockquote>")
    return "\n".join(result)


def _extract_tables(src: str) -> tuple[str, list[str]]:
    """Find consecutive Markdown table rows and replace with placeholders."""
    table_blocks: list[str] = []
    out_lines: list[str] = []
    table_buf: list[str] = []

    def _flush() -> None:
        if len(table_buf) >= 2:
            idx = len(table_blocks)
            table_blocks.append(_format_table(table_buf))
            out_lines.append(_placeholder("TB", idx))
        else:
            out_lines.extend(table_buf)
        table_buf.clear()

    for line in src.split("\n"):
        if "|" in line and re.search(r"\|.*\|", line.strip()):
            table_buf.append(line)
        else:
            if table_buf:
                _flush()
            out_lines.append(line)
    if table_buf:
        _flush()
    return "\n".join(out_lines), table_blocks


def markdown_to_telegram_html(text: str) -> str:
    """Convert Markdown text to Telegram-compatible HTML.

    Handles: code blocks, inline code, bold, italic, strikethrough,
    links, headings, blockquotes, horizontal rules, tables, and list bullets.
    """
    text = strip_button_syntax(text)

    code_blocks: list[tuple[str, str]] = []
    inline_codes: list[str] = []

    def _save_code_block(m: re.Match[str]) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(code_blocks)
        code_blocks.append((lang, code))
        return _placeholder("CB", idx)

    text = re.sub(r"```(\w*)\n(.*?)```", _save_code_block, text, flags=re.DOTALL)
    text, table_blocks = _extract_tables(text)

    def _save_inline_code(m: re.Match[str]) -> str:
        idx = len(inline_codes)
        inline_codes.append(m.group(1))
        return _placeholder("IC", idx)

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)
    text = html.escape(text)

    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = _convert_blockquotes(text)
    text = re.sub(r"^[-*]{3,}$", "\u2014\u2014\u2014", text, flags=re.MULTILINE)
    text = re.sub(r"^- ", "\u2022 ", text, flags=re.MULTILINE)

    for i, code in enumerate(inline_codes):
        text = text.replace(_placeholder("IC", i), f"<code>{html.escape(code)}</code>")

    for i, table_text in enumerate(table_blocks):
        text = text.replace(_placeholder("TB", i), f"<pre>{html.escape(table_text)}</pre>")

    for i, (lang, code) in enumerate(code_blocks):
        escaped = html.escape(code)
        if lang:
            block = f'<pre><code class="language-{html.escape(lang)}">{escaped}</code></pre>'
        else:
            block = f"<pre>{escaped}</pre>"
        text = text.replace(_placeholder("CB", i), block)

    return text


def _accumulate_parts(
    parts: list[str], separator: str, max_len: int
) -> tuple[list[str], list[str]]:
    """Accumulate text parts into chunks, splitting at *separator* boundaries."""
    chunks: list[str] = []
    current = ""
    oversized: list[str] = []

    for part in parts:
        candidate = f"{current}{separator}{part}" if current else part
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(part) <= max_len:
            current = part
        else:
            oversized.append(part)

    if current:
        chunks.append(current)
    return chunks, oversized


def split_html_message(text: str, max_len: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Split an HTML message into chunks that fit Telegram's limit,
    ensuring HTML tags are properly closed in each chunk and re-opened in the next.
    """
    if len(text) <= max_len:
        return [text]

    # Tokenize HTML: split by tags, double newlines, and single newlines
    tokens = re.split(r'(</?[a-zA-Z][^>]*>|\n\n|\n)', text)
    
    chunks = []
    current_chunk_parts = []
    current_len = 0
    open_tags = []  # list of full start tags, e.g., ['<blockquote expandable>', '<b>']

    def get_open_tag_name(tag: str) -> str:
        return tag.strip('<>').split()[0]

    def get_close_tag_name(tag: str) -> str:
        return tag.strip('<>/').split()[0]

    for token in tokens:
        if not token:
            continue
        
        is_tag = token.startswith('<') and token.endswith('>')
        closing_tags_str = "".join(f"</{get_open_tag_name(t)}>" for t in reversed(open_tags))
        
        # If this is text and it is longer than the remaining space in the chunk
        if not is_tag and len(token) > (max_len - current_len - len(closing_tags_str)):
            available = max_len - current_len - len(closing_tags_str)
            if available > 0:
                current_chunk_parts.append(token[:available])
                token = token[available:]
            
            # Close current chunk and start a new one
            current_chunk_parts.append(closing_tags_str)
            chunks.append("".join(current_chunk_parts))
            
            # Reset chunk and re-open tags
            current_chunk_parts = list(open_tags)
            current_len = sum(len(t) for t in open_tags)
            
            # Recursively split the remaining text if it still exceeds max_len
            while len(token) > (max_len - current_len - len(closing_tags_str)):
                available = max_len - current_len - len(closing_tags_str)
                current_chunk_parts.append(token[:available])
                token = token[available:]
                current_chunk_parts.append(closing_tags_str)
                chunks.append("".join(current_chunk_parts))
                
                # Reset chunk
                current_chunk_parts = list(open_tags)
                current_len = sum(len(t) for t in open_tags)

        # If adding this token (tag or text) exceeds max_len, split before it
        elif current_chunk_parts and (current_len + len(token) + len(closing_tags_str) > max_len):
            current_chunk_parts.append(closing_tags_str)
            chunks.append("".join(current_chunk_parts))
            
            # Reset chunk and re-open tags
            current_chunk_parts = list(open_tags)
            current_len = sum(len(t) for t in open_tags)

        current_chunk_parts.append(token)
        current_len += len(token)
        
        if is_tag:
            if token.startswith('</'):
                # End tag
                name = get_close_tag_name(token)
                # Find matching start tag from the end of open_tags
                for idx in range(len(open_tags) - 1, -1, -1):
                    if get_open_tag_name(open_tags[idx]) == name:
                        open_tags.pop(idx)
                        break
            elif not token.endswith('/>'):
                # Start tag
                open_tags.append(token)

    if current_chunk_parts:
        closing_tags_str = "".join(f"</{get_open_tag_name(t)}>" for t in reversed(open_tags))
        current_chunk_parts.append(closing_tags_str)
        chunks.append("".join(current_chunk_parts))

    return [c for c in chunks if c.strip()] or [""]
