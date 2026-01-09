import streamlit as st
from agno.agent import Agent
from agno.tools.sql import SQLTools
from agno.models.openai import OpenAIChat
from dotenv import load_dotenv
import os
import re
load_dotenv()

os.environ['GOOGLE_API_KEY'] = os.getenv('GOOGLE_API_KEY')
os.environ['OPENAI_API_KEY'] = os.getenv('OPENAI_API_KEY')

db_url = "postgresql+psycopg://ai_user:secret@localhost:5432/unoptimized_db"

# Helper function to extract optimized SQL from agent output
def extract_optimized_sql(text: str) -> str:
    """Extract the optimized SQL query from the agent's output text."""
    # Try multiple strategies to find the optimized SQL
    
    # Strategy 1: Look for fenced code blocks with sql
    fence_pattern = r"```sql\s*(.*?)\s*```"
    matches = re.findall(fence_pattern, text, re.DOTALL | re.IGNORECASE)
    if matches:
        # Return the last SQL block (usually the optimized one)
        return matches[-1].strip()
    
    # Strategy 2: Look for "Optimized Query:" header
    opt_patterns = [
        r"Optimized Query:\s*(SELECT.*?)(?:\n\n|Explanation|Recommended)",
        r"Optimized SQL:\s*(SELECT.*?)(?:\n\n|Explanation|Recommended)",
        r"(?:Optimized Query|Optimized SQL):\s*(SELECT.*?)(?:\n\n|\Z)",
    ]
    
    for pattern in opt_patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    # Strategy 3: Find any SELECT statement that looks complete
    select_pattern = r"(SELECT\s+.+?FROM.+?;)"
    matches = re.findall(select_pattern, text, re.DOTALL | re.IGNORECASE)
    if matches:
        # Return the longest one (usually more complete)
        return max(matches, key=len).strip()
    
    # Fallback: return empty string
    return ""

# SQL Optimizer Agent
optimizer_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a database optimization expert",
        "Use list_tables and describe_table to understand the actual database schema",
        "Analyze the provided unoptimized SQL query against the live schema",
        "Identify performance issues (missing indexes, inefficient joins, SELECT *, etc.)",
        "Provide an optimized version of the query",
        "Explain each optimization with detailed reasoning",
        "Suggest indexes, query rewrites, or schema improvements if needed"
    ],
    markdown=True,
)

# SQL Executor Agent
executor_agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[SQLTools(db_url=db_url)],
    instructions=[
        "You are a SQL execution assistant",
        "IMPORTANT: You MUST execute the SQL query using the run_sql_query tool",
        "DO NOT just describe what the query does - actually execute it and return the results",
        "After executing, format the results as a table or list",
        "Show the actual data rows returned by the query",
        "If the query returns no rows, say 'No results found'",
        "If query fails, show the error message"
    ],
    markdown=True,
)

# Streamlit UI
st.set_page_config(page_title="SQL Optimizer", page_icon="🔧", layout="wide")

# Custom CSS for better UI/UX
st.markdown("""
<style>
    /* Main background and text colors */
    .stApp {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    }
    
    /* Content container with better contrast */
    .main .block-container {
        background-color: #ffffff;
        padding: 2rem 3rem;
        border-radius: 15px;
        box-shadow: 0 10px 40px rgba(0,0,0,0.1);
    }
    
    /* Headers */
    h1 {
        color: #1a1a2e !important;
        font-weight: 700 !important;
        margin-bottom: 0.5rem !important;
    }
    
    h2, h3, h4 {
        color: #2d3748 !important;
        font-weight: 600 !important;
    }
    
    /* All paragraph text */
    .stMarkdown p, .stMarkdown li, .stMarkdown span {
        color: #2d3748 !important;
        font-size: 1rem !important;
    }
    
    /* Ensure strong/bold text is visible */
    .stMarkdown strong, .stMarkdown b {
        color: #1a202c !important;
        font-weight: 700 !important;
    }
    
    /* Caption text */
    .stCaption, .caption {
        color: #4a5568 !important;
        font-size: 0.875rem !important;
    }
    
    /* Text areas with better contrast */
    .stTextArea textarea {
        background-color: #f7fafc !important;
        border: 2px solid #cbd5e0 !important;
        color: #1a202c !important;
        font-size: 14px !important;
        border-radius: 8px !important;
        font-weight: 500 !important;
    }
    
    .stTextArea textarea::placeholder {
        color: #a0aec0 !important;
    }
    
    .stTextArea textarea:focus {
        border-color: #667eea !important;
        box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1) !important;
        background-color: #ffffff !important;
    }
    
    .stTextArea label {
        color: #1a202c !important;
        font-weight: 600 !important;
        font-size: 1rem !important;
    }
    
    /* Text input fields */
    .stTextInput input {
        background-color: #f7fafc !important;
        border: 2px solid #cbd5e0 !important;
        color: #1a202c !important;
        border-radius: 8px !important;
    }
    
    .stTextInput label {
        color: #1a202c !important;
        font-weight: 600 !important;
    }
    
    /* Buttons styling */
    .stButton button {
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 16px !important;
        padding: 0.6rem 1.5rem !important;
        transition: all 0.3s ease !important;
    }
    
    .stButton button[kind="primary"] {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
        border: none !important;
        color: #ffffff !important;
    }
    
    .stButton button[kind="primary"]:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4) !important;
    }
    
    .stButton button[kind="secondary"] {
        background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%) !important;
        border: none !important;
        color: #ffffff !important;
    }
    
    .stButton button[kind="secondary"]:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 5px 20px rgba(245, 87, 108, 0.4) !important;
    }
    
    /* Info boxes with better contrast */
    .stAlert, [data-testid="stNotification"] {
        background-color: #e6f2ff !important;
        border: 1px solid #4299e1 !important;
        border-left: 4px solid #3182ce !important;
        color: #1a365d !important;
        border-radius: 8px !important;
        padding: 1rem !important;
    }
    
    .stAlert p, [data-testid="stNotification"] p {
        color: #1a365d !important;
    }
    
    /* Warning boxes */
    [data-testid="stNotificationWarning"], .stWarning {
        background-color: #fffbeb !important;
        border: 1px solid #f59e0b !important;
        border-left: 4px solid #d97706 !important;
        color: #78350f !important;
    }
    
    /* Success boxes */
    [data-testid="stNotificationSuccess"], .stSuccess {
        background-color: #ecfdf5 !important;
        border: 1px solid #10b981 !important;
        border-left: 4px solid #059669 !important;
        color: #064e3b !important;
    }
    
    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: #f1f5f9;
        padding: 8px;
        border-radius: 10px;
    }
    
    .stTabs [data-baseweb="tab"] {
        background-color: #ffffff;
        color: #1e293b !important;
        font-weight: 600;
        border-radius: 8px;
        padding: 10px 20px;
        border: 1px solid #e2e8f0;
    }
    
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
        color: #ffffff !important;
        border: none !important;
    }
    
    /* Code blocks with maximum specificity */
    .stCodeBlock {
        background-color: #1e293b !important;
        border-radius: 8px !important;
        border: 1px solid #475569 !important;
    }
    
    .stCodeBlock pre {
        background-color: #1e293b !important;
        border-radius: 8px !important;
        padding: 1rem !important;
        margin: 0 !important;
    }
    
    /* Force white text in ALL code blocks - override everything */
    .stCodeBlock pre code,
    .stCodeBlock code,
    .stCodeBlock pre span,
    .stCodeBlock span,
    div[data-testid="stCodeBlock"] pre code,
    div[data-testid="stCodeBlock"] code,
    div[data-testid="stCodeBlock"] pre span,
    div[data-testid="stCodeBlock"] span,
    div[data-testid="stCodeBlock"] pre *,
    .element-container .stCodeBlock pre code,
    .element-container .stCodeBlock code,
    .element-container .stCodeBlock pre span,
    .element-container .stCodeBlock span {
        color: #ffffff !important;
        background-color: transparent !important;
        padding: 0 !important;
        border-radius: 0 !important;
        font-family: 'Consolas', 'Monaco', monospace !important;
        font-weight: 500 !important;
    }
    
    /* Inline code (not in blocks) */
    p code:not(.stCodeBlock code), 
    li code:not(.stCodeBlock code),
    span code:not(.stCodeBlock code) {
        color: #1e293b !important;
        background-color: #fce7f3 !important;
        padding: 2px 6px !important;
        border-radius: 4px !important;
        font-family: 'Consolas', 'Monaco', monospace !important;
        font-weight: 600 !important;
    }
    
    /* General pre blocks */
    pre:not(.stCodeBlock pre) {
        background-color: #1e293b !important;
        border-radius: 8px !important;
        padding: 1rem !important;
    }
    
    pre:not(.stCodeBlock pre) code {
        color: #ffffff !important;
    }
    
    /* Sidebar styling with maximum contrast */
    .css-1d391kg, [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%) !important;
    }
    
    .css-1d391kg .stMarkdown p, 
    [data-testid="stSidebar"] .stMarkdown p,
    .css-1d391kg .stMarkdown li,
    [data-testid="stSidebar"] .stMarkdown li,
    .css-1d391kg .stMarkdown span,
    [data-testid="stSidebar"] .stMarkdown span {
        color: #f1f5f9 !important;
    }
    
    .css-1d391kg .stMarkdown strong,
    [data-testid="stSidebar"] .stMarkdown strong {
        color: #ffffff !important;
        font-weight: 700 !important;
    }
    
    .css-1d391kg h3, [data-testid="stSidebar"] h3,
    .css-1d391kg h2, [data-testid="stSidebar"] h2,
    .css-1d391kg h4, [data-testid="stSidebar"] h4 {
        color: #ffffff !important;
        font-weight: 700 !important;
    }
    
    /* Sidebar info box */
    [data-testid="stSidebar"] .stAlert {
        background-color: #334155 !important;
        border-left: 4px solid #60a5fa !important;
        border: 1px solid #475569 !important;
    }
    
    [data-testid="stSidebar"] .stAlert p {
        color: #e0e7ff !important;
    }
    
    /* Sidebar text inputs */
    [data-testid="stSidebar"] .stTextArea textarea {
        background-color: #334155 !important;
        border: 1px solid #475569 !important;
        color: #f1f5f9 !important;
    }
    
    [data-testid="stSidebar"] .stTextArea label {
        color: #f1f5f9 !important;
    }
    
    /* Sidebar captions */
    [data-testid="stSidebar"] .stCaption {
        color: #cbd5e1 !important;
    }
    
    /* Expander */
    .streamlit-expanderHeader {
        background-color: #f8fafc !important;
        border: 1px solid #e2e8f0 !important;
        border-radius: 8px !important;
        color: #1e293b !important;
        font-weight: 600 !important;
    }
    
    .streamlit-expanderContent {
        background-color: #ffffff !important;
        border: 1px solid #e2e8f0 !important;
        border-top: none !important;
    }
    
    /* Sidebar expander */
    [data-testid="stSidebar"] .streamlit-expanderHeader {
        background-color: #334155 !important;
        border: 1px solid #475569 !important;
        color: #f1f5f9 !important;
    }
    
    [data-testid="stSidebar"] .streamlit-expanderContent {
        background-color: #1e293b !important;
        border: 1px solid #475569 !important;
    }
    
    /* Divider */
    hr {
        margin: 2rem 0 !important;
        border-color: #e2e8f0 !important;
    }
    
    /* Spinner */
    .stSpinner > div {
        border-top-color: #667eea !important;
    }
    
    /* Ensure all text in containers is visible - but NOT in code blocks */
    .element-container p:not(.stCodeBlock p), 
    .element-container span:not(.stCodeBlock span):not(.stCodeBlock pre span), 
    .element-container li:not(.stCodeBlock li) {
        color: #2d3748 !important;
    }
    
    /* Override for code blocks specifically */
    .element-container .stCodeBlock,
    .element-container .stCodeBlock *,
    .element-container div[data-testid="stCodeBlock"],
    .element-container div[data-testid="stCodeBlock"] * {
        color: #ffffff !important;
    }
    
    /* Results section text visibility */
    div[data-testid="stMarkdownContainer"] p,
    div[data-testid="stMarkdownContainer"] li,
    div[data-testid="stMarkdownContainer"] span,
    div[data-testid="stMarkdownContainer"] td,
    div[data-testid="stMarkdownContainer"] th,
    div[data-testid="stMarkdownContainer"] strong,
    .stMarkdown table,
    .stMarkdown table td,
    .stMarkdown table th,
    .stMarkdown table *:not(code) {
        color: #2d3748 !important;
    }
    
    /* Ensure markdown tables are visible */
    .stMarkdown table {
        background-color: #ffffff !important;
        border: 1px solid #e2e8f0 !important;
    }
    
    .stMarkdown table td,
    .stMarkdown table th {
        border: 1px solid #e2e8f0 !important;
        padding: 8px 12px !important;
        background-color: #ffffff !important;
    }
    
    .stMarkdown table th {
        background-color: #f8fafc !important;
        font-weight: 600 !important;
    }
</style>
""", unsafe_allow_html=True)

st.title("🔧 SQL Query Optimizer & Executor")
st.markdown("Analyze and optimize your SQL queries with live database schema inspection")

st.markdown("---")

# Input section with better layout
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📝 Query Description")
    task = st.text_area(
        "What are you trying to achieve?",
        placeholder="e.g., Get the most sold product by quantity, Find users who spent more than $500...",
        height=120,
        help="Describe what data you want to retrieve from the database"
    )

with col_right:
    st.subheader("💡 How It Works")
    st.info("""
    **🔍 Schema Inspection**  
    Analyzes your live database tables and indexes
    
    **⚡ Performance Analysis**  
    Identifies bottlenecks and inefficiencies
    
    **🚀 Query Optimization**  
    Suggests optimized SQL with explanations
    
    **📊 Direct Execution**  
    Run queries and see results instantly
    """)

# Full width SQL input
st.markdown("### 📄 Your SQL Query")
unoptimized_sql = st.text_area(
    "Paste your SQL query here:",
    placeholder="SELECT * FROM users WHERE ...",
    height=200,
    help="Paste the SQL query you want to optimize and analyze",
    label_visibility="collapsed"
)

st.markdown("---")

# Action buttons
col1, col2 = st.columns([1, 1])

with col1:
    optimize_btn = st.button("🚀 Optimize Query", type="primary", use_container_width=True)

with col2:
    execute_btn = st.button("▶️ Execute Optimized Query", type="secondary", use_container_width=True)

st.markdown("---")

# Handle Optimization
if optimize_btn:
    if task and unoptimized_sql:
        # Clear previous execution results when optimizing
        if 'execution_result' in st.session_state:
            del st.session_state['execution_result']
        
        with st.spinner("🔍 Analyzing schema and optimizing query..."):
            prompt = f"""
            Task: {task}
            
            Unoptimized SQL Query:
            ```sql
            {unoptimized_sql}
            ```
            
            Please:
            1. Inspect the actual database schema using list_tables and describe_table
            2. Analyze the query against the real schema
            3. if the table name and columns are wrong then write ur own sql query based on correct info
            3. Identify all performance issues
            4. Provide an optimized version of the query
            5. Explain each optimization with reasoning
            6. Suggest any additional indexes or schema changes
            7. Dont include any sql for index creation and stuff
            """
            
            response = optimizer_agent.run(prompt)
            st.session_state['optimization_result'] = response.content
            
            # Extract the optimized SQL from the agent's response
            optimized = extract_optimized_sql(response.content)
            if optimized:
                st.session_state['optimized_query'] = optimized
            else:
                st.session_state['optimized_query'] = unoptimized_sql
    else:
        st.warning("⚠️ Please provide both a task description and SQL query")

# Handle Execution
if execute_btn:
    if 'optimized_query' in st.session_state and st.session_state['optimized_query']:
        with st.spinner("⚡ Executing optimized query..."):
            execute_prompt = f"""
            Use the run_sql_query tool to execute this SQL query and return the actual data results:
            
            ```sql
            {st.session_state['optimized_query']}
            ```
            
            IMPORTANT: 
            - You MUST call run_sql_query tool to execute this query
            - Return the actual data rows from the database
            - Format the results as a markdown table showing all columns and rows
            - Do NOT just describe what the query does
            """
            
            response = executor_agent.run(execute_prompt)
            st.session_state['execution_result'] = response.content
            # Mark that we just executed
            st.session_state['just_executed'] = True
    else:
        st.warning("⚠️ Please optimize the query first")

# Display results
# Show execution results if they exist
if 'execution_result' in st.session_state:
    st.markdown("### ✅ Query Execution Results")
    st.markdown("---")
    
    # Show the SQL that was executed in a styled container
    with st.expander("📋 View Executed SQL Query", expanded=False):
        st.code(st.session_state.get('optimized_query', ''), language='sql')
    
    # Results container with better styling
    st.markdown("#### 📊 Data Output")
    with st.container():
        st.markdown(st.session_state['execution_result'])
    
    st.markdown("---")

# Show optimization results if they exist
if 'optimization_result' in st.session_state:
    st.markdown("### 📊 Optimization Analysis")
    st.markdown("---")
    
    # Create tabs for better organization
    tab1, tab2 = st.tabs(["📝 Analysis & Recommendations", "💻 Optimized SQL Code"])
    
    with tab1:
        st.markdown("#### 🔍 Performance Insights")
        st.markdown(st.session_state['optimization_result'])
    
    with tab2:
        st.markdown("#### ✏️ Review and Edit SQL")
        st.caption("The optimized SQL was automatically extracted. You can modify it before execution.")
        optimized_query_display = st.text_area(
            "Optimized SQL Query",
            value=st.session_state.get('optimized_query', ''),
            height=280,
            key='optimized_display',
            help="Edit the optimized SQL if needed, then click 'Execute Optimized Query' above",
            label_visibility="collapsed"
        )
        # Update session state if user edits the query
        st.session_state['optimized_query'] = optimized_query_display
        
        st.info("💡 **Tip:** Click the 'Execute Optimized Query' button above to run this SQL")

# Sidebar
with st.sidebar:
    st.markdown("### 💡 How It Works")
    st.markdown("""
    **1️⃣ Describe Your Goal**  
    Explain what data you need
    
    **2️⃣ Paste Your SQL**  
    Add your current query
    
    **3️⃣ Click Optimize**  
    AI analyzes and improves it
    
    **4️⃣ Execute & View**  
    Run and see live results
    """)
    
    st.markdown("---")
    
    st.markdown("### ⚙️ Key Features")
    st.markdown("""
    ✅ **Live Schema Analysis**  
    Real-time DB inspection
    
    ✅ **Smart Optimization**  
    AI-powered improvements
    
    ✅ **Index Suggestions**  
    Performance recommendations
    
    ✅ **Query Execution**  
    Instant result preview
    
    ✅ **Error Detection**  
    Syntax & logic validation
    """)
    
    st.markdown("---")
    
    st.markdown("### 📊 Database Info")
    st.info(f"""
    **Database:** `unoptimized_db`  
    **Type:** PostgreSQL  
    **Status:** 🟢 Connected
    """)
    
    st.markdown("---")
    
    # Manual query execution
    with st.expander("🔧 Advanced: Manual Execution", expanded=False):
        st.caption("Run any SQL query directly")
        manual_query = st.text_area(
            "SQL Query:", 
            height=120, 
            key='manual_query_input',
            placeholder="SELECT * FROM table_name..."
        )
        if st.button("▶️ Execute", use_container_width=True, key='manual_exec_btn'):
            if manual_query:
                with st.spinner("Running query..."):
                    response = executor_agent.run(f"Execute: ```sql\n{manual_query}\n```")
                    st.session_state['manual_result'] = response.content
        
        if 'manual_result' in st.session_state:
            st.markdown("**Result:**")
            st.code(st.session_state['manual_result'])