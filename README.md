# Semantic Folder Organizer

A CLI tool that uses the Gemini API to organize files based on their actual content — not just file extensions. It reads text previews from your files, understands what they're about, and proposes a semantic folder structure.

## How It Works

1. You provide a folder path
2. The tool scans all files and reads content previews (.txt, .csv, .pdf)
3. Gemini analyzes the content and proposes category-based subfolders
4. You review the plan, approve it, or give feedback to revise
5. Files are moved into the new folder structure

## Architecture

| Component | Role |
|---|---|
| `file_server.py` | MCP server that scans folders and moves files |
| `cli_agent.py` | LangChain agent that orchestrates the AI and user interaction |

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

```
Enter the absolute path to the folder you want to organize:
> C:\Users\you\Downloads

Scanning folder and analyzing content...

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

Done! Your files have been organized.
```

You can provide feedback instead of approving to refine the categories:

```
> put Archives and Images under Miscellaneous
```

The agent will revise the plan and ask for approval again.

## Supported File Types for Content Analysis

| Type | Method |
|---|---|
| `.txt`, `.csv` | Reads first 1000 characters |
| `.pdf` | Extracts text from first page (up to 1000 chars) |
| All other files | Categorized by filename and extension only |

## Requirements

- Python 3.11+
- Google API key with Gemini access
