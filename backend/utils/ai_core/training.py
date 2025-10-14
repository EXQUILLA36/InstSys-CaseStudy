# backend/utils/ai_core/training.py

"""
This module contains the TrainingSystem class, responsible for logging
and analyzing AI performance data over time.
"""

import json
import os
from datetime import datetime
from typing import Optional, List
from collections import Counter

class TrainingSystem:
    """
    Manages the collection and analysis of query data to improve AI performance over time.
    It records successful and failed queries, extracts patterns, and provides insights.
    """
    def __init__(self, training_file: str = "config/training_data.json"):
        """
        Initializes the training system.

        Args:
            training_file: The name of the JSON file used to store training data.
        """
        self.training_file = training_file
        self.training_data = self._load_training_data()
        
    def _load_training_data(self) -> dict:
        """
        Loads training data from the specified JSON file.
        If the file does not exist or is empty, it creates a new, correct data structure.
        """
        try:
            with open(self.training_file, 'r', encoding='utf-8') as f:
                # Check for an empty file to prevent a crash
                if os.path.getsize(self.training_file) == 0:
                    raise FileNotFoundError # Treat empty file as a new file
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # This is the corrected, modern data structure
            return {
                "query_log": [],
                "query_patterns": {},
                "metadata": {"created": datetime.now().isoformat(), "version": "1.0"}
            }
    
    def _save_training_data(self):
        """Saves the current state of the training data to the JSON file."""
        with open(self.training_file, 'w', encoding='utf-8') as f:
            json.dump(self.training_data, f, indent=2, ensure_ascii=False)
    
    def record_query_result(self, query: str, plan: dict, results_count: int,
                            execution_time: float, error_msg: str = None,
                            execution_mode: str = "unknown", outcome: str = "FAIL_UNKNOWN",
                            analyst_mode: str = "unknown", final_answer: str = "",
                            corruption_details: Optional[List[str]] = None):
        """
        [UPGRADED] Records the outcome of a single query with a detailed outcome status.
        """
        record = {
            "query": query,
            "plan": plan,
            "outcome": outcome,
            "analyst_mode": analyst_mode,
            "execution_mode": execution_mode,
            "results_count": results_count,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat(),
            "final_answer": final_answer,  # <-- ADDED THIS LINE
            "error_message": error_msg,
            "corruption_details": corruption_details
        }
        self.training_data["query_log"].append(record)
        self._save_training_data()
    
    def get_training_insights(self) -> str:
        """
        [UPGRADED] Generates a highly detailed summary of training data, including a breakdown by outcome.
        """
        from collections import Counter
        
        total_queries = len(self.training_data["query_log"])
        if total_queries == 0:
            return "No training data recorded yet."

        outcome_counts = Counter(r['outcome'] for r in self.training_data["query_log"])
        
        direct_success_count = outcome_counts.get("SUCCESS_DIRECT", 0)
        direct_success_rate = (direct_success_count / total_queries) * 100 if total_queries > 0 else 0
        
        insights = [
            f"Training Summary ({total_queries} total queries):",
            f"  - Direct Success Rate (Primary tools worked): {direct_success_rate:.1f}%",
            "",
            "Detailed Outcome Breakdown:"
        ]
        
        outcome_descriptions = {
            "SUCCESS_DIRECT": "Primary tool succeeded.",
            "SUCCESS_FALLBACK": "Primary tool failed, but fallback search found results.",
            "FAIL_EMPTY": "Tool and fallback ran correctly but found no data.",
            "FAIL_PLANNER": "AI Planner failed to choose a tool.",
            "FAIL_EXECUTION": "An unexpected error occurred during tool execution.",
            "FAIL_UNKNOWN": "An unknown failure occurred."
        }

        for outcome, count in outcome_counts.most_common():
            percentage = (count / total_queries) * 100
            description = outcome_descriptions.get(outcome, "No description.")
            insights.append(f"   - {outcome}: {count} queries ({percentage:.1f}%) - {description}")
            
        return "\n".join(insights)
    
    def _extract_query_patterns(self, query: str, plan: dict, success: bool):
        """
        Analyzes a query to identify and categorize its structural patterns.
        This helps in understanding which types of queries succeed or fail.
        """
        query_lower = query.lower()
        
        patterns = {
            "has_year_filter": any(year in query_lower for year in ['1st', '2nd', '3rd', '4th', 'year 1', 'year 2']),
            "has_program_filter": any(prog in query_lower for prog in ['bscs', 'bstm', 'computer science', 'tourism']),
            "is_random_request": 'random' in query_lower,
            "is_multi_condition": any(word in query_lower for word in ['and', 'or', 'both']),
            "has_name_search": any(char.isupper() for char in query if char.isalpha()),
            "plan_steps": len(plan.get('plan', [])) if isinstance(plan, dict) else 0
        }
        
        pattern_key = f"year:{patterns['has_year_filter']}_prog:{patterns['has_program_filter']}_rand:{patterns['is_random_request']}_multi:{patterns['is_multi_condition']}"
        
        if pattern_key not in self.training_data["query_patterns"]:
            self.training_data["query_patterns"][pattern_key] = {
                "successful": 0, "failed": 0, "examples": []
            }
        
        if success:
            self.training_data["query_patterns"][pattern_key]["successful"] += 1
        else:
            self.training_data["query_patterns"][pattern_key]["failed"] += 1
        
        examples = self.training_data["query_patterns"][pattern_key]["examples"]
        examples.append({"query": query, "success": success})
        if len(examples) > 5:
            examples.pop(0)
    
    def get_training_insights(self) -> str:
        """
        Generates a human-readable summary of the training data, including success rates
        and performance analysis by query pattern.
        """
        total_success = len(self.training_data["successful_queries"])
        total_failed = len(self.training_data["failed_queries"])
        success_rate = total_success / (total_success + total_failed) * 100 if (total_success + total_failed) > 0 else 0
        
        insights = [
            f"Training Summary:",
            f"   - Success Rate: {success_rate:.1f}% ({total_success}/{total_success + total_failed})",
            f"   - Successful Queries: {total_success}",
            f"   - Failed Queries: {total_failed}",
            "",
            "Pattern Analysis:"
        ]
        
        for pattern, data in self.training_data["query_patterns"].items():
            total = data["successful"] + data["failed"]
            pattern_success = data["successful"] / total * 100 if total > 0 else 0
            insights.append(f"   - {pattern}: {pattern_success:.1f}% success ({data['successful']}/{total})")
        
        return "\n".join(insights)
    
    def suggest_plan_improvements(self, query: str) -> Optional[dict]:
        """
        Suggests an improved execution plan based on identified common failure patterns.
        """
        query_lower = query.lower()
        
        if 'random' in query_lower and ('and' in query_lower or 'or' in query_lower):
            return {
                "suggestion": "For random queries with multiple conditions, use separate steps instead of complex filters",
                "recommended_approach": "Split into individual searches per condition"
            }
        
        return None
