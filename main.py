#!/usr/bin/env python3
"""
Zava Logistics - Capacity Planning Demo
========================================

This script demonstrates the Microsoft Agent Framework Group Chat Pattern
with a manager agent coordinating analyst and reviewer agents.

Use Case: Analyze next month's expected package volume and recommend
          capacity and optimized routes for Zava Logistics.

Agents:
  - Manager Agent: Coordinates the discussion, selects who speaks next
  - Analyst Agent: Uses Code Interpreter to analyze package demand data
  - Reviewer Agent: Uses File Search to look up company documentation

Requirements:
  - pip install agent-framework-azure-ai --pre
  - pip install azure-identity python-dotenv

Environment Variables (set in .env file):
  - AZURE_AI_PROJECT_ENDPOINT: Your Azure AI Foundry project endpoint
  - AZURE_AI_MODEL_DEPLOYMENT_NAME: Your model deployment (e.g., gpt-5-mini)
"""

import asyncio
import json
import os
from pathlib import Path

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()
from typing import cast

from azure.identity.aio import AzureCliCredential

from agent_framework import (
    AgentRunUpdateEvent,
    ChatAgent,
    ChatMessage,
    GroupChatBuilder,
    HostedFileSearchTool,
    HostedVectorStoreContent,
    Role,
    WorkflowOutputEvent,
)
from agent_framework.azure import AzureAIAgentClient


# =============================================================================
# CONFIGURATION
# =============================================================================

# Paths to data files (relative to this script)
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
DOCS_DIR = SCRIPT_DIR / "docs"

CSV_FILE = DATA_DIR / "package_demand.csv"
DOC_FILES = [
    DOCS_DIR / "fleet_specifications.md",
    DOCS_DIR / "route_network.md",
    DOCS_DIR / "capacity_policy.md",
    DOCS_DIR / "peak_season_guidelines.md",
    DOCS_DIR / "cost_efficiency_targets.md",
]

# The task for our agents to solve
USER_TASK = """
Analyze next month's (February 2026) expected package volume for Zava Logistics
and recommend capacity and optimized routes.

Please:
1. Analyze the package demand data to identify volume trends and peak periods
2. Check our fleet capacity and policies to ensure we can handle the demand
3. Provide specific recommendations for capacity adjustments if needed
"""

# Maximum number of conversation turns
# 4 rounds × 2 agents per round = 8 agent responses + manager selections
# We count assistant messages (includes manager), so ~12-16 total
MAX_TURNS = 16

# Phrase that signals the manager has completed the analysis
COMPLETION_PHRASE = "The analysis is complete"


# =============================================================================
# CLI FORMATTING HELPERS
# =============================================================================

# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


def print_header():
    """Print the application header."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}")
    print("═" * 64)
    print("               ZAVA LOGISTICS")
    print("         Capacity Planning Assistant")
    print("═" * 64)
    print(f"{Colors.END}")


def print_task(task: str):
    """Print the user's task."""
    print(f"{Colors.BOLD}📋 Task:{Colors.END}")
    print(f"{Colors.YELLOW}{task}{Colors.END}")
    print()


def get_agent_style(agent_name: str) -> tuple[str, str, str]:
    """Get icon, color, and display name for an agent."""
    if "manager" in agent_name.lower():
        return "🎯", Colors.GREEN, "MANAGER"
    elif "analyst" in agent_name.lower():
        return "📊", Colors.BLUE, "ANALYST"
    elif "reviewer" in agent_name.lower():
        return "📚", Colors.CYAN, "REVIEWER"
    else:
        return "💬", Colors.YELLOW, agent_name.upper()


def format_manager_output(message: str) -> str:
    """Parse and format manager JSON output into readable sections."""
    try:
        data = json.loads(message)

        output_parts = []

        # Next speaker
        if "selected_participant" in data and data["selected_participant"]:
            participant = data["selected_participant"].replace("Agent", "")
            output_parts.append(f"{Colors.YELLOW}▶ Next Speaker:{Colors.END} {participant}")

        # Instruction/task
        if "instruction" in data and data["instruction"]:
            instruction = data["instruction"]
            # Truncate if too long
            if len(instruction) > 200:
                instruction = instruction[:200] + "..."
            output_parts.append(f"{Colors.YELLOW}▶ Task:{Colors.END} {instruction}")

        # Final message (if finishing)
        if data.get("finish") and data.get("final_message"):
            output_parts.append(f"{Colors.YELLOW}▶ Conclusion:{Colors.END} {data['final_message']}")

        return "\n".join(output_parts) if output_parts else message

    except json.JSONDecodeError:
        # Not JSON, return as-is
        return message


def print_agent_message(agent_name: str, message: str):
    """Print a formatted agent message with appropriate icon and color."""
    icon, color, display_name = get_agent_style(agent_name)

    # Print the agent header
    print(f"\n{Colors.BOLD}{'─' * 64}{Colors.END}")
    print(f"{color}{Colors.BOLD}{icon} [{display_name}]{Colors.END}")
    print(f"{'─' * 64}")

    # Format manager output specially (parse JSON)
    if "manager" in agent_name.lower():
        formatted_message = format_manager_output(message)
    else:
        formatted_message = message

    # Print the message content
    print(f"{formatted_message}")


def extract_text_from_event(event_data) -> str:
    """Extract text content from an AgentRunUpdateEvent data object."""
    if event_data is None:
        return ""

    # Try to get direct text property
    if hasattr(event_data, 'text') and event_data.text:
        text = event_data.text
        # TextContent object has a 'text' attribute itself
        if hasattr(text, 'text'):
            return str(text.text)
        return str(text)

    # Try to iterate through contents
    if hasattr(event_data, 'contents') and event_data.contents:
        text_parts = []
        for content in event_data.contents:
            if hasattr(content, 'text') and content.text:
                text_parts.append(str(content.text))
        if text_parts:
            return "".join(text_parts)

    return ""


def print_separator():
    """Print a section separator."""
    print(f"\n{Colors.BOLD}{'═' * 64}{Colors.END}")


def print_status(message: str):
    """Print a status message."""
    print(f"{Colors.YELLOW}⏳ {message}{Colors.END}")


def print_success(message: str):
    """Print a success message."""
    print(f"{Colors.GREEN}✅ {message}{Colors.END}")


def print_error(message: str):
    """Print an error message."""
    print(f"{Colors.RED}❌ {message}{Colors.END}")


# =============================================================================
# AGENT INSTRUCTIONS
# =============================================================================

MANAGER_INSTRUCTIONS = """
You are the Planning Manager at Zava Logistics coordinating capacity planning.

STRICT ROTATION RULE - You MUST follow this exact order every time:
1. ALWAYS select AnalystAgent first
2. ALWAYS select ReviewerAgent second
3. Repeat this pattern: Analyst → Reviewer → Analyst → Reviewer...

NEVER select the same agent twice in a row. NEVER select Reviewer before Analyst in a round.

Keep your instructions to 1-2 sentences. Be direct.

After 4 rounds (8 agent responses total), provide a 2-sentence final recommendation and end with exactly: "The analysis is complete."
"""

ANALYST_INSTRUCTIONS = """
You are a Data Analyst at Zava Logistics. Use this February 2026 demand data:

TOTAL: 425,000 packages / 1,530,000 kg

BY ROUTE: LAX-JFK: 156K (37%), ORD-MIA: 98K (23%), SEA-DFW: 68K (16%), DEN-ATL: 73K (17%)

VALENTINE'S PEAK (Feb 10-14): Feb 13 is peak day at 17,700 packages (+58% vs daily avg of 11,200).

RESPONSE RULES:
- Maximum 3 sentences per response
- Include specific numbers
- Be direct, no filler phrases
"""

REVIEWER_INSTRUCTIONS = """
You are an Operations Reviewer at Zava Logistics with access to company documentation.

Search the docs for relevant policies, fleet specs, and constraints. Cite the specific document name.

RESPONSE RULES:
- Maximum 3 sentences per response
- Always reference the source document
- Be direct, no filler phrases
"""


# =============================================================================
# MAIN APPLICATION
# =============================================================================

def count_assistant_messages(messages: list[ChatMessage]) -> int:
    """Count the number of assistant messages in the conversation."""
    return sum(1 for msg in messages if msg.role == Role.ASSISTANT)


def should_terminate(messages: list[ChatMessage]) -> bool:
    """Check if the conversation should terminate.

    Terminates when:
    1. The completion phrase is found in any message, OR
    2. Maximum turns reached (backup)
    """
    # Check for completion phrase
    for msg in messages:
        if msg.role == Role.ASSISTANT and msg.text:
            if COMPLETION_PHRASE.lower() in msg.text.lower():
                return True

    # Backup: max turns reached
    return count_assistant_messages(messages) >= MAX_TURNS


async def main():
    """Main function to run the capacity planning demo."""

    print_header()
    print_task(USER_TASK)

    # Check environment variables
    project_endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    model_deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5-mini")

    if not project_endpoint:
        print_error("AZURE_AI_PROJECT_ENDPOINT environment variable not set!")
        print("Please set it to your Azure AI Foundry project endpoint.")
        print("Example: export AZURE_AI_PROJECT_ENDPOINT='https://your-project.services.ai.azure.com/api/projects/your-project-id'")
        return

    # Verify data files exist
    if not CSV_FILE.exists():
        print_error(f"Data file not found: {CSV_FILE}")
        return

    for doc_file in DOC_FILES:
        if not doc_file.exists():
            print_error(f"Documentation file not found: {doc_file}")
            return

    print_success("All data files found")
    print_status("Connecting to Azure AI Foundry...")

    # Create Azure credential
    credential = AzureCliCredential()

    # Track resources for cleanup
    uploaded_file_ids = []
    vector_store_id = None

    try:
        # Create separate AzureAIAgentClient instances for each agent
        # (Required for proper routing in group chat with Azure AI Agents)
        manager_client = AzureAIAgentClient(credential=credential)
        analyst_client = AzureAIAgentClient(credential=credential)
        reviewer_client = AzureAIAgentClient(credential=credential)

        async with manager_client, analyst_client, reviewer_client:
            print_success("Connected to Azure AI Foundry")

            # =================================================================
            # STEP 1: Upload docs and create vector store for File Search
            # =================================================================
            print_status("Uploading documentation files for File Search...")

            doc_file_ids = []
            for doc_file in DOC_FILES:
                print_status(f"  Uploading: {doc_file.name}")
                uploaded_doc = await reviewer_client.agents_client.files.upload_and_poll(
                    file_path=str(doc_file),
                    purpose="assistants"
                )
                doc_file_ids.append(uploaded_doc.id)
                uploaded_file_ids.append(uploaded_doc.id)

            # Create vector store with the documentation files
            print_status("Creating vector store...")
            vector_store = await reviewer_client.agents_client.vector_stores.create_and_poll(
                file_ids=doc_file_ids,
                name="zava-logistics-docs"
            )
            vector_store_id = vector_store.id
            print_success(f"Vector store created (ID: {vector_store_id})")

            # =================================================================
            # STEP 3: Create the File Search tool for Reviewer
            # =================================================================
            file_search_tool = HostedFileSearchTool(
                inputs=[HostedVectorStoreContent(vector_store_id=vector_store_id)]
            )

            # =================================================================
            # STEP 4: Create Agents (each with own client instance)
            # =================================================================
            # Create the Manager Agent (no tools)
            print_status("Creating Manager Agent...")
            manager_agent = ChatAgent(
                chat_client=manager_client,
                name="ManagerAgent",
                instructions=MANAGER_INSTRUCTIONS,
            )
            print_success("Manager Agent created")

            # Create the Analyst Agent (with embedded data)
            print_status("Creating Analyst Agent...")
            analyst_agent = ChatAgent(
                chat_client=analyst_client,
                name="AnalystAgent",
                instructions=ANALYST_INSTRUCTIONS,
            )
            print_success("Analyst Agent created")

            # Create the Reviewer Agent (with File Search)
            print_status("Creating Reviewer Agent with File Search...")
            reviewer_agent = ChatAgent(
                chat_client=reviewer_client,
                name="ReviewerAgent",
                instructions=REVIEWER_INSTRUCTIONS,
                tools=file_search_tool,
            )
            print_success("Reviewer Agent created with File Search")

            # =================================================================
            # STEP 5: Build the Group Chat Workflow
            # =================================================================
            print_status("Building Group Chat workflow...")

            workflow = (
                GroupChatBuilder()
                .set_manager(manager_agent, display_name="Manager")
                .participants([analyst_agent, reviewer_agent])
                .with_termination_condition(should_terminate)
                .build()
            )

            print_success("Group Chat workflow ready")
            print_separator()
            print(f"{Colors.BOLD}🚀 Starting Agent Collaboration...{Colors.END}")
            print_separator()

            # =================================================================
            # STEP 6: Run the Workflow
            # =================================================================

            current_agent = None
            current_message = ""
            turn_count = 0

            async for event in workflow.run_stream(USER_TASK):
                if isinstance(event, AgentRunUpdateEvent):
                    # Get the agent identifier
                    agent_id = event.executor_id or "Unknown"

                    # Check if we're starting a new agent's turn
                    if agent_id != current_agent:
                        # Print the previous agent's complete message
                        if current_agent and current_message.strip():
                            print_agent_message(current_agent, current_message.strip())
                            turn_count += 1

                        # Start collecting the new agent's message
                        current_agent = agent_id
                        current_message = ""

                    # Accumulate the message content
                    text = extract_text_from_event(event.data)
                    if text:
                        current_message += text

                elif isinstance(event, WorkflowOutputEvent):
                    # Print any remaining message
                    if current_agent and current_message.strip():
                        print_agent_message(current_agent, current_message.strip())

                    # Workflow completed
                    print_separator()
                    print(f"{Colors.BOLD}{Colors.GREEN}✅ Capacity Planning Analysis Complete{Colors.END}")
                    print_separator()

                    # Get the final conversation
                    final_messages = cast(list[ChatMessage], event.data)
                    print(f"\n{Colors.CYAN}Total conversation turns: {len([m for m in final_messages if m.role == Role.ASSISTANT])}{Colors.END}")

            print_success("Workflow completed successfully")

            # =================================================================
            # STEP 7: Optional Cleanup
            # =================================================================
            print()
            print(f"{Colors.YELLOW}{'─' * 64}{Colors.END}")
            print(f"{Colors.BOLD}🧹 Resource Cleanup{Colors.END}")
            print(f"{Colors.YELLOW}{'─' * 64}{Colors.END}")
            print()
            print("The following resources were created in Azure AI Foundry:")
            print(f"  • 3 Agents: ManagerAgent, AnalystAgent, ReviewerAgent")
            print(f"  • 1 Vector Store: {vector_store_id}")
            print(f"  • {len(uploaded_file_ids)} Files (documentation)")
            print()

            cleanup_input = input(f"{Colors.CYAN}Do you want to delete these resources? (y/n): {Colors.END}").strip().lower()

            if cleanup_input == 'y' or cleanup_input == 'yes':
                print()
                print_status("Cleaning up resources...")

                # Delete agents
                try:
                    await manager_client.agents_client.delete_agent(manager_agent.id)
                    print_status(f"  Deleted agent: ManagerAgent")
                except Exception as e:
                    print_status(f"  Could not delete ManagerAgent: {e}")

                try:
                    await analyst_client.agents_client.delete_agent(analyst_agent.id)
                    print_status(f"  Deleted agent: AnalystAgent")
                except Exception as e:
                    print_status(f"  Could not delete AnalystAgent: {e}")

                try:
                    await reviewer_client.agents_client.delete_agent(reviewer_agent.id)
                    print_status(f"  Deleted agent: ReviewerAgent")
                except Exception as e:
                    print_status(f"  Could not delete ReviewerAgent: {e}")

                # Delete vector store
                if vector_store_id:
                    try:
                        await reviewer_client.agents_client.vector_stores.delete(vector_store_id)
                        print_status(f"  Deleted vector store: {vector_store_id}")
                    except Exception as e:
                        print_status(f"  Could not delete vector store: {e}")

                # Delete uploaded documentation files
                for file_id in uploaded_file_ids:
                    try:
                        await reviewer_client.agents_client.files.delete(file_id=file_id)
                        print_status(f"  Deleted file: {file_id}")
                    except Exception as e:
                        print_status(f"  Could not delete file {file_id}: {e}")

                print_success("Cleanup completed")
            else:
                print()
                print(f"{Colors.CYAN}Resources kept. You can view them in the Azure AI Foundry portal.{Colors.END}")
                print(f"{Colors.CYAN}To delete later, go to your project → Agents / Files sections.{Colors.END}")

    except Exception as e:
        print_error(f"An error occurred: {str(e)}")
        raise

    finally:
        await credential.close()


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    print(f"\n{Colors.BOLD}Starting Zava Logistics Capacity Planning Demo...{Colors.END}\n")
    asyncio.run(main())
