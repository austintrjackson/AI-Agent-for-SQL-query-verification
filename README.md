# SQL Verification Agent (CS 4354, Homework 3)

An AI agent that writes SQL for natural-language questions, runs it against MySQL, and then **investigates on its own whether the SQL is actually right**, not just whether it returned the right number.

Built with **LangChain `create_agent()`**, **gpt-oss:20b** running on **Ollama**, **SQLAlchemy**, and **MySQL**, on the `soccerhub` database from Homework 2.

> **Why this matters:** a query can return the expected number while its logic is wrong. That can happen by coincidence, through double counting, or because a filter only works on this particular data. Matching the ground truth is necessary, but it isn't proof. This agent has to gather independent evidence before it calls an answer **Verified**.

---

## How it differs from Homework 2 Part 2

| Homework 2 Part 2 (fixed pipeline) | Homework 3 (agent) |
|---|---|
| Python decides every step: prompt → SQL → run → compare | The **model decides** which tool to call next based on what the last result showed |
| One shot per question | Up to **5 attempts** at the main query, revising after failures |
| Correct = number matches | Correct = number matches **and** independent checks support the logic |
| No look at the data or schema | Can inspect the schema, sample rows, and run its own investigation queries |

---

## The agent's tools

| Tool | What it does |
|---|---|
| `get_schema(table_name="")` | Lists tables, columns, types, keys, and row counts. For a single table, it also returns 3 sample rows. |
| `execute_sql(sql)` | Runs one **read-only** query and returns the rows (up to 25) or the error. The agent uses it to explore and verify. It does not count as an attempt. |
| `check_answer(question_id, sql)` | Runs a candidate **main** query and reports whether its single value matches the ground truth. **It never reveals the expected value.** Each call uses one of the 5 attempts. |

All three are registered with LangChain's `@tool` decorator in [`src/db_tools.py`](src/db_tools.py).

### Read-only, enforced in three layers
1. **SQL text check:** only a single `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` statement is allowed. Write keywords, multiple statements, `INTO OUTFILE`, and `SLEEP` are rejected. Text inside quotes is ignored, so `WHERE name = 'DROP TABLE'` is still allowed.
2. **Read-only session:** every connection runs `SET SESSION TRANSACTION READ ONLY`.
3. **Read-only account:** the agent logs in as `agent_ro`, which only has `SELECT` privileges.

### Guardrails (deterministic Python, no SQL decisions)
- **5-attempt limit** per question, enforced inside `check_answer`. A 6th attempt is refused, and the agent is told to report **Unresolved**.
- **Status safety net:** if the agent claims **Verified** but its last `check_answer` did not match, the status is recorded as **Unresolved**.
- Python **never** chooses verification queries or fixes SQL. Those decisions are all the model's.

---

## Repository layout

```
├── src/
│   ├── agent.py          # system prompt, create_agent(), run loop, logging
│   └── db_tools.py       # get_schema / execute_sql / check_answer + read-only guard
├── data/
│   └── expected.jsonl    # ground truth from HW2 Part 2 (gold SQL + value)
├── sql/
│   └── soccerhub.sql     # creates and loads the soccerhub database
├── notebooks/
│   ├── HW3_Colab_Standalone.ipynb   # everything in one notebook: upload to Colab and Run all
│   └── HW3_Colab.ipynb              # same steps, but clones this repo first
├── logs/                 # agent execution logs (deliverable)
├── REFLECTION.md         # half-page reflection (deliverable)
├── requirements.txt
└── .env.example          # copy to .env; .env is git-ignored
```

---

## Running it

### Option 1: Google Colab (recommended, free GPU)
`gpt-oss:20b` needs a GPU to run at a usable speed. On a MacBook Air without one, it could not finish a single query in 5 minutes during Homework 2.

1. Open [`notebooks/HW3_Colab_Standalone.ipynb`](notebooks/HW3_Colab_Standalone.ipynb) in Colab (File → Upload notebook). It contains all the code, so nothing else needs to be uploaded.
2. **Runtime → Change runtime type → T4 GPU**.
3. Run the cells top to bottom. The notebook installs MySQL, loads the data, creates the read-only user, installs Ollama and the model, runs the agent, and downloads the logs.

### Option 2: Local machine (needs a GPU or plenty of RAM)
```bash
pip install -r requirements.txt
mysql -u root -p < sql/soccerhub.sql

# create the read-only account the agent uses
mysql -u root -p -e "CREATE USER 'agent_ro'@'%' IDENTIFIED BY 'pick-a-password';
                     GRANT SELECT, SHOW VIEW ON soccerhub.* TO 'agent_ro'@'%';"

cp .env.example .env        # then put the password in .env
ollama pull gpt-oss:20b
python src/agent.py         # all three questions
python src/agent.py Q6      # or just one
```

### Outputs (written to `logs/`)
| File | Contents |
|---|---|
| `run_<time>.jsonl` | Every step: model reasoning, tool calls with arguments, tool results, and the final report |
| `run_<time>_report.md` | Readable transcript and final report for each question |
| `summary_<time>.csv` | One row per question: final SQL, answer, match, status, and tool-call counts |

---

## Questions

| ID | Question | Why it's tricky |
|---|---|---|
| Q4 | How many matches have not yet been played (both scores not recorded)? | `= NULL` never matches, so the query needs `IS NULL` on **both** columns |
| Q6 | How many players have participated in two or more appearances? | `GROUP BY … HAVING` returns one row per player, so the groups have to be counted in an outer query |
| Q7 | Total goals scored by Real Madrid across all matches (home plus away)? | Has to count home goals when Real Madrid is home and away goals when it is away, without double counting through joins |

---

## Results

Run on Google Colab (T4 GPU) with `gpt-oss:20b`, reasoning=medium, num_ctx=8192. Full transcript: [`logs/run_20261112_185622_report.md`](logs/run_20261112_185622_report.md).

| Question | Final answer | Matched ground truth | Independent verification | Status |
|---|---|---|---|---|
| Q4 | 1 | Yes (attempt 1) | Listed all matches; searched for matches with only one NULL score (none) | **Verified** |
| Q6 | 10 | Yes (attempt 1) | Viewed per-player appearance counts; compared with 14 total players | **Verified** |
| Q7 | 3 | Yes (attempt 1) | Rejected its own first query (returned 7); summed home and away separately; listed Real Madrid's matches | **Verified** |

---

**Final SQL**

```sql
-- Q4
SELECT COUNT(*) AS unplayed FROM `Match` WHERE homeGoals IS NULL AND awayGoals IS NULL;

-- Q6
SELECT COUNT(*) AS num_players
FROM (SELECT playerId FROM Appearance GROUP BY playerId HAVING COUNT(*) >= 2) AS sub;

-- Q7
SELECT COALESCE(SUM(CASE WHEN homeTeamId=1 THEN homeGoals END),0)
     + COALESCE(SUM(CASE WHEN awayTeamId=1 THEN awayGoals END),0) AS total_goals
FROM `Match`;
```

**Known limitation:** in its Q6 final report, the agent listed a duplicate-row check that does not appear in the tool log. The answer is still correct, but the agent's summary should be read against the log, not trusted alone. See the reflection.

---

## Reflection

See [`REFLECTION.md`](REFLECTION.md).

---

*Course: CS 4354 Database Systems, Texas Tech University, Fall 2026. Author: Austin Jackson.*
