from flask import Flask, render_template, request, jsonify
import sqlite3
import pandas as pd
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
from langchain_core.messages import AIMessage
import operator
import threading
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

app = Flask(__name__)

# === Setup ===
llm = ChatOllama(model="mistral:latest", temperature=0)

def load_schema():
    logging.info("Starting load_schema function")
    try:
        with open("db.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Schema file not found"

def load_column_descriptions():
    logging.info("Starting load_column_descriptions function")
    try:
        with open("column_descriptions.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Description file not found"

SCHEMA = load_schema()
DESCRIPTIONS = load_column_descriptions()

# === State Definition ===
class AgentState(TypedDict):
    user_query: str
    sql_query: str
    query_result: str
    error_message: str
    error_explanation: str
    summary: str
    messages: Annotated[list, operator.add]
    retry_count: int

# === Agent 1: SQL Generator ===
def sql_generator_agent(state: AgentState):
    logging.info("Starting sql_generator_agent function")
    """Generates SQL using proven hospital data governance rules - Returns ONLY SQL query"""
    
    # Check if this is a retry attempt with error explanation
    if state.get("error_explanation"):
        prompt = f"""
You are a senior SQL developer specializing in hospital management systems.
The database of the hospital management system is a SQLite database.

Your previous SQL query failed with this error explanation:
{state["error_explanation"]}

Previous failed query: {state["sql_query"]}

Now generate a corrected SQL query based on the error feedback.

{SCHEMA}

**Very Important Rules:**
* Only use the **database name and table name** exactly as defined in the schema.
* Tailor the SQL query to align precisely with the intent of the user's question.
* The SQL must be correct, syntactically valid, and directly executable on the specified table.
* Do not invent columns, values, or make assumptions.
* Return ONLY the SQL query - no explanations, formatting, or comments.

User's original request: "{state['user_query']}"

Return ONLY the corrected SQL query:
"""
    else:
        prompt = f"""
You are a senior SQL developer specializing in hospital management systems.
The database of the hospital management system is a SQLite database.

Your responsibility is to translate medical questions posed by the user into a single accurate SQL query, strictly only using the table named "Patient_Data".

{SCHEMA}

**Very Important Rules:**
* Only use the **database name and table name** exactly as defined in the schema.
* Tailor the SQL query to align precisely with the intent of the user's question.
* The SQL must be correct, syntactically valid, and directly executable on the specified table.
* Do not invent columns, values, or make assumptions.
* Return ONLY the SQL query - no explanations, formatting, or comments.
* If uncertain about which specific columns to select, use `SELECT *` to retrieve all relevant values.

User's request: "{state['user_query']}"

Return ONLY the SQL query:
"""
    
    response = llm.invoke(prompt)
    sql_query = response.content.strip().strip('"').strip("'")
    
    # Clean SQL formatting
    if sql_query.startswith("```sql"):
        sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
    if sql_query.startswith("```"):
        sql_query = sql_query.replace("```", "").strip()
    
    return {
        "sql_query": sql_query,
        "error_explanation": "",  # Clear previous error explanation
        "messages": [AIMessage(content=f"Generated SQL: {sql_query}")]
    }

# === Agent 2: Database Executor ===
def database_executor_agent(state: AgentState):
    logging.info("Starting database_executor_agent function")
    """Executes SQL queries and routes based on success/failure"""
    
    try:
        conn = sqlite3.connect("patient_data.db")
        df = pd.read_sql_query(state["sql_query"], conn)
        conn.close()
        
        if df.empty:
            result = "Query executed successfully, but returned no rows."
        else:
            result = df.head(10).to_string(index=False)
        
        return {
            "query_result": result,
            "error_message": "",
            "messages": [AIMessage(content=f"Query executed successfully. Found {len(df)} records.")]
        }
        
    except Exception as e:
        return {
            "query_result": "",
            "error_message": str(e),
            "messages": [AIMessage(content=f"Database execution failed: {str(e)}")]
        }

# === Agent 3: Error Explainer ===
def error_explainer_agent(state: AgentState):
    logging.info("Starting error_explainer_agent function")
    """Analyzes and explains SQL errors to help generate better queries"""
    
    prompt = f"""
You are a SQL error analysis expert. Analyze the following SQL error and provide a clear explanation to help fix the query.

Failed SQL Query: {state["sql_query"]}
Error Message: {state["error_message"]}
User's Original Question: {state["user_query"]}

Database Schema:
{SCHEMA}

Column Descriptions:
{DESCRIPTIONS}

Analyze the error and provide:
1. What exactly went wrong
2. Why the error occurred
3. What needs to be corrected in the SQL query
4. Specific guidance for generating a correct query

Be specific and technical in your explanation to help generate a corrected SQL query.
"""
    
    response = llm.invoke(prompt)
    error_explanation = response.content.strip()
    
    return {
        "error_explanation": error_explanation,
        "retry_count": state.get("retry_count", 0) + 1,
        "messages": [AIMessage(content="Error analyzed and explanation generated")]
    }

# === Agent 4: Result Summarizer ===
def result_summarizer_agent(state: AgentState):
    logging.info("Starting result_summarizer_agent function")
    """Summarizes successful results using hospital reporting standards"""
    
    prompt = f"""
You are a hospital reporting assistant. You summarize SQL result outputs for medical briefings.

Use column descriptions below:
{DESCRIPTIONS}

Summarize the result below clearly and concisely, avoiding SQL terms. Please do not try to make up new information and very strictly 
stick only to the information that is mentioned in the below content and summarize in a structured way.

Query Results:
{state["query_result"]}

Please make the output very structured and perfect for reading.
"""
    
    response = llm.invoke(prompt)
    summary = response.content.strip()
    
    return {
        "summary": summary,
        "messages": [AIMessage(content="Summary generated successfully")]
    }

# === Routing Logic ===
def route_after_execution(state: AgentState):
    logging.info("Starting route_after_execution function")
    """Route based on execution results"""
    if state["error_message"]:
        return "error_explainer"
    return "result_summarizer"

def route_after_error_explanation(state: AgentState):
    logging.info("Starting route_after_error_explanation function")
    """Route after error explanation - check retry limit"""
    if state.get("retry_count", 0) >= 3:  # Limit retries to prevent infinite loops
        return "result_summarizer"  # Generate final error summary
    return "sql_generator"

# === Build Graph ===
def create_healthcare_agent_graph():
    logging.info("Starting create_healthcare_agent_graph function")
    workflow = StateGraph(AgentState)
    
    # Add agents
    workflow.add_node("sql_generator", sql_generator_agent)
    workflow.add_node("database_executor", database_executor_agent)
    workflow.add_node("error_explainer", error_explainer_agent)
    workflow.add_node("result_summarizer", result_summarizer_agent)
    
    # Define flow
    workflow.set_entry_point("sql_generator")
    workflow.add_edge("sql_generator", "database_executor")
    
    # Conditional routing after execution
    workflow.add_conditional_edges(
        "database_executor",
        route_after_execution,
        {
            "result_summarizer": "result_summarizer",
            "error_explainer": "error_explainer"
        }
    )
    
    # Error explainer routes back to SQL generator or to final summary
    workflow.add_conditional_edges(
        "error_explainer",
        route_after_error_explanation,
        {
            "sql_generator": "sql_generator",
            "result_summarizer": "result_summarizer"
        }
    )
    
    workflow.add_edge("result_summarizer", END)
    
    return workflow.compile()

# === Healthcare AI System ===
def run_healthcare_ai_system(user_question: str):
    logging.info("Starting run_healthcare_ai_system function")
    """Main function to run the optimized multi-agent system"""
    app_graph = create_healthcare_agent_graph()
    
    initial_state = {
        "user_query": user_question,
        "sql_query": "",
        "query_result": "",
        "error_message": "",
        "error_explanation": "",
        "summary": "",
        "messages": [],
        "retry_count": 0
    }
    
    final_state = app_graph.invoke(initial_state)
    
    # Handle case where max retries reached with errors
    if final_state.get("error_message") and not final_state.get("summary"):
        final_state["summary"] = f"Unable to generate a valid SQL query after multiple attempts. Last error: {final_state.get('error_explanation', final_state['error_message'])}"
    
    return {
        "sql_query": final_state['sql_query'],
        "summary": final_state['summary'],
        "success": True if final_state['summary'] and not final_state.get("error_message") else False
    }

# === Flask Routes ===
@app.route('/')
def index():
    logging.info("Starting index route function")
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    logging.info("Starting chat route function")
    try:
        data = request.get_json()
        user_query = data.get('message', '').strip()
        
        if not user_query:
            return jsonify({
                'success': False,
                'response': 'Please enter a valid healthcare question.'
            })
        
        # Process the query using the healthcare AI system
        result = run_healthcare_ai_system(user_query)
        
        return jsonify({
            'success': result['success'],
            'response': result['summary'],
            'sql_query': result['sql_query']
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'response': f'An error occurred while processing your request: {str(e)}'
        })

if __name__ == '__main__':
    logging.info("Starting Flask application")
    app.run(debug=True, host='0.0.0.0', port=5000)