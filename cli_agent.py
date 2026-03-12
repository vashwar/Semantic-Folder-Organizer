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


BATCH_SIZE = 200

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


def _tool_result_to_str(result) -> str:
    """Convert an MCP tool result to a plain string (handles list-of-content-blocks)."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in result
        )
    return str(result)


def merge_category_maps(maps: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    """Merge multiple category maps into one, combining file lists for matching categories."""
    merged: dict[str, list[str]] = {}
    for m in maps:
        for category, filenames in m.items():
            if category in merged:
                merged[category].extend(filenames)
            else:
                merged[category] = list(filenames)
    return merged


def _build_batch_prompt(file_data: str, existing_categories: list[str] | None = None) -> str:
    """Build the prompt sent to the LLM for a single batch."""
    prompt = (
        "You are a semantic file organizer. Analyze the following files and categorize them "
        "by their actual content, meaning, or purpose (not just file extension).\n\n"
    )
    if existing_categories:
        prompt += (
            "IMPORTANT: Previous batches have already established these categories:\n"
            + ", ".join(f'"{c}"' for c in existing_categories)
            + "\nReuse these category names when files fit. Only create a new category "
            "if a file truly doesn't belong in any existing one.\n\n"
        )
    prompt += (
        f"{file_data}\n\n"
        "Output ONLY a JSON object mapping category folder names to arrays of filenames. "
        "Use EXACT filenames from the listing above. Every file must appear in exactly one category. "
        "No explanation, no markdown fences — just the raw JSON object."
    )
    return prompt


async def process_large_folder(
    llm,
    scan_tool,
    organize_tool,
    folder_path: str,
    total_files: int,
) -> tuple[dict[str, list[str]] | None, list[dict] | None]:
    """Process a large folder in batches, returning (category_map, move_plan) or (None, None)."""
    import math

    num_batches = math.ceil(total_files / BATCH_SIZE)
    print(f"\nLarge folder detected ({total_files} files). Processing in {num_batches} batches of {BATCH_SIZE}...\n")

    all_maps: list[dict[str, list[str]]] = []
    all_categories: list[str] = []

    for batch_idx in range(num_batches):
        offset = batch_idx * BATCH_SIZE
        end = min(offset + BATCH_SIZE, total_files)
        print(f"[Batch {batch_idx + 1}/{num_batches}: processing files {offset + 1}-{end}...]")

        # Get this batch's file data via MCP tool
        scan_result = _tool_result_to_str(await scan_tool.ainvoke({
            "folder_path": folder_path,
            "offset": str(offset),
            "limit": str(BATCH_SIZE),
        }))

        # Build prompt and call LLM directly (no agent overhead)
        prompt = _build_batch_prompt(scan_result, all_categories if all_categories else None)
        response = await llm.ainvoke(prompt)
        response_text = response.content if hasattr(response, "content") else str(response)

        # Extract category map from this batch
        batch_map = extract_category_map(response_text)
        if batch_map is None:
            # Retry once asking for just JSON
            retry_prompt = (
                "Your previous response could not be parsed. Output ONLY a valid JSON object "
                "mapping category names to arrays of filenames. No markdown, no explanation.\n\n"
                f"Files to categorize:\n{scan_result}"
            )
            retry_response = await llm.ainvoke(retry_prompt)
            retry_text = retry_response.content if hasattr(retry_response, "content") else str(retry_response)
            batch_map = extract_category_map(retry_text)

        if batch_map:
            batch_file_count = sum(len(v) for v in batch_map.values())
            print(f"  -> {batch_file_count} files into {len(batch_map)} categories")
            all_maps.append(batch_map)
            # Update known categories for priming the next batch
            for cat in batch_map:
                if cat not in all_categories:
                    all_categories.append(cat)
        else:
            print(f"  -> WARNING: Could not extract categories for this batch. Skipping.")

    if not all_maps:
        return None, None

    # Merge all batch maps
    merged = merge_category_maps(all_maps)
    plan = build_move_plan(folder_path, merged)
    return merged, plan


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

    # Get references to tools for direct invocation
    organize_tool = next((t for t in tools if t.name == "organize_files"), None)
    scan_tool = next((t for t in tools if t.name == "scan_folder"), None)
    if not organize_tool or not scan_tool:
        print("Error: Required tools not found on MCP server.")
        sys.exit(1)

    llm = ChatGoogleGenerativeAI(
        model=model_name,
        google_api_key=api_key,
    )

    # Step 1: Quick scan to get total file count
    print("Scanning folder...\n")
    initial_scan = _tool_result_to_str(await scan_tool.ainvoke({
        "folder_path": folder_path,
        "offset": "0",
        "limit": "1",
    }))

    # Parse total file count from header: "... (showing X of Y total files):"
    count_match = re.search(r'of (\d+) total files', initial_scan)
    total_file_count = int(count_match.group(1)) if count_match else 0

    category_map = None
    plan = None
    agent = None
    messages = []  # conversation history for feedback loop (small-folder path)

    if total_file_count > BATCH_SIZE:
        # Large folder: batch processing path
        category_map, plan = await process_large_folder(
            llm, scan_tool, organize_tool, folder_path, total_file_count
        )
        if category_map:
            total_files = sum(len(v) for v in category_map.values())
            print(f"\n{'=' * 60}")
            print("PROPOSED ORGANIZATION PLAN")
            print("=" * 60)
            for cat, files in sorted(category_map.items()):
                print(f"\n  {cat}/ ({len(files)} files)")
                # Show first few filenames as a preview
                for f in files[:5]:
                    print(f"    - {f}")
                if len(files) > 5:
                    print(f"    ... and {len(files) - 5} more")
            print(f"\n{'=' * 60}")
            print(f"\n[Plan: {total_files} files into {len(category_map)} categories]")
        else:
            print("\n[Error: Could not generate a plan for this folder.]")
    else:
        # Small folder: existing single-pass agent flow
        agent = create_agent(llm, tools)

        print("Analyzing content...\n")
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
        if category_map:
            plan = build_move_plan(folder_path, category_map)
            total_files = sum(len(v) for v in category_map.values())
            print(f"\n[Plan: {total_files} files into {len(category_map)} categories]")
        else:
            # Retry: ask the LLM for just the JSON
            print("\n[Could not extract plan from response. Requesting structured output...]")
            messages = response["messages"]
            messages.append(
                ("human", "Output ONLY the category mapping as a ```json code block. No explanation. Just the JSON object with category names as keys and filename arrays as values.")
            )
            retry_response = await agent.ainvoke({"messages": messages})
            messages = retry_response["messages"]
            last_message = messages[-1]
            category_map = extract_category_map(last_message.content)
            if category_map:
                plan = build_move_plan(folder_path, category_map)
                total_files = sum(len(v) for v in category_map.values())
                print(f"[Plan: {total_files} files into {len(category_map)} categories]")
            else:
                print("[Warning: Could not extract category plan. Provide feedback to generate one.]")

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
            # User provided feedback — revise the plan
            print("\nRevising plan based on your feedback...\n")

            if total_file_count > BATCH_SIZE:
                # Large folder: send current category map + feedback to LLM directly
                revision_prompt = (
                    "You are a semantic file organizer. Here is the current category mapping "
                    "for a folder:\n\n"
                    f"```json\n{json.dumps(category_map, indent=2)}\n```\n\n"
                    f"User feedback: \"{user_input}\"\n\n"
                    "Revise the mapping based on this feedback. Output ONLY the complete "
                    "updated JSON object (category names as keys, filename arrays as values). "
                    "Include ALL files — not just the changed ones. No explanation, no markdown fences."
                )
                revision_response = await llm.ainvoke(revision_prompt)
                revision_text = revision_response.content if hasattr(revision_response, "content") else str(revision_response)

                new_map = extract_category_map(revision_text)
                if new_map:
                    category_map = new_map
                    plan = build_move_plan(folder_path, category_map)
                    total_files = sum(len(v) for v in category_map.values())
                    print("=" * 60)
                    print("REVISED ORGANIZATION PLAN")
                    print("=" * 60)
                    for cat, files in sorted(category_map.items()):
                        print(f"\n  {cat}/ ({len(files)} files)")
                        for f in files[:5]:
                            print(f"    - {f}")
                        if len(files) > 5:
                            print(f"    ... and {len(files) - 5} more")
                    print(f"\n{'=' * 60}")
                    print(f"\n[Updated plan: {total_files} files into {len(category_map)} categories]")
                else:
                    print("[Warning: Could not extract revised plan. Previous plan is still active if available.]")
            else:
                # Small folder: use the agent with full conversation history
                if agent is None:
                    agent = create_agent(llm, tools)
                messages.append(
                    ("human", (
                        f"User feedback: \"{user_input}\"\n\n"
                        f"Revise the plan. Include the COMPLETE updated category mapping "
                        f"as a ```json code block at the end (category names → filename arrays)."
                    ))
                )

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
