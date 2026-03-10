import asyncio
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import os

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent


SYSTEM_PROMPT = """You are a semantic file organizer. Your job is to analyze files in a folder
and propose an organization plan based on the actual content and meaning of the files.

When you receive file listings with content previews, analyze them deeply:
- Group files by their semantic meaning, topic, or purpose (not just file extension).
- Create descriptive subfolder names that reflect the content categories.
- Explain WHY you grouped certain files together.

CRITICAL: At the END of your response, you MUST include a ```json code block containing
a JSON object mapping category folder names to arrays of filenames. Use EXACT filenames
from the scan. Example:

```json
{
  "Documents": ["report.pdf", "notes.txt"],
  "Images": ["photo.jpg", "screenshot.png"],
  "Spreadsheets": ["data.csv", "budget.xlsx"]
}
```

Rules:
- Every file from the scan MUST appear in exactly one category.
- Use the exact filenames as they appear in the scan results.
- Do NOT include paths, just filenames.
- If the user gives feedback, revise the categories and output the updated JSON.
"""


def extract_category_map(text: str) -> dict[str, list[str]] | None:
    """Extract category mapping from LLM response."""
    # Try code blocks first
    lines = text.split('\n')
    blocks = []
    current_block = []
    in_block = False

    for line in lines:
        if line.strip().startswith('```') and not in_block:
            in_block = True
            current_block = []
        elif line.strip().startswith('```') and in_block:
            in_block = False
            blocks.append('\n'.join(current_block))
        elif in_block:
            current_block.append(line)

    for block in reversed(blocks):
        block = block.strip()
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and all(isinstance(v, list) for v in parsed.values()):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue

    # Fallback: find JSON objects by brace counting
    i = 0
    candidates = []
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            while i < len(text):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        if '[' in candidate:
                            candidates.append(candidate)
                        break
                i += 1
        i += 1

    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and all(isinstance(v, list) for v in parsed.values()):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def build_move_plan(folder_path: str, category_map: dict[str, list[str]]) -> list[dict]:
    """Build a full move plan from a category mapping."""
    folder = folder_path.replace('\\', '/')
    if not folder.endswith('/'):
        folder += '/'

    plan = []
    for category, filenames in category_map.items():
        for filename in filenames:
            plan.append({
                "source": f"{folder}{filename}",
                "dest": f"{folder}{category}/{filename}",
            })
    return plan


def strip_json_block(text: str) -> str:
    """Remove JSON code blocks and lead-in lines from display text."""
    lines = text.split('\n')
    result = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('```'):
            if not in_code_block:
                if result and any(kw in result[-1].lower() for kw in ['json', 'plan', "here's", 'below', 'mapping']):
                    result.pop()
                in_code_block = True
            else:
                in_code_block = False
            continue
        if not in_code_block:
            result.append(line)

    output = '\n'.join(result)
    output = re.sub(r'\n{3,}', '\n\n', output).strip()
    return output


async def run_agent():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("Error: GOOGLE_API_KEY not found. Set it in your .env file.")
        sys.exit(1)

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    folder_path = input("\nEnter the absolute path to the folder you want to organize:\n> ").strip()

    if not folder_path:
        print("Error: No path provided.")
        sys.exit(1)

    target = Path(folder_path)
    if not target.exists() or not target.is_dir():
        print(f"Error: '{folder_path}' is not a valid directory.")
        sys.exit(1)

    print(f"\nUsing model: {model_name}")
    print(f"Target folder: {folder_path}")
    print("Connecting to MCP server...\n")

    server_script = str(Path(__file__).parent / "file_server.py")

    client = MultiServerMCPClient(
        {
            "file_organizer": {
                "command": sys.executable,
                "args": [server_script],
                "transport": "stdio",
            }
        }
    )
    tools = await client.get_tools()

    # Get reference to organize_files tool for direct invocation
    organize_tool = next((t for t in tools if t.name == "organize_files"), None)
    if not organize_tool:
        print("Error: organize_files tool not found on MCP server.")
        sys.exit(1)

    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
    )

    agent = create_agent(llm, tools)

    # Step 1: Scan the folder
    print("Scanning folder and analyzing content...\n")
    scan_message = (
        f"Scan the folder at '{folder_path}' using the scan_folder tool, "
        f"then analyze the files and propose an organization plan. "
        f"At the end, include a ```json code block with a category mapping object "
        f"(category names as keys, arrays of filenames as values)."
    )

    response = await agent.ainvoke(
        {"messages": [("system", SYSTEM_PROMPT), ("human", scan_message)]}
    )

    last_message = response["messages"][-1]
    print("=" * 60)
    print("PROPOSED ORGANIZATION PLAN")
    print("=" * 60)
    print(strip_json_block(last_message.content))
    print("=" * 60)

    # Extract category map and build move plan
    category_map = extract_category_map(last_message.content)
    plan = None
    if category_map:
        plan = build_move_plan(folder_path, category_map)
        total_files = sum(len(v) for v in category_map.values())
        print(f"\n[Plan: {total_files} files into {len(category_map)} categories]")
    else:
        print("\n[Warning: Could not extract category plan from response]")

    # Step 2: Human-in-the-loop feedback cycle
    messages = response["messages"]

    while True:
        user_input = input(
            "\nType 'approve' to execute, or provide feedback to revise:\n> "
        ).strip()

        if not user_input:
            print("No input received. Please type 'approve' or give feedback.")
            continue

        if user_input.lower() in ("approve", "yes", "y", "ok", "go"):
            if not plan:
                print("No valid plan to execute. Please provide feedback to generate one.")
                continue

            print(f"\nExecuting organization plan ({len(plan)} moves)...\n")

            move_plan_json = json.dumps(plan)
            try:
                result = await organize_tool.ainvoke({"move_plan": move_plan_json})
            except Exception as e:
                print(f"Error calling organize_files: {e}")
                break

            print("=" * 60)
            print("EXECUTION RESULT")
            print("=" * 60)
            print(result)
            print("=" * 60)
            print("\nDone! Your files have been organized.")
            break
        else:
            # User provided feedback — ask the agent to revise
            messages.append(
                ("human", (
                    f"User feedback: \"{user_input}\"\n\n"
                    f"Revise the plan. Include the COMPLETE updated category mapping "
                    f"as a ```json code block at the end (category names → filename arrays)."
                ))
            )
            print("\nRevising plan based on your feedback...\n")

            response = await agent.ainvoke({"messages": messages})
            messages = response["messages"]
            last_message = messages[-1]

            print("=" * 60)
            print("REVISED ORGANIZATION PLAN")
            print("=" * 60)
            print(strip_json_block(last_message.content))
            print("=" * 60)

            new_map = extract_category_map(last_message.content)
            if new_map:
                category_map = new_map
                plan = build_move_plan(folder_path, category_map)
                total_files = sum(len(v) for v in category_map.values())
                print(f"\n[Updated plan: {total_files} files into {len(category_map)} categories]")
            else:
                # Retry: ask for just the JSON
                print("[Requesting category mapping from agent...]")
                messages.append(
                    ("human", "Output ONLY the category mapping as a ```json code block. No explanation. Just the JSON object with category names as keys and filename arrays as values.")
                )
                retry_response = await agent.ainvoke({"messages": messages})
                messages = retry_response["messages"]
                new_map = extract_category_map(messages[-1].content)
                if new_map:
                    category_map = new_map
                    plan = build_move_plan(folder_path, category_map)
                    total_files = sum(len(v) for v in category_map.values())
                    print(f"[Updated plan: {total_files} files into {len(category_map)} categories]")
                else:
                    print("[Warning: Could not extract plan. Previous plan is still active if available.]")


def main():
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
