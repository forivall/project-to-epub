"""Source rendering and EPUB page styling."""

import logging
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from typing import Generator, List, Optional, Tuple, TypeVar

try:
    from importlib.resources import files
except ImportError:  # pragma: no cover - Python 3.8 fallback
    from importlib.resources import read_text

    files = None

import markdown
import pygments
import pygments.util
from pygments import lexers
from pygments.formatters import HtmlFormatter
from pygments.style import Style
from pygments.token import Comment, Generic, Keyword, Name, Operator

from project_to_epub.models import FileEntry

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class EInkMonochromeStyle(Style):
    """Pygments style that preserves syntax cues without color."""

    background_color = "#FFFFFF"
    default_style = ""
    styles = {
        Comment: "italic",
        Generic.Deleted: "underline",
        Generic.Emph: "italic",
        Generic.Error: "bold underline",
        Generic.Heading: "bold",
        Generic.Inserted: "bold",
        Generic.Strong: "bold",
        Generic.Subheading: "bold",
        Keyword: "bold",
        Name.Class: "bold",
        Name.Function: "bold",
        Name.Namespace: "bold",
        Operator.Word: "bold",
    }


def create_highlight_formatter(theme_name: str) -> HtmlFormatter:
    """
    Create a Pygments HTML formatter with the specified theme.

    Args:
        theme_name: Name of the syntax highlighting theme

    Returns:
        HtmlFormatter: Pygments HTML formatter
    """
    if theme_name == "default_eink":
        return HtmlFormatter(
            style=EInkMonochromeStyle,
            cssclass="highlight",
            linenos=False,
            full=False,
            noclasses=False,
            nobackground=True,
        )

    try:
        return HtmlFormatter(
            style=theme_name,
            cssclass="highlight",
            linenos=False,
            full=False,
            noclasses=False,
        )
    except pygments.util.ClassNotFound:
        logger.warning(f"Theme '{theme_name}' not found, falling back to 'default'")
        return HtmlFormatter(
            style="default",
            cssclass="highlight",
            linenos=False,
            full=False,
            noclasses=False,
        )


def get_css_for_epub(formatter: Optional[HtmlFormatter] = None) -> str:
    """
    Get base CSS for EPUB styling.

    Returns:
        str: CSS content
    """
    if files is not None:
        css = (files("project_to_epub") / "style.css").read_text(encoding="utf-8")
    else:
        css = read_text("project_to_epub", "style.css", encoding="utf-8")
    if formatter is not None:
        css += "\n" + formatter.get_style_defs(".highlight")
    return css


def highlight_code(content: str, lexer, formatter: HtmlFormatter) -> str:
    """
    Apply syntax highlighting to code content.

    Args:
        content: Source code content
        lexer: Pygments lexer for the language
        formatter: Pygments HTML formatter

    Returns:
        str: HTML content with syntax highlighting
    """
    try:
        highlighted = pygments.highlight(content, lexer, formatter)
        if highlighted.strip():
            return _code_pre_to_ordered_list(highlighted)
        logger.warning(f"Pygments returned empty highlighted content for {lexer.name}")
    except Exception as e:
        logger.error(f"Error highlighting code with {lexer.name}: {e}")
    fallback = f'<pre class="highlight">{escape(content)}</pre>'
    try:
        return _code_pre_to_ordered_list(fallback)
    except Exception:
        return fallback


def with_is_last(items: List[_T]) -> Generator[Tuple[_T, bool], None, None]:
    for item in items[:-1]:
        yield item, False
    yield items[-1], True


class _CodeLineListBuilder:
    """Visit highlighted XML nodes and copy their content into ordered lines."""

    def __init__(self) -> None:
        self.ordered_list = ET.Element("ol", {"class": "source-lines"})
        self.current_line = ET.SubElement(self.ordered_list, "li")
        self.source_stack: list[ET.Element] = []
        self.output_stack = [self.current_line]

    @property
    def current_leaf(self):
        return self.output_stack[-1]

    def transform(self, pre: ET.Element) -> ET.Element:
        """Visit all highlighted content inside the source ``pre`` element."""
        self.append(pre.text)
        for child in pre:
            self.visit(child)
            self.append(child.tail)
        return self.ordered_list

    def visit(self, source: ET.Element) -> None:
        """Copy a node and descendants, preserving inline syntax markup."""
        self.source_stack.append(source)
        clone = ET.SubElement(self.current_leaf, source.tag, source.attrib)
        self.output_stack.append(clone)
        self.append(source.text)
        for child in source:
            self.visit(child)
            self.append(child.tail)
        self.output_stack.pop()
        self.source_stack.pop()

    def append(self, value: Optional[str]) -> None:
        """Append text, opening a new list item for every source newline."""
        if value is None:
            return
        sub_lines = value.split("\n")
        for sub_line, is_last in with_is_last(sub_lines):
            if sub_line:
                self.append_to_current_leaf(sub_line)
            if not is_last:
                self.start_new_line()

    def append_to_current_leaf(self, sub_line):
        leaf = self.current_leaf
        if len(leaf):
            last_child = leaf[-1]
            last_child.tail = (last_child.tail or "") + sub_line
        else:
            leaf.text = (leaf.text or "") + sub_line

    def start_new_line(self):
        self.current_line = ET.SubElement(self.ordered_list, "li")
        self.output_stack = [self.current_line]
        for source in self.source_stack:
            clone = ET.SubElement(self.current_leaf, source.tag, source.attrib)
            self.output_stack.append(clone)


def replace_element(container: ET.Element, target: ET.Element, replacement: ET.Element):
    parent = next(node for node in container.iter() if target in node)
    index = next(i for i, child in enumerate(parent) if child is target)
    parent.remove(target)
    parent.insert(index, replacement)


def _code_pre_to_ordered_list(fragment: str) -> str:
    """Convert each highlighted ``pre`` into XHTML ordered-list lines."""
    wrapper = ET.fromstring(f"<fragment>{fragment}</fragment>")
    pre_elements = wrapper.findall(".//pre")
    if not pre_elements:
        return fragment

    for pre in pre_elements:
        ol = _CodeLineListBuilder().transform(pre)
        replace_element(wrapper, pre, ol)
    return "".join(
        ET.tostring(child, encoding="unicode", method="xml") for child in wrapper
    )


def render_markdown(content: str, formatter: Optional[HtmlFormatter] = None) -> str:
    """
    Render Markdown content to HTML.

    Args:
        content: Markdown content
        formatter: Pygments formatter to use for fenced code blocks

    Returns:
        str: HTML content
    """
    if formatter is None:
        formatter = create_highlight_formatter("default_eink")

    try:
        html = markdown.markdown(
            content,
            extensions=["tables", "fenced_code", "codehilite"],
            extension_configs={
                "codehilite": {
                    "linenums": formatter.linenos,
                    "noclasses": formatter.noclasses,
                    "pygments_style": formatter.style,
                }
            },
            output_format="xhtml",
        )
        if formatter.linenos:
            html = _code_pre_to_ordered_list(html)
        return f"<div class='markdown-content'>{html}</div>"
    except Exception as e:
        logger.error(f"Error rendering Markdown: {e}")
        return f'<pre class="markdown-error">{escape(content)}</pre>'


def render_source_content(
    file_entry: FileEntry,
    content: str,
    formatter: HtmlFormatter,
) -> str:
    """Render a source file as an HTML fragment."""
    if file_entry.language == "markdown":
        return render_markdown(content, formatter)

    try:
        lexer = lexers.get_lexer_for_filename(str(file_entry.absolute_path))
    except pygments.util.ClassNotFound:
        lexer = lexers.get_lexer_by_name("text")

    return highlight_code(content, lexer, formatter)


def render_file_xhtml(relative_path: Path, html_content: str) -> str:
    """Render a complete XHTML page for one project file."""
    escaped_relative_path = escape(relative_path.as_posix(), quote=True)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
    <title>{escaped_relative_path}</title>
    <link rel="stylesheet" type="text/css" href="style.css" />
</head>
<body>
    <h1>{escaped_relative_path}</h1>
    {html_content}
</body>
</html>"""
