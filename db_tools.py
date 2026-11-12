"""
Database tools the agent can call.

    get_schema    - describe the soccerhub tables/columns (plus sample rows)
    execute_sql   - run a READ-ONLY query and return rows or the error
    check_answer  - run a candidate "main" query and say whether its value
                    matches the HW2 ground truth (the expected value itself
                    is never shown to the agent)

Read-only is enforced three ways:
    1. the SQL text is checked before it runs (single SELECT-type statement)
    2. every session is put in READ ONLY transaction mode
    3. the DB account the agent uses (agent_ro) only has SELECT privileges
"""

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from langchain_core.tools import tool
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_USER = os.getenv("DB_USER", "agent_ro")
DB_PASS = os.getenv("DB_PASS", "")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "soccerhub")

EXPECTED_PATH = ROOT / "data" / "expected.jsonl"
MAX_MAIN_ATTEMPTS = 5       # assignment: max 5 tries at the main SQL per question
MAX_ROWS_RETURNED = 25      # keep tool output small so it fits in the context
QUERY_TIMEOUT_MS = 10_000
FLOAT_EPS = 1e-6

engine = create_engine(
    f"mysql+mysqlconnector://{DB_USER}:{quote_plus(DB_PASS)}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}",
    pool_pre_ping=True,
)


# ---------------- ground truth ----------------
def _load_expected():
    """qid -> gold value, using the HW2 gold SQL as the source of truth."""
    gold = {}
    with open(EXPECTED_PATH) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                gold[f"Q{row['qid']}"] = row
    return gold


EXPECTED = _load_expected()

# attempts used per question; reset by the runner before each question
attempts = {}


def reset_attempts():
    attempts.clear()


# ---------------- read-only guard ----------------
ALLOWED_START = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.I)
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|MERGE|DROP|ALTER|CREATE|TRUNCATE|RENAME|"
    r"GRANT|REVOKE|LOCK|UNLOCK|CALL|LOAD|HANDLER|SET|COMMIT|ROLLBACK|"
    r"OUTFILE|DUMPFILE|SLEEP|BENCHMARK)\b",
    re.I,
)


def _strip_strings(sql):
    """Remove quoted literals so a word inside a string (e.g. 'Real Madrid')
    can't trip the keyword check."""
    return re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", sql)


def check_read_only(sql):
    """Return an error message if the SQL isn't a single read-only statement."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return "empty query"
    bare = _strip_strings(s)
    if ";" in bare:
        return "only one statement is allowed"
    if not ALLOWED_START.match(bare):
        return "only read-only queries (SELECT / WITH / SHOW / DESCRIBE / EXPLAIN) are allowed"
    bad = FORBIDDEN.search(bare)
    if bad:
        return f"keyword '{bad.group(1).upper()}' is not allowed (read-only access)"
    return None


def _to_jsonable(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def run_query(sql):
    """Run one read-only query. Returns (columns, rows) or raises."""
    problem = check_read_only(sql)
    if problem:
        raise PermissionError(problem)
    with engine.connect() as conn:
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        for stmt in (f"SET SESSION max_execution_time = {QUERY_TIMEOUT_MS}",       # MySQL
                     f"SET SESSION max_statement_time = {QUERY_TIMEOUT_MS / 1000}"):  # MariaDB
            try:
                conn.exec_driver_sql(stmt)
            except Exception:
                pass
        result = conn.execute(text(sql.strip().rstrip(";")))
        cols = list(result.keys())
        rows = [[_to_jsonable(v) for v in r] for r in result.fetchall()]
        conn.rollback()
    return cols, rows


def _same_number(a, b):
    try:
        return abs(float(a) - float(b)) <= FLOAT_EPS
    except (TypeError, ValueError):
        return False


# ---------------- tools ----------------
@tool
def get_schema(table_name: str = "") -> str:
    """Describe the soccerhub database.
    With no table_name: list every table with its columns, types, nullability,
    primary key, foreign keys and row count.
    With a table_name: the same details for that table plus 3 sample rows."""
    insp = inspect(engine)
    tables = insp.get_table_names()
    if table_name:
        match = [t for t in tables if t.lower() == table_name.strip("` ").lower()]
        if not match:
            return json.dumps({"error": f"no table named {table_name}", "tables": tables})
        tables = match

    out = {"database": DB_NAME,
           "note": "Match is a reserved word in MySQL; write the table as `Match`. "
                   "Table names are case-sensitive: use them exactly as listed.",
           "tables": []}
    for t in tables:
        info = {
            "table": t,
            "columns": [
                {"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
                for c in insp.get_columns(t)
            ],
            "primary_key": insp.get_pk_constraint(t).get("constrained_columns", []),
            "foreign_keys": [
                f"{','.join(fk['constrained_columns'])} -> "
                f"{fk['referred_table']}({','.join(fk['referred_columns'])})"
                for fk in insp.get_foreign_keys(t)
            ],
        }
        _, cnt = run_query(f"SELECT COUNT(*) FROM `{t}`")
        info["row_count"] = cnt[0][0]
        if table_name:
            cols, rows = run_query(f"SELECT * FROM `{t}` LIMIT 3")
            info["sample_rows"] = [dict(zip(cols, r)) for r in rows]
        out["tables"].append(info)
    return json.dumps(out, default=str)


@tool
def execute_sql(sql: str) -> str:
    """Run ONE read-only MySQL query (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN) against
    soccerhub and return the columns, up to 25 rows, and the total row count,
    or the error message if the query fails. Use this to explore the data and
    to run independent verification queries. It does NOT count as an attempt."""
    try:
        cols, rows = run_query(sql)
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        return json.dumps({"error": msg[:400]})
    return json.dumps({
        "columns": cols,
        "rows": rows[:MAX_ROWS_RETURNED],
        "row_count": len(rows),
        "truncated": len(rows) > MAX_ROWS_RETURNED,
    }, default=str)


@tool
def check_answer(question_id: str, sql: str) -> str:
    """Submit a candidate MAIN query for a question (e.g. question_id="Q6").
    The query is executed and its single numeric result is compared with the
    ground truth. Returns whether it matches, never the expected value.
    Each call uses one of the 5 attempts allowed per question."""
    qid = question_id.strip().upper()
    if not qid.startswith("Q"):
        qid = "Q" + qid
    if qid not in EXPECTED:
        return json.dumps({"error": f"unknown question_id {question_id}",
                           "valid_ids": sorted(EXPECTED)})

    used = attempts.get(qid, 0)
    if used >= MAX_MAIN_ATTEMPTS:
        return json.dumps({
            "error": f"attempt limit reached ({MAX_MAIN_ATTEMPTS}) for {qid}. "
                     "No more checks allowed; if confidence is not established, "
                     "report this question as Unresolved."})
    attempts[qid] = used + 1
    left = MAX_MAIN_ATTEMPTS - attempts[qid]

    try:
        cols, rows = run_query(sql)
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        return json.dumps({"attempt": attempts[qid], "attempts_left": left,
                           "matches_ground_truth": False,
                           "error": f"query failed: {msg[:300]}"})

    if len(rows) != 1 or len(cols) != 1:
        return json.dumps({
            "attempt": attempts[qid], "attempts_left": left,
            "matches_ground_truth": False,
            "result_shape": f"{len(rows)} row(s) x {len(cols)} column(s)",
            "note": "the main query must return exactly one row with one numeric value"})

    value = rows[0][0]
    gold_cols, gold_rows = run_query(EXPECTED[qid]["gold_sql"])
    return json.dumps({
        "attempt": attempts[qid],
        "attempts_left": left,
        "result_value": value,
        "matches_ground_truth": _same_number(value, gold_rows[0][0]),
    }, default=str)


ALL_TOOLS = [get_schema, execute_sql, check_answer]
