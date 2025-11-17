
`# backend/utils/run_ai_batch.py
import sys
import json
from pathlib import Path
import os
import argparse
import uuid
import io
from contextlib import redirect_stdout
from pymongo import MongoClient, DESCENDING

# --- Import all the setup functions from run_ai ---
# [FIX] Removed the problematic 'get_mongo_params' from this import
from run_ai import (
    CustomPlaceholderError,
    load_config,
    list_all_collections
)

# Change the working directory to the project's root
try:
    os.chdir(Path(__file__).resolve().parents[1])
except Exception:
    print("Warning: Could not change directory. Assuming already in project root.")

# Add the current directory to the system path to allow importing AI.py
sys.path.append(str(Path(__file__).resolve().parent))

try:
    from ai_core.analyst import AIAnalyst
except ImportError:
    print("❌ FATAL: Could not import AIAnalyst. Make sure you are in the correct directory.")
    sys.exit(1)


def load_queries_json(file_path: Path) -> list[str]:
    """
    Loads a JSON file containing a list of query strings.
    """
    if not file_path.exists():
        print(f"❌ FATAL: Input query file not found at {file_path}")
        raise FileNotFoundError(f"Input file missing: {file_path}")
    
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Input JSON must be a simple list of query strings.")
        
        queries = [str(q) for q in data if isinstance(q, str) and q.strip()]
        return queries

def get_latest_log_entry(log_collection, session_id):
    """
    Fetches the most recent log entry from MongoDB for the given session_id.
    """
    try:
        log_entry = log_collection.find_one(
            {"session_id": session_id},
            sort=[("timestamp", DESCENDING)]
        )
        if log_entry:
            # Convert non-serializable BSON types (like ObjectId) to strings
            log_entry["_id"] = str(log_entry["_id"])
            if "timestamp" in log_entry:
                log_entry["timestamp"] = log_entry["timestamp"].isoformat()
            
            return {
                "plan": log_entry.get("plan"),
                "outcome": log_entry.get("outcome"),
                "timings": {
                    "total": log_entry.get("total_time", 0),
                    "planner": log_entry.get("planner_duration", 0),
                    "retrieval": log_entry.get("retrieval_duration", 0),
                    "synth": log_entry.get("synth_duration", 0)
                },
                "plan_hash": log_entry.get("plan_hash")
            }
    except Exception as e:
        print(f"   ⚠️  Warning: Could not fetch log entry from MongoDB: {e}")
    
    return {
        "plan": None,
        "outcome": "ERROR_LOG_FETCH",
        "timings": None,
        "plan_hash": None
    }


def main():
    """
    Initializes the AI Analyst and runs it in batch mode from a JSON file.
    [UPGRADED] Captures full debug logs and detailed metrics for each query.
    """
    # 1) Setup command-line argument parser
    parser = argparse.ArgumentParser(description="Run AI Analyst in batch mode from a JSON query file.")
    parser.add_argument("input_file", type=str, help="Path to the JSON file containing a list of queries.")
    args = parser.parse_args()

    # 2) Load configuration
    config_path = Path("config/config.json")
    try:
        config = load_config(config_path)
    except CustomPlaceholderError:
        return

    # 3) Load the list of queries from the specified JSON file
    try:
        input_path = Path(args.input_file).resolve()
        queries_to_run = load_queries_json(input_path)
    except Exception as e:
        print(f"❌ Error loading query file: {e}")
        return

    # 4) [NEW & FIXED] Connect to MongoDB to fetch logs
    try:
        # --- THIS IS THE FIX ---
        # Get params directly from config, just like AIAnalyst does.
        # This avoids the problematic 'get_mongo_params' function.
        mongo_cfg = config.get("mongodb", {})
        connection_string = mongo_cfg.get("connection_string", "mongodb://localhost:27017/")
        database_name = mongo_cfg.get("database_name", "school_system")
        # --- END OF FIX ---

        client = MongoClient(connection_string)
        db = client[database_name]
        log_collection = db["query_log"]
        print(f"✅ Connected to MongoDB log collection: '{database_name}.query_log'")
    except Exception as e:
        print(f"❌ FATAL: Could not connect to MongoDB: {e}")
        return

    # 5) Initialize the AI Analyst
    execution_mode = config.get("execution_mode", "split")
    collections = list_all_collections(config)
    print("\n🗂️  MongoDB collections to be used:", collections)
    print("\n🚀 Starting AI Analyst for BATCH RUN...")
    
    ai = AIAnalyst(collections=collections, llm_config=config, execution_mode=execution_mode)

    # 6) Create a single, unique session for this entire batch run
    session_id = f"batch_run_{uuid.uuid4()}"
    print(f"🔧 Running {len(queries_to_run)} queries in session: {session_id}")
    
    batch_results = []

    # 7) Run the queries one by one
    for i, query in enumerate(queries_to_run):
        print("\n" + "="*70)
        print(f"▶️  Query {i+1}/{len(queries_to_run)}")
        print(f"   You: {query}")
        print("="*70)
        
        log_stream = io.StringIO()
        
        try:
            with redirect_stdout(log_stream):
                response_data = ai.web_start_ai_analyst(query, session_id)
            
            ai_response = response_data.get("ai_response", "Error: No 'ai_response' key in return data.")
            print(f"\nAnalyst: {ai_response}")

            debug_log = log_stream.getvalue()
            metrics = get_latest_log_entry(log_collection, session_id)

            batch_results.append({
                "query": query,
                "outcome": metrics.get("outcome"),
                "ai_response": ai_response,
                "plan": metrics.get("plan"),
                "timings": metrics.get("timings"),
                "plan_hash": metrics.get("plan_hash"),
                "debug_log": debug_log
            })

        except Exception as e:
            print(f"\nAnalyst (ERROR): Query failed catastrophically.")
            print(f"   Error: {e}")
            debug_log = log_stream.getvalue()
            batch_results.append({
                "query": query,
                "outcome": "FAIL_CATASTROPHIC",
                "ai_response": f"ERROR: {e}",
                "plan": None,
                "timings": None,
                "plan_hash": None,
                "debug_log": f"{debug_log}\n\n--- CATASTROPHIC ERROR ---\n{e}"
            })

    # 8) Save the final results to a new JSON file
    output_filename = f"batch_results_{session_id}.json"
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(batch_results, f, indent=2)

    print("\n" + "="*70)
    print("✅ Batch run complete.")
    print(f"📜 All detailed results saved to: {output_filename}")
    print("="*70)

if __name__ == "__main__":
    main()