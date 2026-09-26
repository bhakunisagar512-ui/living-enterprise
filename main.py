"""
The Living Enterprise - Stage 1
Four AI agents (Planner -> Retriever -> Executor -> Validator) handle one
enterprise request from intake to a checked draft.
"""
from pathlib import Path

from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import tool

DATA_DIR = Path(__file__).parent / "data"

# ---------- Models ----------
# Stronger model for the thinking roles, cheaper model for routine roles.
smart = LLM(model="anthropic/claude-sonnet-5", max_tokens=4096)
fast = LLM(model="anthropic/claude-haiku-4-5-20251001", max_tokens=4096)


# ---------- Tools: how the Retriever reads company files ----------
@tool("List company documents")
def list_documents() -> str:
    """Lists the file names of all company documents that can be read."""
    return "\n".join(p.name for p in sorted(DATA_DIR.glob("*.txt")))


@tool("Read company document")
def read_document(filename: str) -> str:
    """Reads one company document by its file name, for example 'contract.txt'."""
    path = DATA_DIR / Path(filename).name
    if not path.exists():
        return f"'{filename}' not found. Use 'List company documents' first."
    return path.read_text(encoding="utf-8")


# ---------- Agents: one job each ----------
planner = Agent(
    role="Planner",
    goal="Break an enterprise request into clear steps and list which facts are needed.",
    backstory="A senior operations lead who turns messy requests into precise plans.",
    llm=smart,
    allow_delegation=False,
    verbose=True,
)

retriever = Agent(
    role="Retriever",
    goal="Find every relevant fact in the company documents and say where each came from.",
    backstory="A careful analyst who never states a fact without naming its source file.",
    llm=fast,
    tools=[list_documents, read_document],
    allow_delegation=False,
    verbose=True,
)

executor = Agent(
    role="Executor",
    goal="Draft the reply that resolves the request, using only the retrieved facts.",
    backstory="A procurement specialist who writes firm, professional counter-offers.",
    llm=fast,
    allow_delegation=False,
    verbose=True,
)

validator = Agent(
    role="Validator",
    goal="Check the draft against every policy rule and contract clause, then approve or reject it.",
    backstory="A strict compliance officer who rejects anything that breaks a rule.",
    llm=smart,
    allow_delegation=False,
    verbose=True,
)


# ---------- Tasks: the handoffs between agents ----------
plan_task = Task(
    description=(
        "A new enterprise request has arrived:\n\n{request}\n\n"
        "Break it into numbered steps. List the specific facts that must be "
        "looked up (contract terms, policy rules, spend and performance data)."
    ),
    expected_output="A numbered plan and a list of facts to look up.",
    agent=planner,
)

retrieve_task = Task(
    description=(
        "Follow the plan and look up every needed fact in the company documents. "
        "First list the documents, then read each relevant one. "
        "For every fact, give the source file and the clause or rule number."
    ),
    expected_output="A bullet list of facts, each with its source file and clause/rule number.",
    agent=retriever,
    context=[plan_task],
)

execute_task = Task(
    description=(
        "Using ONLY the retrieved facts, draft the reply to the vendor. "
        "Follow the procurement policy and quote contract clause numbers. "
        "Then add a short internal note explaining the decision."
    ),
    expected_output="A vendor reply draft followed by a short internal note.",
    agent=executor,
    context=[plan_task, retrieve_task],
)

validate_task = Task(
    description=(
        "Check the draft against every policy rule and contract clause in the "
        "retrieved facts. Go rule by rule and mark each one PASS or FAIL. "
        "Start your answer with exactly 'VERDICT: APPROVED' or 'VERDICT: REJECTED'. "
        "If rejected, list exactly what must be fixed."
    ),
    expected_output="A verdict line, a rule-by-rule PASS/FAIL checklist, and fixes if rejected.",
    agent=validator,
    context=[retrieve_task, execute_task],
)


crew = Crew(
    agents=[planner, retriever, executor, validator],
    tasks=[plan_task, retrieve_task, execute_task, validate_task],
    process=Process.sequential,
    verbose=True,
)


if __name__ == "__main__":
    request = (
        "Email from Acme Cloud Services: 'Your contract ends on 31 October 2026. "
        "We would like to renew for another 12 months with a 12% price increase "
        "due to rising infrastructure costs. Please confirm.'"
    )
    result = crew.kickoff(inputs={"request": request})

    print("\n" + "=" * 60)
    print("DRAFT FROM EXECUTOR\n")
    print(execute_task.output.raw)
    print("\n" + "=" * 60)
    print("VALIDATOR VERDICT\n")
    print(result.raw)
