# Semantic Folder Organizer

A CLI tool that uses the Gemini API to organize files based on their actual content — not just file extensions. It reads text previews from your files, understands what they're about, and proposes a semantic folder structure. Handles folders of any size — from a handful of files to 1800+.

## How It Works

1. You provide a folder path
2. The tool scans all files and reads content previews (.txt, .csv, .pdf)
3. Gemini analyzes the content and proposes category-based subfolders
4. You review the plan, approve it, or give feedback to revise
5. Files are moved into the new folder structure
6. A post-move sweep catches any leftover files and sorts them into existing categories

**Large folders (200+ files)** are automatically processed in batches — each batch is sent to the LLM separately, and the results are merged into a single plan. Earlier batch categories are carried forward so naming stays consistent across batches.

## Architecture

| Component | Role |
|---|---|
| `file_server.py` | MCP server that scans folders and moves files (supports paginated scanning via `offset`/`limit`) |
| `cli_agent.py` | LangChain agent that orchestrates the AI and user interaction, with batch processing for large folders |

The two communicate via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) over stdio.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your API key

Create a `.env` file:

```
GOOGLE_API_KEY=your_google_api_key_here
GEMINI_MODEL=gemini-2.0-flash
```

Get an API key from [Google AI Studio](https://aistudio.google.com/apikey).

## Usage

```bash
python cli_agent.py
```

### Small folder (≤ 200 files)

```
Enter the absolute path to the folder you want to organize:
> C:\Users\you\Downloads

Scanning folder...
Analyzing content...

============================================================
PROPOSED ORGANIZATION PLAN
============================================================
Based on the content analysis, here are the proposed categories:

- **Documents**: Reports, syllabi, and written assignments
- **Images**: Screenshots, photos, and graphics
- **Data**: CSV files and spreadsheets
...
============================================================

[Plan: 150 files into 8 categories]

Type 'approve' to execute, or provide feedback to revise:
> approve

Executing organization plan (150 moves)...

[Sweep complete — no files left at top level.]

Done! Your files have been organized.
```

### Large folder (200+ files)

```
Enter the absolute path to the folder you want to organize:
> C:\Users\you\Downloads

Scanning folder...

Large folder detected (1823 files). Processing in 10 batches of 200...

[Batch 1/10: processing files 1-200...]
  -> 200 files into 6 categories
[Batch 2/10: processing files 201-400...]
  -> 200 files into 7 categories
...
[Batch 10/10: processing files 1801-1823...]
  -> 23 files into 4 categories

============================================================
PROPOSED ORGANIZATION PLAN
============================================================

  Data/ (312 files)
    - budget_2024.csv
    - sales_report.csv
    ... and 310 more

  Documents/ (540 files)
    - project_plan.pdf
    - meeting_notes.txt
    ... and 538 more
...
============================================================

[Plan: 1823 files into 9 categories]

Type 'approve' to execute, or provide feedback to revise:
> approve

Executing organization plan (1823 moves)...

[Sweep complete — no files left at top level.]

Done! Your files have been organized.
```

### Providing feedback

You can provide feedback instead of approving to refine the categories:

```
> merge Archives and Images into Miscellaneous
```

The agent will revise the plan and ask for approval again.

## Supported File Types for Content Analysis

| Type | Method |
|---|---|
| `.txt`, `.csv` | Reads first 1000 characters |
| `.pdf` | Extracts text from first page (up to 1000 chars) |
| All other files | Categorized by filename and extension only |

## Key Features

- **Semantic categorization** — groups files by meaning/content, not just extension
- **Batch processing** — handles large folders (1800+ files) by splitting into batches of 200
- **Category consistency** — earlier batch categories are primed into later batches so names stay uniform
- **Post-move sweep** — re-scans after organizing and moves any leftover files into existing categories
- **Fallback safety net** — any file the LLM misses is placed in an "Other" folder so nothing is left behind
- **Human-in-the-loop** — review, revise with natural language feedback, then approve

## Requirements

- Python 3.11+
- Google API key with Gemini access
