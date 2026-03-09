from agno.agent import Agent
from agno.models.openai import OpenAIChat
from pydantic import BaseModel, Field
from typing import List
from dotenv import load_dotenv
import os

load_dotenv()
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")


# ─── Response Models ──────────────────────────────────────────────────────────

class DDLRecommendation(BaseModel):
    type: str = Field(description="Either 'index' or 'materialized_view'")
    name: str = Field(description="Name for the index or materialized view (snake_case)")
    ddl: str = Field(description="Exact SQL DDL statement to execute")
    reason: str = Field(description="Why this improves performance")


class QueryRewrite(BaseModel):
    original_query: str = Field(description="The original slow query")
    rewritten_query: str = Field(description="The optimized rewritten query")
    explanation: str = Field(description="What changed and why it's faster")


class OptimizationPlan(BaseModel):
    summary: str = Field(description="Brief overall summary of findings")
    ddl_recommendations: List[DDLRecommendation] = Field(
        description="List of indexes and materialized views to create"
    )
    query_rewrites: List[QueryRewrite] = Field(
        description="How affected queries should be rewritten"
    )


# ─── Optimizer Agent ──────────────────────────────────────────────────────────
# Analyzes workload + schema, returns a structured OptimizationPlan.
# No SQLTools needed — all context is passed in the prompt.

optimizer_agent = Agent(
    name="DB Optimizer",
    model=OpenAIChat(id="gpt-4o"),
    output_schema=OptimizationPlan,
    instructions=[
        "You are a senior PostgreSQL database optimization expert.",
        "You will receive: schema info, existing indexes, and top slow queries.",
        "Identify the most impactful indexes and materialized views to create.",
        "Index columns used in WHERE, JOIN ON, ORDER BY, and GROUP BY clauses.",
        "Suggest materialized views only for aggregate queries called many times.",
        "For each DDL recommendation, write the exact executable SQL.",
        "Name indexes as idx_<table>_<columns>, e.g. idx_orders_user_id.",
        "For query rewrites, show concretely how each query benefits.",
        "Only recommend changes that provide clear, measurable improvement.",
    ],
)


# ─── Query Rewriter Agent ─────────────────────────────────────────────────────
# Rewrites a single user-provided query to leverage existing indexes/MVs.

rewriter_agent = Agent(
    name="Query Rewriter",
    model=OpenAIChat(id="gpt-4o"),
    instructions=[
        "You are a PostgreSQL query rewriter.",
        "Given a SQL query, available indexes, and schema info:",
        "1. Rewrite the query to use available indexes and materialized views.",
        "2. Show the rewritten query inside a ```sql code block.",
        "3. Explain specifically what changed and why it performs better.",
        "4. If no rewrite is needed, say so and explain why the query is already optimal.",
        "Keep your response concise and practical.",
    ],
    markdown=True,
)
