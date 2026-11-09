import logging
import math
import os
import re

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from groq import Groq
from dotenv import load_dotenv
import sqlparse
import uvicorn

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# Load environment variables

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DB_URL = os.getenv("DB_URL")

# Validate env vars
if not GROQ_API_KEY or not DB_URL:
    raise ValueError("Missing environment variables: GROQ_API_KEY or DB_URL")


# Database and Groq client setup

engine = create_engine(DB_URL)
client = Groq(api_key=GROQ_API_KEY)


# FastAPI App Configuration

app = FastAPI(title="Chat With Data API")

# Restrict CORS to configured frontend origin(s); default is local dev only.
# Set ALLOWED_ORIGINS as a comma-separated list in production, e.g.
# ALLOWED_ORIGINS=https://analytics.example.com
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request Body Model

class QueryRequest(BaseModel):
    query: str


# Schema Description (for LLM)

SCHEMA_DESCRIPTION = """
                        Table: analytics_data
                        Columns:
                        - vendor_name (text)
                        - invoice_id (text)
                        - invoice_date (text)
                        - sub_total (float)
                        - total_tax (float)
                        - invoice_total (float)
                    """

# Statements / constructs that must never appear in LLM-generated SQL.
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|COPY|PG_SLEEP|INFORMATION_SCHEMA|PG_CATALOG)\b",
    re.IGNORECASE,
)
SQL_COMMENT = re.compile(r"(--|/\*)")


def validate_readonly_select(sql: str) -> str:
    """Validate LLM-generated SQL and return it only if it is a single
    read-only SELECT statement. Raises ValueError otherwise.

    - Strips markdown fences and trailing semicolons.
    - Rejects anything that is not exactly one statement.
    - Rejects non-SELECT statements, forbidden keywords (DROP/DELETE/...
      /INFORMATION_SCHEMA/PG_CATALOG) and SQL comments.
    """
    cleaned = sql.strip()
    # Strip markdown code fences (```sql / ```), wherever they appear.
    cleaned = cleaned.replace("```sql", "").replace("```", "").strip()
    # Strip trailing semicolons (a trailing ';' would otherwise let a second
    # statement hide behind it in some drivers).
    cleaned = cleaned.rstrip(";").strip()
    if not cleaned:
        raise ValueError("Empty SQL query")
    if SQL_COMMENT.search(cleaned):
        raise ValueError("SQL comments are not allowed")
    if FORBIDDEN_KEYWORDS.search(cleaned):
        raise ValueError("Forbidden keyword in SQL query")
    parsed = sqlparse.parse(cleaned)
    if len(parsed) != 1:
        raise ValueError("Only a single SQL statement is allowed")
    if parsed[0].get_type() != "SELECT":
        raise ValueError("Only SELECT statements are allowed")
    return cleaned


# Helper Function

def clean_for_json(df: pd.DataFrame):
    """Convert DataFrame to JSON-safe records."""
    records = df.to_dict(orient="records")
    cleaned = []
    for row in records:
        new_row = {}
        for k, v in row.items():
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                new_row[k] = None
            elif isinstance(v, (pd.Timestamp, pd.Timedelta)):
                new_row[k] = str(v)
            else:
                new_row[k] = v
        cleaned.append(new_row)
    return cleaned


# Main API Route

@app.post("/chat-with-data")
async def chat_with_data(req: QueryRequest):
    user_query = req.query

    try:
        # Step 1 — Create LLM prompt
        prompt = f"""
                   You are an expert PostgreSQL assistant.
                   Given this database schema:
                   {SCHEMA_DESCRIPTION}
                   Generate a single valid read-only SELECT query
                   (no markdown formatting, no explanations, no comments)
                   for the user request: "{user_query}"
                   """

        # Step 2 — Generate SQL using Groq LLM
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
        )

        raw_sql = response.choices[0].message.content.strip()

        # Step 3 — Validate: single read-only SELECT only. Anything else is
        # rejected before it can reach the database.
        sql_query = validate_readonly_select(raw_sql)

        # Step 4 — Ensure proper date casting
        if "invoice_date" in sql_query and "::date" not in sql_query:
            sql_query = sql_query.replace("invoice_date", "invoice_date::date")
            # Re-validate after mutation so the cast cannot smuggle anything in.
            sql_query = validate_readonly_select(sql_query)

        # Step 5 — Execute SQL query
        with engine.connect() as conn:
            df = pd.read_sql(text(sql_query), conn)

        # Step 6 — Clean and return results
        cleaned = clean_for_json(df)
        return {"generated_sql": sql_query, "results": cleaned}

    except ValueError as exc:
        # Validation rejection — safe to describe generically to the client.
        logger.warning("Rejected LLM-generated SQL: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="Query execution failed or rejected by read-only safety policy.",
        ) from exc
    except Exception:
        logger.exception("Chat-with-data request failed")
        raise HTTPException(
            status_code=400,
            detail="Query execution failed or rejected by read-only safety policy.",
        ) from None


# Run Server

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
