"""Read and apply each independent Git unified diff against the pinned tree."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
import shlex


HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NO_NEWLINE = b"\\ No newline at end of file"


class PatchError(ValueError):
    pass


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)
    binary: bool = False
    mode_changed: bool = False
    copied: bool = False


def safe_path(path):
    if path is None or path == "/dev/null":
        return None
    posix = PurePosixPath(path)
    if posix.is_absolute() or not posix.parts or any(part in {"..", ".git"} for part in posix.parts):
        raise PatchError("path outside the pinned checkout")
    return posix.as_posix()


def header_path(header):
    value = header.decode("utf-8").strip("\n\r")
    if value.startswith('"'):
        value = shlex.split(value)[0]
    else:
        value = value.split("\t", 1)[0]
    return safe_path(value[2:] if value.startswith(("a/", "b/")) else value)


def read_patch(data):
    # Splitting only at LF preserves CRs and non-ASCII source contents.
    if not data:
        return []
    raw = data.split(b"\n")
    lines = [line + b"\n" for line in raw[:-1]]
    if raw[-1]:
        lines.append(raw[-1])
    files = []
    current = None
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith(b"diff --git "):
            names = shlex.split(line.decode("utf-8").strip())
            old_path, new_path = (None, None)
            if len(names) == 4:
                old_path, new_path = map(header_path, (names[2].encode(), names[3].encode()))
            current = FilePatch(old_path, new_path)
            files.append(current)
        elif line.startswith(b"--- "):
            if index >= len(lines) or not lines[index].startswith(b"+++ "):
                raise PatchError("missing new file header")
            old_path, new_path = header_path(line[4:]), header_path(lines[index][4:])
            if current is None or current.hunks:
                current = FilePatch(old_path, new_path)
                files.append(current)
            current.old_path, current.new_path = old_path, new_path
            index += 1
        elif line.startswith(b"@@"):
            match = HUNK.match(line)
            if match is None or current is None:
                raise PatchError("invalid unified diff hunk")
            old, count, _, new_count = match.groups()
            before = int(count or b"1")
            after = int(new_count or b"1")
            start = int(old) - bool(before)
            entries = []
            while before or after:
                if index >= len(lines):
                    raise PatchError("truncated unified diff hunk")
                entry = lines[index]
                kind, content = entry[:1], entry[1:]
                index += 1
                if index < len(lines) and lines[index].startswith(NO_NEWLINE):
                    content = content.removesuffix(b"\n")
                    index += 1
                if kind not in (b" ", b"+", b"-"):
                    raise PatchError("unexpected line inside hunk")
                before -= kind != b"+"
                after -= kind != b"-"
                if before < 0 or after < 0:
                    raise PatchError("unified diff line count mismatch")
                entries.append((kind, content))
            current.hunks.append((start, entries))
        elif current is not None:
            if line.startswith((b"Binary files ", b"GIT binary patch")):
                current.binary = True
            elif line.startswith((b"old mode ", b"new mode ")):
                current.mode_changed = True
            elif line.startswith((b"rename from ", b"copy from ")):
                prefix, _, name = line.partition(b" from ")
                current.old_path = safe_path(name.decode().rstrip("\r\n"))
                current.copied |= prefix == b"copy"
            elif line.startswith((b"rename to ", b"copy to ")):
                prefix, _, name = line.partition(b" to ")
                current.new_path = safe_path(name.decode().rstrip("\r\n"))
                current.copied |= prefix == b"copy"
    if not files and data.strip():
        raise PatchError("unrecognized patch format")
    if any(not (file.old_path or file.new_path) for file in files):
        raise PatchError("missing file paths")
    return files


def apply_file(original, file):
    raw = original.split(b"\n")
    lines = [line + b"\n" for line in raw[:-1]]
    if raw[-1]:
        lines.append(raw[-1])
    output = []
    cursor = 0
    for start, entries in file.hunks:
        if start < cursor or start > len(lines):
            raise PatchError("hunk outside file")
        output.extend(lines[cursor:start])
        cursor = start
        for kind, content in entries:
            if kind != b"+":
                if cursor >= len(lines) or lines[cursor] != content:
                    raise PatchError("hunk disagrees with pinned file")
                cursor += 1
            if kind != b"-":
                output.append(content)
    output.extend(lines[cursor:])
    return b"".join(output)
