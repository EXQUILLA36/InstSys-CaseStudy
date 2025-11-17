# File: backend/utils/ai_core/training.py

"""
This module contains the TrainingSystem class, responsible for logging
and analyzing AI performance data over time using a MongoDB collection.
"""

from datetime import datetime, timezone
from typing import Optional, List
from collections import Counter
from pymongo.database import Database
from pymongo.errors import CollectionInvalid

class TrainingSystem:
    """
    [UPGRADED] Manages the collection and analysis of query data to improve AI performance.
    It logs detailed turn data to the 'query_log' collection for real-time analysis.
    """
    def __init__(self, mongo_db: Database):
        """
        [MODIFIED] Initializes the training system with a connection to MongoDB.
        It now directly uses the 'query_log' collection passed from the AIAnalyst.

        Args:
            mongo_db: An active pymongo.database.Database instance.
        """
        self.db = mongo_db
        self.log_collection = self.db["query_log"]
        print(f"TrainingSystem initialized and connected to 'query_log'.")

    
    def record_query_result(
        self, 
        query: str, 
        plan: dict, 
        results_count: int,
        execution_time: float, 
        error_msg: str = None,
        execution_mode: str = "unknown", 
        outcome: str = "FAIL_UNKNOWN",
        analyst_mode: str = "unknown", 
        final_answer: str = "",
        corruption_details: Optional[List[str]] = None,
        
        # --- These are all the new fields ---
        timestamp: datetime = None,
        session_id: str = None,
        planner_duration: float = 0.0,
        retrieval_duration: float = 0.0,
        synth_duration: float = 0.0,
        planner_model: str = None,
        synth_model: str = None,
        plan_hash: str = None
    ):
        """
        [UPGRADED] Records the outcome of a single query as a document directly into MongoDB,
        now including detailed performance metrics and session data.
        """
        
        # Fallback to ensure timestamp is always set
        if not timestamp:
            timestamp = datetime.now(timezone.utc)

        record = {
            "timestamp": timestamp,
            "session_id": session_id,
            "query": query,
            "plan": plan,
            "plan_hash": plan_hash,
            "outcome": outcome,
            "analyst_mode": analyst_mode,
            "execution_mode": execution_mode,
            "results_count": results_count,
            
            # --- Durations (rounded) ---
            "total_time": round(execution_time, 4),
            "planner_duration": round(planner_duration, 4),
            "retrieval_duration": round(retrieval_duration, 4),
            "synth_duration": round(synth_duration, 4),
            
            # --- Model Info ---
            "planner_model": planner_model,
            "synth_model": synth_model,
            
            "final_answer": final_answer,
            "error_message": error_msg,
            "corruption_details": corruption_details
        }
        
        try:
            # Insert the document directly into the MongoDB collection.
            self.log_collection.insert_one(record)
        except Exception as e:
            # Print a critical error if logging fails, but don't crash the app
            print(f"CRITICAL: Failed to log query to MongoDB: {e}")
            print(f"Failed Log Entry: {record}")



            # In backend/utils/ai_core/training.py
    
    # --- REPLACE THIS ENTIRE METHOD ---

    def get_training_insights(self) -> str:
        """
        [UPGRADED] Generates a summary by running efficient aggregation queries on the MongoDB log,
        now including average performance timings AND plan-based success/failure analysis.
        """
        total_queries = self.log_collection.count_documents({})
        if total_queries == 0:
            return "No training data recorded yet in the database."

        # === Pipeline 1: Count outcomes AND average timings (Existing) ===
        pipeline_outcomes = [
            {
                "$group": {
                    "_id": "$outcome",
                    "count": {"$sum": 1},
                    "avg_total_time": {"$avg": "$total_time"},
                    "avg_planner_time": {"$avg": "$planner_duration"},
                    "avg_retrieval_time": {"$avg": "$retrieval_duration"},
                    "avg_synth_time": {"$avg": "$synth_duration"}
                }
            },
            {"$sort": {"count": -1}}
        ]
        results_outcomes = list(self.log_collection.aggregate(pipeline_outcomes))
        
        outcome_counts = {item['_id']: item['count'] for item in results_outcomes}
        
        # === Pipeline 2: Calculate overall average timings (Existing) ===
        pipeline_all_avg = [
            {
                "$group": {
                    "_id": None,
                    "avg_total": {"$avg": "$total_time"},
                    "avg_planner": {"$avg": "$planner_duration"},
                    "avg_retrieval": {"$avg": "$retrieval_duration"},
                    "avg_synth": {"$avg": "$synth_duration"}
                }
            }
        ]
        avg_times_data = list(self.log_collection.aggregate(pipeline_all_avg))
        
        timing_report = "  No timing data available."
        if avg_times_data:
            times = avg_times_data[0]
            timing_report = (
                f"  - Avg Total Time:   {times.get('avg_total', 0):.2f}s\n"
                f"  - Avg Planner:    {times.get('avg_planner', 0):.2f}s\n"
                f"  - Avg Retrieval:  {times.get('avg_retrieval', 0):.2f}s\n"
                f"  - Avg Synthesizer: {times.get('avg_synth', 0):.2f}s"
            )
        
        # === Pipeline 3: NEW! Analyze Plan Success/Failure ===
        # We group by the 'plan_hash' to see which plans are used most and which fail most.
        pipeline_plans = [
            {
                "$match": {
                    "plan_hash": {"$ne": None}  # Only analyze queries that have a plan hash
                }
            },
            {
                "$group": {
                    "_id": "$plan_hash",
                    "plan_example": {"$first": "$plan"}, # Get an example of the plan
                    "total_runs": {"$sum": 1},
                    "successes": {
                        "$sum": {
                            "$cond": [
                                {"$regexMatch": {"input": "$outcome", "regex": "^SUCCESS"}},
                                1,
                                0
                            ]
                        }
                    },
                    "failures": {
                        "$sum": {
                            "$cond": [
                                {"$regexMatch": {"input": "$outcome", "regex": "^FAIL"}},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {
                "$project": {
                    "plan_example": 1,
                    "total_runs": 1,
                    "success_rate": {
                        "$multiply": [
                            {"$divide": ["$successes", "$total_runs"]},
                            100
                        ]
                    },
                    "failure_rate": {
                        "$multiply": [
                            {"$divide": ["$failures", "$total_runs"]},
                            100
                        ]
                    }
                }
            },
            {"$sort": {"total_runs": -1}}
        ]
        plan_analytics = list(self.log_collection.aggregate(pipeline_plans))

        # --- Build the report string ---
        direct_success_count = outcome_counts.get("SUCCESS_DIRECT", 0)
        direct_success_rate = (direct_success_count / total_queries) * 100 if total_queries > 0 else 0
        
        insights = [
            f"Training Summary (from {total_queries} logged queries in MongoDB):",
            f"  - Direct Success Rate (Primary tools worked): {direct_success_rate:.1f}%",
            "",
            "Average Performance (Overall):",
            timing_report,
            "",
            "Detailed Outcome Breakdown:"
        ]
        
        outcome_descriptions = {
            "SUCCESS_DIRECT": "Primary tool(s) succeeded.",
            "SUCCESS_FALLBACK": "Primary tool(s) failed, but fallback search found results.",
            "FAIL_EMPTY": "Tool(s) ran correctly but found no data.",
            "FAIL_PLANNER": "AI Planner failed to choose a tool.",
            "FAIL_EXECUTION": "An unexpected error occurred during tool execution.",
            "FAIL_UNKNOWN": "An unknown failure occurred.",
            "SUCCESS_CONVERSATIONAL": "A conversational query was handled directly.",
            "FAIL_AMBIGUOUS": "Plan stopped due to ambiguity (e.g., multiple people found)."
        }

        for outcome, count in outcome_counts.items():
            percentage = (count / total_queries) * 100
            description = outcome_descriptions.get(outcome, "No description.")
            insights.append(f"   - {outcome}: {count} queries ({percentage:.1f}%) - {description}")
        
        # --- Add the NEW plan analytics to the report ---
        insights.append("\nPlan Performance (Top 5 Most Used):")
        if not plan_analytics:
            insights.append("  No plan analytics available yet.")
        else:
            for i, plan_stat in enumerate(plan_analytics[:5]):
                # Get the first tool name to make the report readable
                try:
                    first_tool = plan_stat['plan_example']['plan'][0]['tool_call']['tool_name']
                    if 'plan' in plan_stat['plan_example']['plan'][1]['tool_call']['tool_name']:
                        plan_name = f"{first_tool} (1-step)"
                    else:
                        plan_name = f"{first_tool} -> ..."
                except Exception:
                    plan_name = f"Plan Hash: {plan_stat['_id'][:7]}..."
                
                insights.append(
                    f"  {i+1}. {plan_name}\n"
                    f"     - Runs: {plan_stat['total_runs']}, "
                    f"Success: {plan_stat.get('success_rate', 0):.1f}%, "
                    f"Fail: {plan_stat.get('failure_rate', 0):.1f}%"
                )
            
        return "\n".join(insights)
