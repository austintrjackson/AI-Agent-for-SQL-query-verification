"""
CS 4354 - Homework 3 Part 1: an AI agent for SQL query verification.

The agent (LangChain create_agent + gpt-oss:20b on Ollama) gets one
natural-language question at a time and decides on its own which tools to
call: get_schema, execute_sql, check_answer. Python only runs the tools,
enforces read-only access and the 5-attempt limit, and records everything.

Usage:
    python src/agent.py              # run Q4, Q6, Q7
    python src/agent.py Q6           # run just one question

Outputs (in logs/):
    run_<timestamp>.jsonl       every message / tool call / tool result
    run_<timestamp>_report.md   readable transcript + final report per question
    summary_<timestamp>.csv     one row per question
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_ollama import ChatOllama

import db_tools
from db_tools import ALL_TOOLS, EXPECTED, MAX_MAIN_ATTEMPTS

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
NUM_CTX = int(os.getenv("NUM_CTX", "8192"))      # agent conversations get long
REASONING = os.getenv("REASONING", "medium")      # gpt-oss reasoning effort
RECURSION_LIMIT = 60                              # max agent steps per question

QUESTIONS = ["Q4", "Q6", "Q7"]

SYSTEM_PROMPT = f"""You are a careful SQL verification agent working on a MySQL database called soccerhub.

Your job for each question: produce a SQL query that correctly answers the question, and
build real evidence that the query's LOGIC is right, not just its number.

Tools:
- get_schema(table_name): learn the tables, columns, keys and see sample rows.
- execute_sql(sql): run any read-only query. Use it to explore data and to verify.
  It does not count as an attempt.
- check_answer(question_id, sql): submit your candidate MAIN query. It runs the query
  and tells you whether the single value matches the ground truth. You get at most
  {MAX_MAIN_ATTEMPTS} check_answer attempts per question.

Important principles:
- A query can return the expected number while its logic is wrong (for example by
  coincidence, double counting that happens to cancel out, or a filter that happens
  to work on this data). A match from check_answer is necessary but NOT sufficient.
- So, regardless of whether check_answer says it matches, independently investigate
  whether your SQL truly represents the question. Decide yourself what evidence is
  convincing for THIS question. Examples of things you might do (not a required
  checklist): write an alternative query that reaches the answer a different way and
  compare; look at the underlying rows to see what is being counted; check whether a
  JOIN multiplies rows; check that filters (NULL handling, dates, names) capture
  exactly what the question means; check edge cases in the data.
- If you find a problem, revise the query and check again. Base each next step on what
  the previous tool results told you.
- The database is read-only. Never try to modify it.
- MATCH is a reserved word: write the table as `Match` with backticks.
- Table names are case-sensitive on this server: use Team, Player, `Match`, Appearance exactly.

Status rules:
- "Verified" only if check_answer matched AND your independent checks support the logic.
- "Unresolved" if you used all {MAX_MAIN_ATTEMPTS} attempts without that confidence, or if your
  checks contradict the query. Never claim Verified without evidence.

When you are done, reply with ONLY a JSON object (no markdown) in this form:
{{"question_id": "...",
  "final_sql": "...",
  "numerical_answer": <number or null>,
  "matched_ground_truth": true/false,
  "attempts_used": <int>,
  "verification": [{{"check": "what you checked and why", "sql": "...", "result": "...", "outcome": "what it showed"}}],
  "status": "Verified" or "Unresolved",
  "conclusion": "2-4 sentences explaining why you are (or are not) confident"}}
"""


def build_agent():
    llm = ChatOllama(
        model=MODEL,
        base_url=OLLAMA_URL,
        temperature=0,
        num_ctx=NUM_CTX,
        reasoning=REASONING,   # keeps gpt-oss's reasoning separate so we can log it
    )
    return create_agent(model=llm, tools=ALL_TOOLS, system_prompt=SYSTEM_PROMPT)


def parse_report(text):
    """Pull the JSON report out of the agent's final message."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None


def message_events(messages):
    """Turn the agent's message history into a flat list of log events."""
    events = []
    for m in messages:
        if isinstance(m, AIMessage):
            reasoning = (m.additional_kwargs or {}).get("reasoning_content", "")
            if reasoning:
                events.append({"type": "reasoning", "text": reasoning})
            for tc in m.tool_calls or []:
                events.append({"type": "tool_call", "id": tc.get("id"),
                               "tool": tc["name"], "args": tc["args"]})
            if m.content and not m.tool_calls:
                events.append({"type": "final_message", "text": m.content})
        elif isinstance(m, ToolMessage):
            events.append({"type": "tool_result", "id": m.tool_call_id,
                           "tool": m.name, "result": m.content})
    return events


def tool_facts(events):
    """Facts taken from the actual tool results (not from what the agent claims)."""
    checks = [json.loads(e["result"]) for e in events
              if e["type"] == "tool_result" and e["tool"] == "check_answer"]
    checks = [c for c in checks if isinstance(c, dict)]
    return {
        "tool_calls": sum(e["type"] == "tool_call" for e in events),
        "execute_sql_calls": sum(e["type"] == "tool_call" and e["tool"] == "execute_sql"
                                 for e in events),
        "check_answer_calls": len(checks),
        "any_check_matched": any(c.get("matches_ground_truth") for c in checks),
        "last_check_matched": bool(checks and checks[-1].get("matches_ground_truth")),
    }


def run_question(agent, qid):
    db_tools.reset_attempts()
    question = EXPECTED[qid]["prompt"]
    user_msg = (f"question_id: {qid}\nQuestion: {question}\n\n"
                "Produce a correct SQL query for this question and verify it.")
    print(f"\n=== {qid}: {question}")

    t0 = time.time()
    error = ""
    try:
        state = agent.invoke({"messages": [{"role": "user", "content": user_msg}]},
                             config={"recursion_limit": RECURSION_LIMIT})
        messages = state["messages"]
    except Exception as e:      # e.g. recursion limit hit, Ollama down
        error = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        messages = []
    secs = round(time.time() - t0, 1)

    events = message_events(messages)
    final_text = next((e["text"] for e in reversed(events)
                       if e["type"] == "final_message"), "")
    report = parse_report(final_text)
    facts = tool_facts(events)

    # Safety net: the agent can't call something Verified if no check_answer
    # attempt actually matched. (This only checks the claim against the tool
    # log; it doesn't pick queries for the agent.)
    status = (report or {}).get("status", "Unresolved")
    note = ""
    if status == "Verified" and not facts["last_check_matched"]:
        status, note = "Unresolved", "agent said Verified but its last check_answer did not match"
    if error:
        status, note = "Unresolved", f"run stopped: {error}"

    for e in events:
        if e["type"] == "tool_call":
            print(f"  -> {e['tool']}({json.dumps(e['args'])[:150]})")
    print(f"  status: {status}  ({secs}s, {facts['tool_calls']} tool calls)")

    return {"question_id": qid, "question": question, "seconds": secs,
            "events": events, "final_text": final_text, "report": report,
            "facts": facts, "status": status, "status_note": note, "error": error}


# ---------------- writing the logs ----------------
def write_logs(results, stamp):
    LOG_DIR.mkdir(exist_ok=True)

    with open(LOG_DIR / f"run_{stamp}.jsonl", "w") as f:
        for r in results:
            for i, e in enumerate(r["events"], 1):
                f.write(json.dumps({"question_id": r["question_id"], "step": i, **e},
                                   default=str) + "\n")
            f.write(json.dumps({"question_id": r["question_id"], "step": "final",
                                "report": r["report"], "status": r["status"],
                                "status_note": r["status_note"], "facts": r["facts"],
                                "seconds": r["seconds"], "error": r["error"]},
                               default=str) + "\n")

    import csv
    with open(LOG_DIR / f"summary_{stamp}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "question", "final_sql", "numerical_answer",
                    "matched_ground_truth", "status", "tool_calls", "check_answer_calls",
                    "execute_sql_calls", "seconds", "note"])
        for r in results:
            rep = r["report"] or {}
            w.writerow([r["question_id"], r["question"], rep.get("final_sql", ""),
                        rep.get("numerical_answer", ""), r["facts"]["last_check_matched"],
                        r["status"], r["facts"]["tool_calls"],
                        r["facts"]["check_answer_calls"], r["facts"]["execute_sql_calls"],
                        r["seconds"], r["status_note"]])

    lines = [f"# Agent execution log ({stamp})", "",
             f"Model: `{MODEL}` (reasoning={REASONING}, num_ctx={NUM_CTX})", "",
             "| Question | Final answer | Matched ground truth | Status | Tool calls | Time |",
             "|---|---|---|---|---|---|"]
    for r in results:
        rep = r["report"] or {}
        lines.append(f"| {r['question_id']} | {rep.get('numerical_answer', '')} | "
                     f"{r['facts']['last_check_matched']} | **{r['status']}** | "
                     f"{r['facts']['tool_calls']} | {r['seconds']}s |")
    for r in results:
        rep = r["report"] or {}
        lines += ["", "---", "", f"## {r['question_id']}: {r['question']}", ""]
        lines += ["### Tool calls", ""]
        n = 0
        for e in r["events"]:
            if e["type"] == "tool_call":
                n += 1
                args = e["args"]
                if "sql" in args:
                    lines += [f"**{n}. `{e['tool']}`**"
                              + (f" ({args.get('question_id')})" if "question_id" in args else ""),
                              "", "```sql", args["sql"], "```"]
                else:
                    lines += [f"**{n}. `{e['tool']}`** `{json.dumps(args)}`"]
            elif e["type"] == "tool_result":
                res = e["result"]
                if e["tool"] == "get_schema" and len(res) > 400:
                    res = res[:400] + " ... (truncated in this view; full text in the .jsonl)"
                lines += ["", "Result:", "```json", res, "```", ""]
        lines += ["### Final report", ""]
        if rep:
            lines += ["- **Final SQL:**", "", "```sql", str(rep.get('final_sql', '')), "```", "",
                      f"- **Numerical answer:** {rep.get('numerical_answer')}",
                      f"- **Matched ground truth (from check_answer):** "
                      f"{r['facts']['last_check_matched']}",
                      f"- **Status:** **{r['status']}**"
                      + (f" ({r['status_note']})" if r["status_note"] else ""),
                      "", "**Independent verification:**", ""]
            for v in rep.get("verification", []) or []:
                if isinstance(v, dict):
                    lines += [f"- **{v.get('check', '')}**", "", "  ```sql",
                              f"  {v.get('sql', '')}", "  ```", "",
                              f"  Result: {v.get('result', '')}. *{v.get('outcome', '')}*", ""]
                else:
                    lines.append(f"- {v}")
            lines += ["", f"**Conclusion:** {rep.get('conclusion', '')}"]
        else:
            lines += [f"Status: **{r['status']}** ({r['status_note'] or 'no JSON report'})",
                      "", "Raw final message:", "", "```", r["final_text"] or "(none)", "```"]
    (LOG_DIR / f"run_{stamp}_report.md").write_text("\n".join(lines) + "\n")


def main():
    qids = [q.upper() for q in sys.argv[1:]] or QUESTIONS
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"Model {MODEL} | questions {qids}")
    agent = build_agent()
    results = []
    for qid in qids:
        results.append(run_question(agent, qid))
        write_logs(results, stamp)          # save after every question
    print(f"\nLogs written to {LOG_DIR}/ (run_{stamp}.*, summary_{stamp}.csv)")


if __name__ == "__main__":
    main()
