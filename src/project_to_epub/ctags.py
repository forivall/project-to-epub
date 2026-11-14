"""Universal Ctags indexing and XHTML cross-reference helpers."""

import json
import logging
import re
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from html import escape
from itertools import groupby
from typing import (
    Callable,
    Collection,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    TypedDict,
    TypeVar,
    Unpack,
    cast,
)
from urllib.parse import quote

from project_to_epub.models import FileEntry

logger = logging.getLogger(__name__)


SOURCE_LINES_CLASSNAME = "source-lines"


def xml_escape(value: object) -> str:
    """Escape text or attribute values for generated EPUB XML documents."""
    return escape(str(value), quote=True)


T = TypeVar("T")
K = TypeVar("K")


def sort_and_group(
    values: list[T],
    key: Callable[[T], Tuple[K, Unpack[Tuple]]],
) -> Dict[K, OrderedDict[T, None]]:
    sorted_values = sorted(values, key=key)
    return {
        name: OrderedDict.fromkeys(group)
        for name, group in groupby(sorted_values, key=lambda tag: key(tag)[0])
    }


class CtagsRecord(TypedDict, total=False):
    """Fields read from a Universal Ctags JSON tag record."""

    _type: str
    name: str
    path: str
    pattern: str
    line: int
    kind: str


@dataclass(frozen=True)
class Tag:
    name: str
    file: FileEntry
    file_index: int
    record: CtagsRecord

    def __hash__(self) -> int:
        return hash((self.name, self.file_index, self.line_number))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Tag):
            return NotImplemented
        return (
            self.name,
            self.file_index,
            self.line_number,
        ) == (other.name, other.file_index, other.line_number)

    def __lt__(self, other: "Tag"):
        if self.file != other.file:
            raise ValueError("Same file comparison only")
        return self.line_number < other.line_number

    def is_current(self, file: FileEntry, line: int):
        return self.file == file and self.line_number == line

    @property
    def file_name(self) -> str:
        """XHTML page name for a scanned file index."""
        return f"file_{self.file_index}.xhtml"

    @property
    def fragment(self) -> str:
        """XHTML fragment identifier for symbol"""
        return f"tag-{quote(self.name, safe='')}"

    @property
    def line_number(self) -> int:
        return self.record["line"]

    def url_from(self, entry: FileEntry) -> str:
        """Format this tag's link from the current EPUB source page."""
        frag = xml_escape(self.fragment)
        if entry == self.file:
            return f"#{frag}"
        file_name = xml_escape(self.file_name)
        return f"{file_name}#{frag}"

    def anchor_for(self, entry: FileEntry) -> Optional[Tuple[int, str]]:
        """Return the definition line and fragment for this page, if present."""
        if self.file == entry:
            return self.record["line"], self.fragment
        return None


TagsByName = Dict[str, OrderedDict[Tag, None]]
TagsByFile = Dict[int, OrderedDict[Tag, None]]
TagsParsed = Tuple[TagsByName, TagsByFile]


class _TagReferenceLinker:
    """Visit highlighted XML and link names inside Pygments name tokens."""

    def __init__(self, file: FileEntry, file_index: int, tags: TagsParsed) -> None:
        self.file = file
        self.file_index = file_index
        self.tags, tags_by_file = tags
        self.file_tags = tags_by_file.get(file_index, None)
        symbols = sorted(self.tags, key=len, reverse=True)
        self.pattern = re.compile(
            r"(?<![\w])(?:"
            + "|".join(re.escape(name) for name in symbols)
            + r")(?![\w])"
        )

    def next_file_tag(self, name: str, or_own=False) -> Optional[Tag]:
        tags = self.tags[name]
        for tag in tags.keys():
            if or_own and tag.file_index == self.file_index:
                return tag
            if tag.file_index > self.file_index:
                return tag
        return next((tag for tag in tags if tag.file_index != self.file_index), None)

    def transform(self, fragment: ET.Element) -> ET.Element:
        """Visit the fragment tree and add links to matching name tokens."""
        self.visit(fragment)
        return fragment

    def visit(
        self,
        source: ET.Element,
        inside_line: Optional[int] = None,
        inside_name: bool = False,
        inside_link: bool = False,
    ) -> None:
        """Visit a node and its children, preserving all non-link markup."""
        classes = source.get("class", "").split()
        if SOURCE_LINES_CLASSNAME in classes:
            for index, child in enumerate(source):
                self.visit(child, index + 1)
            return
        is_name = inside_name or any(
            classname == "n" or classname.startswith("n") for classname in classes
        )
        is_link = inside_link or source.tag == "a"
        if is_name and not is_link:
            self.link_text(source, inside_line)
        for child in source:
            self.visit(child, inside_line, is_name, is_link)

    def get_target_tag(self, name: str, line: Optional[int]) -> Tag | None:
        if line is None:
            return self.next_file_tag(name, or_own=True)
        tags = self.tags[name]
        file_tags = sorted(
            (tag for tag in tags if tag.file == self.file),
            key=lambda tag: tag.line_number,
        )
        prev = None
        for tag in file_tags:
            if tag.line_number > line and prev is not None:
                return prev
            prev = tag
        return self.next_file_tag(name)

    def link_text(self, source: ET.Element, line: Optional[int]) -> None:
        """Replace matching portions of a name token's text with anchors."""
        if not source.text:
            return
        original = source.text
        maybe_matches = (
            (match, self.get_target_tag(match[0], line))
            for match in self.pattern.finditer(original)
        )
        matches = list((match, target) for match, target in maybe_matches if target)
        if not matches:
            return
        source.text = original[: matches[0][0].start()] or None
        for match_index, (match, target) in enumerate(matches):
            name = match.group(0)
            link = ET.Element(
                "a",
                {
                    "class": "ctags-reference",
                    "href": target.url_from(self.file),
                },
            )
            link.text = name
            if match_index + 1 < len(matches):
                next_match, _ = matches[match_index + 1]
                next_start = next_match.start()
            else:
                next_start = len(original)
            link.tail = original[match.end() : next_start] or None
            source.append(link)


def build_tag_index(files: List[FileEntry], progress=None) -> Optional[TagsParsed]:
    """Return symbols mapped to their EPUB file and fragment, using Universal Ctags."""
    executable = shutil.which("ctags")
    if not files:
        return None
    if not executable:
        logger.warning("Universal Ctags not found; EPUB cross references are disabled")
        return None

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
        if not ctags_processor.stdout:
            raise IOError("Failed to open stdout")

        def write_stdin():
            if not ctags_processor.stdin:
                raise IOError("Failed to open stdin")
            ctags_processor.stdin.writelines(
                f"{file.absolute_path}\n" for file in files
            )
            ctags_processor.stdin.close()

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
        return None

    tags = list(parse_ctags_output(files, output_lines))
    if not tags:
        return None

    return (
        sort_and_group(tags, lambda tag: (tag.name, tag.file_index, tag.line_number)),
        sort_and_group(tags, lambda tag: (tag.file_index, tag.line_number)),
    )


def parse_ctags_output(
    files: List[FileEntry], output_lines: List[str]
) -> Generator[Tag, None, None]:
    path_to_file = {
        str(file.absolute_path): (index, file) for index, file in enumerate(files)
    }
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
        filepath = record.get("path")
        entry = path_to_file.get(filepath) if filepath is not None else None
        if not entry:
            logger.debug("Skipping missing %r", record)
            continue
        index, file = entry
        line_number = record.get("line")
        if not isinstance(line_number, int) or file.language == "markdown":
            logger.debug("Skipping unsupported file %r", record)
            continue
        yield Tag(name, file, index, record)


def link_tag_references(
    html: str, file: FileEntry, file_index: int, tags: TagsParsed
) -> str:
    """Link visible identifier text and add definition anchors to a source fragment."""
    _, tags_by_file = tags
    wrapper = ET.fromstring(f"<fragment>{html}</fragment>")
    _insert_line_anchors(wrapper, tags_by_file.get(file_index, []))
    _TagReferenceLinker(file, file_index, tags).transform(wrapper)
    return "".join(
        ET.tostring(child, encoding="unicode", method="xml") for child in wrapper
    )


def _insert_line_anchors(wrapper: ET.Element, tags: Collection[Tag]) -> None:
    """Insert XHTML anchors at source line starts in a highlighted code block."""
    if not tags:
        return

    lines = wrapper.find(f".//ol[@class='{SOURCE_LINES_CLASSNAME}']")
    if lines is None:
        return

    for tag in tags:
        if tag.line_number < 1 or tag.line_number > len(lines):
            logger.debug("Skipping out-of-range Ctags line %s", tag.line_number)
            continue
        anchor = ET.Element("span", {"id": tag.fragment})
        lines[tag.line_number - 1].insert(0, anchor)
