"""Universal Ctags indexing and XHTML cross-reference helpers."""

import json
import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from html import escape
from typing import Dict, List, Optional, Set, Tuple, TypedDict, cast
from urllib.parse import quote

from project_to_epub.models import FileEntry

logger = logging.getLogger(__name__)


def xml_escape(value: object) -> str:
    """Escape text or attribute values for generated EPUB XML documents."""
    return escape(str(value), quote=True)


class CtagsRecord(TypedDict, total=False):
    """Fields read from a Universal Ctags JSON tag record."""

    _type: str
    name: str
    path: str
    pattern: str
    line: int
    kind: str


def find_file(
    definitions: List[Tuple[FileEntry, CtagsRecord]], suffixes: Set[str]
) -> Optional[Tuple[FileEntry, CtagsRecord]]:
    for definition in definitions:
        file_entry, _ = definition
        if file_entry.absolute_path.suffix.lower() in suffixes:
            return definition
    return None


@dataclass(frozen=True)
class Tag:
    name: str
    index: int
    target: FileEntry
    record: CtagsRecord

    @property
    def file_name(self) -> str:
        """Return the XHTML page name for a scanned file index."""
        return f"file_{self.index}.xhtml"

    @property
    def fragment(self) -> str:
        """Return this symbol's XHTML fragment identifier."""
        return f"tag-{quote(self.name, safe='')}"


@dataclass(frozen=True)
class CTag(Tag):
    """A parsed symbol and its EPUB destinations."""

    implementation: Optional[Tag]

    @staticmethod
    def from_definitions(
        name: str,
        files: List[FileEntry],
        definitions: List[Tuple[FileEntry, CtagsRecord]],
    ) -> "CTag":
        header = find_file(definitions, {".h", ".hh", ".hpp", ".hxx"})
        implementation_file = find_file(definitions, {".c", ".cc", ".cpp", ".cxx"})
        target, record = header or implementation_file or definitions[0]
        implementation = None
        if implementation_file is not None:
            entry, record = implementation_file
            implementation = Tag(
                name,
                files.index(entry),
                entry,
                record,
            )
        index = files.index(target)
        return CTag(name, index, target, record, implementation)

    def url_from(self, entry: FileEntry) -> str:
        """Format this tag's link from the current EPUB source page."""
        target_tag = self
        if self.implementation is not None and entry == self.target:
            target_tag = self.implementation
        frag_hash = f"#{xml_escape(self.fragment)}"
        if entry == target_tag.target:
            return frag_hash
        return xml_escape(target_tag.file_name) + frag_hash

    def anchor_for(self, entry: FileEntry) -> Optional[Tuple[int, str]]:
        """Return the definition line and fragment for this page, if present."""
        if self.target == entry:
            return self.record["line"], self.fragment
        if self.implementation is not None and self.implementation.target == entry:
            return self.implementation.record["line"], self.fragment
        return None


TagIndex = Dict[str, CTag]


def build_tag_index(files: List[FileEntry], progress=None) -> TagIndex:
    """Return symbols mapped to their EPUB file and fragment, using Universal Ctags."""
    executable = shutil.which("ctags")
    if not files:
        return {}
    if not executable:
        logger.warning("Universal Ctags not found; EPUB cross references are disabled")
        return {}

    try:
        TERMINATOR = "__PROJECT_TO_EPUB_FILE_DONE__"
        ctags_processor = subprocess.Popen(
            [
                executable,
                "--output-format=json",
                "--fields=+n",
                "--sort=no",
                "--filter=yes",
                f"--filter-terminator={TERMINATOR}",
                "-f",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        def write_stdin():
            try:
                ctags_processor.stdin.writelines(
                    f"{file.absolute_path}\n" for file in files
                )
                ctags_processor.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=write_stdin)
        writer.start()
        output_lines = []
        for output_line in ctags_processor.stdout:
            if output_line.rstrip("\r\n") == TERMINATOR:
                if progress is not None:
                    progress.update(1)
                continue
            output_lines.append(output_line)
        return_code = ctags_processor.wait()
        writer.join()
        if return_code:
            raise subprocess.CalledProcessError(return_code, ctags_processor.args)
    except (OSError, subprocess.CalledProcessError) as error:
        logger.warning(
            "Universal Ctags failed; EPUB cross references are disabled: %s", error
        )
        return {}
    return parse_ctags_output(files, output_lines)


def parse_ctags_output(files: List[FileEntry], output_lines: List[str]) -> TagIndex:
    path_to_file = {str(file.absolute_path): file for file in files}
    candidates: Dict[str, List[Tuple[FileEntry, CtagsRecord]]] = {}
    for line in output_lines:
        try:
            record = cast(CtagsRecord, json.loads(line))
        except json.JSONDecodeError as error:
            logger.warning("Invalid Ctags JSON output: %s", error)
            continue
        if not isinstance(record, dict):
            logger.warning("Skipping non-object Ctags JSON: %r", record)
            continue
        name = record.get("name")
        if record.get("_type") != "tag" or not name:
            logger.debug("Skipping non-tag: %r", record)
            continue
        source = path_to_file.get(record.get("path", ""))
        line_number = record.get("line")
        if (
            source is None
            or source.language == "markdown"
            or not isinstance(line_number, int)
        ):
            continue
        candidates.setdefault(name, []).append((source, record))

    return {
        name: CTag.from_definitions(name, files, definitions)
        for name, definitions in candidates.items()
    }


def link_tag_references(html: str, entry: FileEntry, tags: TagIndex) -> str:
    """Link visible identifier text and add definition anchors to a source fragment."""
    if not tags:
        return html

    symbols = sorted(tags, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w])(?:" + "|".join(re.escape(x) for x in symbols) + r")(?![\w])"
    )
    chunks = re.split(r"(<[^>]+>)", html)
    for index in range(0, len(chunks), 2):
        text = chunks[index]
        if not text:
            continue

        def replace(match):
            name = match.group(0)
            href = tags[name].url_from(entry)
            return f'<a href="{href}">{name}</a>'

        chunks[index] = pattern.sub(replace, text)

    html_content = "".join(chunks)
    anchors_by_line: Dict[int, Set[str]] = {}
    for tag in tags.values():
        anchor = tag.anchor_for(entry)
        if anchor is not None:
            line_number, fragment = anchor
            anchors_by_line.setdefault(line_number, set()).add(fragment)
    return insert_line_anchors(html_content, anchors_by_line)


def insert_line_anchors(html: str, anchors_by_line: Dict[int, Set[str]]) -> str:
    """Insert XHTML anchors at source line starts in a highlighted code block."""
    if not anchors_by_line:
        return html

    def add_anchors(match):
        lines = match.group("code").splitlines(keepends=True)
        for line_number, fragments in anchors_by_line.items():
            if 1 <= line_number <= len(lines):
                anchors = "".join(
                    f'<span id="{xml_escape(fragment)}"></span>'
                    for fragment in sorted(fragments)
                )
                lines[line_number - 1] = anchors + lines[line_number - 1]
        return match.group("open") + "".join(lines) + match.group("close")

    code_pre = re.compile(
        r'(?P<open><td class="code">\s*<div>\s*<pre[^>]*>)'
        r"(?P<code>.*?)"
        r"(?P<close></pre>)",
        re.DOTALL,
    )
    highlighted_match = code_pre.search(html)
    if highlighted_match is not None:
        return code_pre.sub(add_anchors, html, count=1)

    # Fallback highlighting emits a plain <pre> without Pygments' code table.
    plain_pre = re.compile(
        r"(?P<open><pre[^>]*>)(?P<code>.*?)(?P<close></pre>)", re.DOTALL
    )
    return plain_pre.sub(add_anchors, html, count=1)
