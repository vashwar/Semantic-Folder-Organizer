import json
import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SemanticFileOrganizer")


def _read_text_preview(file_path: Path, max_chars: int = 1000) -> str:
    """Read the first max_chars characters from a text-based file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"[Error reading file: {e}]"


def _read_pdf_preview(file_path: Path, max_chars: int = 1000) -> str:
    """Extract text from the first page of a PDF file."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        if len(reader.pages) == 0:
            return "[Empty PDF]"
        text = reader.pages[0].extract_text() or ""
        return text[:max_chars]
    except Exception as e:
        return f"[Error reading PDF: {e}]"


@mcp.tool()
def scan_folder(folder_path: str, offset: int = 0, limit: int = 0) -> str:
    """Scan a folder and return file names, sizes, and content previews for supported types.

    Args:
        folder_path: Absolute path to the folder to scan.
        offset: Number of files to skip from the beginning (default 0).
        limit: Maximum number of files to return. 0 means return all files.
    """
    path = Path(folder_path)

    if not path.exists():
        return f"Error: The path '{folder_path}' does not exist."
    if not path.is_dir():
        return f"Error: The path '{folder_path}' is not a directory."

    files = sorted(f for f in path.iterdir() if f.is_file())
    total_count = len(files)

    if total_count == 0:
        return f"The folder '{folder_path}' contains no files."

    # Apply offset/limit slicing
    if limit > 0:
        files = files[offset : offset + limit]

    results = []
    for file in files:
        size_bytes = file.stat().st_size
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes / (1024 * 1024):.1f} MB"

        entry = f"- {file.name} ({size_str})"

        suffix = file.suffix.lower()
        if suffix in (".txt", ".csv"):
            preview = _read_text_preview(file)
            entry += f"\n  Content Preview: {preview}"
        elif suffix == ".pdf":
            preview = _read_pdf_preview(file)
            entry += f"\n  Content Preview: {preview}"

        results.append(entry)

    header = f"Files in '{folder_path}' (showing {len(files)} of {total_count} total files):\n"
    return header + "\n".join(results)


@mcp.tool()
def organize_files(move_plan: str) -> str:
    """Move files according to a plan. Takes a JSON string: a list of objects each with 'source' and 'dest' absolute path keys. Example: [{"source": "C:/a/b.txt", "dest": "C:/a/docs/b.txt"}]"""
    try:
        parsed = json.loads(move_plan)
    except (json.JSONDecodeError, TypeError) as e:
        return f"Error: Could not parse move_plan as JSON: {e}\nReceived: {move_plan!r}"

    if not isinstance(parsed, list):
        return f"Error: move_plan must be a JSON array, got {type(parsed).__name__}."

    if not parsed:
        return "Error: The move plan is empty."

    log_lines = []
    success_count = 0
    error_count = 0

    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            log_lines.append(f"[{i + 1}] SKIP - Not a dict: {item}")
            error_count += 1
            continue

        source = item.get("source")
        dest = item.get("dest")

        if not source or not dest:
            log_lines.append(f"[{i + 1}] SKIP - Missing 'source' or 'dest': {item}")
            error_count += 1
            continue

        source_path = Path(source)
        dest_path = Path(dest)

        if not source_path.exists():
            log_lines.append(f"[{i + 1}] ERROR - Source not found: {source}")
            error_count += 1
            continue

        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(dest_path))
            log_lines.append(f"[{i + 1}] OK - Moved: {source} -> {dest}")
            success_count += 1
        except Exception as e:
            log_lines.append(f"[{i + 1}] ERROR - {source}: {e}")
            error_count += 1

    summary = f"\nSummary: {success_count} moved, {error_count} errors."
    return "\n".join(log_lines) + summary


if __name__ == "__main__":
    mcp.run()
