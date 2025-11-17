# backend/utils/ai_core/analyst.py

"""
This module contains the main AIAnalyst class, which orchestrates the entire
AI reasoning and tool-use pipeline.
"""

# Standard library imports
import json
import math
import re
import time
import os
import inspect
import hashlib
from typing import Dict, Any, List, Optional
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import uuid
import hashlib

# Third-party imports
from pymongo import MongoClient

# Local (ai_core) imports
from .policy_engine import PolicyEngine 
from .database import MongoCollectionAdapter
from .llm_service import LLMService
from .prompts import PROMPT_TEMPLATES
from .training import TrainingSystem


class AIAnalyst:
    """
    The main class that orchestrates the entire process of analyzing a user query.
    It uses a Planner LLM to decide which tool to use, executes the tool(s) to
    retrieve data, and then uses a Synthesizer LLM to generate a final answer.
    """
    # In LLM_model.py, inside the AIAnalyst class:

    def __init__(self, collections: List[str], llm_config: Optional[dict] = None, execution_mode: str = "online"):
        """
        [MODIFIED] Initializes the AI Analyst with a MongoDB connection.
        """
        # --- NEW MONGODB CONNECTION ---
        mongo_cfg = llm_config.get("mongodb", {})
        mongo_connection_string = mongo_cfg.get("connection_string", "mongodb://localhost:27017/")
        mongo_db_name = mongo_cfg.get("database_name", "school_system")
        
        try:
            self.mongo_client = MongoClient(mongo_connection_string)
            self.mongo_db = self.mongo_client[mongo_db_name]
            self.mongo_client.admin.command('ping')
            print(f"Successfully connected to MongoDB database: '{mongo_db_name}'")
        except Exception as e:
            print(f"Failed to connect to MongoDB: {e}")
            raise
            
        self.collections = {name: MongoCollectionAdapter(self.mongo_db[name]) for name in collections}
        print(f"AI Analyst is now using MongoDB collections: {list(self.collections.keys())}")
        # --- END OF MONGODB MODIFICATIONS ---

        self.execution_mode = execution_mode
        config = llm_config or {}
        online_cfg = config.get('online', {})
        offline_cfg = config.get('offline', {})

        chat_cfg = config.get('chat_settings', {})
        # In-memory cache for active sessions to reduce DB reads
        self.sessions_cache = {}
        self.max_history_turns = chat_cfg.get('max_history_turns', 2)
        # Connection to the new MongoDB collection for persistent sessions
        self.sessions_collection = self.mongo_db["sessions"]
        # --- ADD THESE NEW LINES ---
        self.tool_cache_collection = self.mongo_db["tool_cache"]
        # Defines how long (in seconds) to cache the results of specific tools
        self.tool_cache_ttl = {
            "get_person_schedule": 3600,      # 1 hour
            "find_people": 86400,             # 1 day
            "get_person_profile": 86400,      # 1 day
            "get_student_grades": 3600,       # 1 hour
            "query_curriculum": 604800        # 1 week
            
        }

        online_cfg['api_mode'] = 'online'
        offline_cfg['api_mode'] = 'offline'

        if execution_mode == 'online':
            print("AI Analyst running in FULLY ONLINE mode.")
            self.planner_llm = LLMService(online_cfg)
            self.synth_llm = LLMService(online_cfg)
            self.debug_mode = online_cfg.get("debug_mode", False)
        elif execution_mode == 'offline':
            print("AI Analyst running in FULLY OFFLINE mode.")
            self.planner_llm = LLMService(offline_cfg)
            self.synth_llm = LLMService(offline_cfg)
            self.debug_mode = offline_cfg.get("debug_mode", False)
        else:
            print("AI Analyst running in SPLIT mode (Offline Planner, Online Synthesizer).")
            self.planner_llm = LLMService(offline_cfg)
            self.synth_llm = LLMService(online_cfg)
            self.debug_mode = offline_cfg.get("debug_mode", False)

        self._build_dynamic_collection_groups()

        self.db_schema_summary = "Schema not generated yet."
        self.REVERSE_SCHEMA_MAP = self._create_reverse_schema_map()
        self._generate_db_schema()
        
        self.debug("Pre-loading dynamic filter values from database...")
        self.all_positions = self._get_unique_values_for_field(['position'])
        self.all_departments = self._get_unique_values_for_field(['department'])
        self.all_programs = self._get_unique_values_for_field(['program', 'course'])
        self.all_statuses = self._get_unique_values_for_field(['employment_status'])
        self.debug(f"  -> Found {len(self.all_positions)} positions: {self.all_positions}")
        self.debug(f"  -> Found {len(self.all_departments)} departments: {self.all_departments}")
        self.debug(f"  -> Found {len(self.all_programs)} programs: {self.all_programs}")
        self.debug(f"  -> Found {len(self.all_statuses)} statuses: {self.all_statuses}")
        # --- ADD THIS NEW BLOCK TO DYNAMICALLY DEFINE GENERIC ROLES ---
        self.debug("Building dynamic role classifiers...")

        # 1. Create lowercase sets of your pre-loaded lists
        all_positions_lower = {p.lower() for p in self.all_positions}
        all_depts_lower = {d.lower() for d in self.all_departments}

        # 2. Create a small list of "meta" words that are always generic
        #    This is the ONLY hard-coded list, and it's small.
        hardcoded_synonyms = {'faculty', 'admin', 'staff', 'non-teaching', 'teaching', 'employee', 'personnel', 'student'}

        # 3. Create our master list of ALL generic words
        #    A role is "generic" if it's a department OR a common synonym
        self.all_generic_roles = hardcoded_synonyms.union(all_depts_lower)

        # 4. Create our master list of SPECIFIC positions
        #    A role is "specific" if it's in the all_positions list
        #    BUT NOT in our generic list.
        self.specific_position_roles = all_positions_lower - self.all_generic_roles

        self.debug(f"  -> All Generic Roles (Categories): {self.all_generic_roles}")
        self.debug(f"  -> Specific Positions (Job Titles): {self.specific_position_roles}")
        self.all_doc_types = self._get_unique_document_types()
        self.policy_engine = PolicyEngine(known_programs=self.all_programs)
        self.training_system = TrainingSystem(mongo_db=self.mongo_db)
        self._build_role_expansion_map()
            
        self.dynamic_examples_collection = self.mongo_db["dynamic_examples"]
        # Ensure a text index exists for efficient searching. This command is idempotent and safe to run on startup.
        self.dynamic_examples_collection.create_index([("user_pattern", "text")], name="query_text_index")  

        self.last_referenced_person = None
        self.last_referenced_aliases = []
        self.corruption_warnings = set() 

        self.available_tools = {
            "answer_conversational_query": self.answer_conversational_query,
            "get_data_by_id": self.get_data_by_id,
            "get_school_info": self.get_school_info,
            "get_database_summary" : self.get_database_summary,
            "get_person_profile": self.get_person_profile,
            "get_person_schedule": self.get_person_schedule,
            "get_adviser_info": self.get_adviser_info,
            "find_faculty_by_class_count": self.find_faculty_by_class_count,
            "verify_student_adviser": self.verify_student_adviser,
            "search_database": self.search_database,
            "resolve_person_entity": self.resolve_person_entity,
            "find_people": self.find_people,
            "compare_schedules": self.compare_schedules,
            "answer_question_about_person": self.answer_question_about_person,
            "get_student_grades": self.get_student_grades,
            "query_curriculum": self.query_curriculum,
            "request_clarification": self.request_clarification
        }

        self.METADATA_FIELD_BLACKLIST = {
            # Universal
            "_id",
            "created_at",
            "updated_at",
            "source_file",
            "data_type",
            "formatted_text",
            "raw_text",

            # Internal IDs
            "curriculum_id",
            "schedule_id",
            "info_id",
            "admin_id",
            "faculty_id",

            # Bloat / Duplicates
            "content",                # The string-duplicate *inside* metadata
            "admin_info",             # The giant duplicate object inside admin profiles
            "family_info",
            "government_ids",
            "image",                  # e.g., {"data": null, "status": "waiting"}
            "audio",                  # e.g., {"data": null, "status": "waiting"}
            "schedule",               # The raw array (schedule_by_day is cleaner)

            # Irrelevant Stats & Statuses
            "completion_percentage",
            "field_status",
            "descriptor",
            "character_count",
            "total_subjects",
            "days_teaching",
            "effective_year",
            "curriculum_year",
            "revision",
            "faculty_type",
            "source",
        }


    # In analyst.py, inside the AIAnalyst class

    # backend/utils/ai_core/analyst.py

    # backend/utils/ai_core/analyst.py

    # --- REPLACE THIS ENTIRE METHOD ---


    # --- ADD THIS ENTIRE NEW METHOD ---

    def _build_role_expansion_map(self):
        """
        [NEW] Runs once on startup to dynamically build a map
        that translates generic roles (like 'staff') into all
        specific positions found in the database.
        """
        self.debug("Building dynamic role expansion map...")
        self.role_expansion_map = {
            "faculty": set(),
            "admin": set(),
            "non_teaching": set(),
            "staff": set()
        }
        
        # We query all staff collections
        all_staff_profiles = self.search_database(collection_filter=self.staff_collections)
        
        if not all_staff_profiles:
            self.debug("...no staff profiles found to build map.")
            return

        for doc in all_staff_profiles:
            meta = doc.get("metadata", {})
            pos = meta.get("position")
            f_type = meta.get("faculty_type")
            
            if not pos:
                continue

            # Add specific positions to the correct category
            if f_type == "teaching":
                self.role_expansion_map["faculty"].add(pos)
            elif f_type == "admin":
                self.role_expansion_map["admin"].add(pos)
            elif f_type == "non_teaching":
                self.role_expansion_map["non_teaching"].add(pos)

        # "staff" is a superset of admin and non-teaching
        self.role_expansion_map["staff"] = (
            self.role_expansion_map["admin"].union(
            self.role_expansion_map["non_teaching"])
        )
        
        # "faculty" often includes the deans
        self.role_expansion_map["faculty"].update(
            pos for pos in self.role_expansion_map["admin"] 
            if "dean" in pos.lower()
        )
        
        # Convert sets to lists for JSON serialization in queries
        for k in self.role_expansion_map:
            self.role_expansion_map[k] = list(self.role_expansion_map[k])
            
        self.debug(f"...Role map built: {self.role_expansion_map}")


    def _build_dynamic_collection_groups(self):
        """
        [CORRECTED] Dynamically categorizes all loaded collections by their prefix
        to support searching new collections automatically.
        
        This version fixes the bug where schedule collections were being
        incorrectly grouped as staff profiles by checking for specific
        schedule prefixes FIRST.
        """
        # 1. Initialize empty lists
        student_list = []
        staff_list = []
        schedule_student_list = []
        schedule_faculty_list = []
        schedule_staff_list = []
        curriculum_list = []
        grades_list = []
        
        collection_keys = self.collections.keys()
        
        # 2. Sort collections, checking for MORE SPECIFIC names FIRST
        for name in collection_keys:
            # --- CHECK FOR SCHEDULES FIRST (MOST SPECIFIC) ---
            if name.startswith("schedules_"):
                schedule_student_list.append(name)
            elif name.startswith("faculty_schedules_"):
                schedule_faculty_list.append(name)
            elif name.startswith("non_teaching_schedule_"):
                schedule_staff_list.append(name)
                
            # --- CHECK FOR PROFILES SECOND (LESS SPECIFIC) ---
            elif name.startswith("students_"):
                student_list.append(name)
            elif (
                name.startswith("faculty_") or 
                name.startswith("admin_") or 
                name.startswith("non_teaching_faculty_") or 
                name.startswith("teaching_faculty_")
            ):
                staff_list.append(name)
                
            # --- OTHER DATA TYPES ---
            elif name.startswith("curriculum_"):
                curriculum_list.append(name)
            elif name.startswith("grades_"):
                grades_list.append(name)
            # 'general_info' and other specific collections are ignored, which is fine.

        # 3. Store the Python LISTS for internal logic
        self.student_collection_list = student_list
        self.staff_collection_list = staff_list
        self.staff_schedule_collection_list = schedule_staff_list
        self.student_schedule_collection_list = schedule_student_list
        self.faculty_schedule_collection_list = schedule_faculty_list
        self.staff_schedule_collection_list = schedule_staff_list

        

        # 4. Convert lists to comma-separated STRINGS for the 'collection_filter' param
        self.student_collections = ",".join(student_list)
        self.staff_collections = ",".join(staff_list)
        self.all_people_collections = ",".join(student_list + staff_list)
        
        
        self.student_schedule_collections = ",".join(schedule_student_list)
        self.faculty_schedule_collections = ",".join(schedule_faculty_list)
        self.staff_schedule_collections = ",".join(schedule_staff_list)
        self.all_schedule_collections = ",".join(
            schedule_student_list + schedule_faculty_list + schedule_staff_list
        )

        self.curriculum_collections = ",".join(curriculum_list)
        self.grades_collections = ",".join(grades_list)

        # 5. Log the results
        self.debug("Dynamic collection groups built (v2 - Corrected):")
        self.debug(f"  -> Students: {self.student_collections}")
        self.debug(f"  -> Staff/Faculty/Admin: {self.staff_collections}")
        self.debug(f"  -> Student Schedules: {self.student_schedule_collections}")
        self.debug(f"  -> Staff/Faculty Schedules: {self.faculty_schedule_collections} | {self.staff_schedule_collections}")
        self.debug(f"  -> Curriculums: {self.curriculum_collections}")
        self.debug(f"  -> Grades: {self.grades_collections}")
    # --- END OF REPLACEMENT ---

    # --- ADD THIS ENTIRE NEW METHOD ---

    def _get_or_create_session(self, session_id: str) -> dict:
        """
        [MODIFIED FOR MONGO] Retrieves a session from the in-memory cache,
        the database, or creates a new one.
        """
        # 1. Check the fast in-memory cache first
        if session_id in self.sessions_cache:
            return self.sessions_cache[session_id]

        # 2. If not in cache, check the database
        self.debug(f"Session {session_id} not in cache. Querying MongoDB...")
        session_doc = self.sessions_collection.find_one({"session_id": session_id})

        if session_doc:
            # --- START OF RECOMMENDED FIX ---
            # Ensure essential keys exist to prevent KeyErrors with old data
            session_doc.setdefault("chat_history", [])
            session_doc.setdefault("conversation_summary", "")
            session_doc.setdefault("structured_context", {
                "current_topic": "None.",
                "active_filters": {},
                "active_person_entity": None
            })
            # --- END OF RECOMMENDED FIX ---

            # 3. If found in DB, load it into the cache and return it
            self.sessions_cache[session_id] = session_doc
            return session_doc
        else:
            # 4. If it's a new session, create a new object in the cache
            self.debug(f"Creating new session: {session_id}")
            new_session = {
                "session_id": session_id,
                "chat_history": [],
                "conversation_summary": "",
                "structured_context": {
                    "current_topic": "None.",
                    "active_filters": {},
                    "active_person_entity": None
                },
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            self.sessions_cache[session_id] = new_session
            return new_session



                # --- NEW: Topic utilities -------------------------------------------------
    def _current_topic_id(self, session: dict) -> str:
        """
        Returns a stable, short topic_id derived from structured_context.current_topic.
        Falls back to 'none' if not available.
        """
        ctx = session.get("structured_context", {}) or {}
        topic = (ctx.get("current_topic") or "None.").strip().lower()
        # short, stable id for storage and filtering
        return hashlib.sha1(topic.encode("utf-8")).hexdigest()[:8]

    def _get_topic_scoped_history(self, session: dict, max_turns: int) -> list:
        """
        Returns the last (max_turns * 2) messages from the *current* topic only.
        Each user/assistant message added by _update_session_history includes a topic_id.
        """
        current_tid = self._current_topic_id(session)
        history = session.get("chat_history", []) or []

        # Walk backwards, taking only messages from the same topic_id
        scoped = []
        taken_pairs = 0
        for msg in reversed(history):
            if msg.get("topic_id") == current_tid:
                scoped.append(msg)
                # Count only assistant messages as "pairs" when seen with a user before it,
                # but simpler: stop when we reach the size limit for messages.
                if len(scoped) >= max_turns * 2:
                    break

        scoped.reverse()
        return scoped


    def _update_session_history(self, session_id: str, user_query: str, ai_response: str):
        """
        [MODIFIED FOR MONGO] Adds the latest exchange to the session's chat history,
        trims it, and saves the entire session object back to MongoDB.
        """
        # Get the current session object (from cache or DB)
        session = self._get_or_create_session(session_id)
        
        # Append the new messages
        topic_id = self._current_topic_id(session)
        session["chat_history"].append({
            "role": "user",
            "content": user_query,
            "topic_id": topic_id
        })
        session["chat_history"].append({
            "role": "assistant",
            "content": ai_response,
            "topic_id": topic_id
        })

        
        # Trim the history list (sliding window)
        history_limit = self.max_history_turns * 2
        if history_limit > 0 and len(session["chat_history"]) > history_limit:
            session["chat_history"] = session["chat_history"][-history_limit:]

        # Update the timestamp
        session["updated_at"] = datetime.now(timezone.utc)

        # Save the entire updated session object to MongoDB
        self.sessions_collection.update_one(
            {"session_id": session_id},
            {"$set": session},
            upsert=True  # Creates the document if it doesn't exist
        )
        self.debug(f"Session {session_id} saved to MongoDB.")


    # Add this new method anywhere inside the AIAnalyst class in AI.py


        # --- NEW: Coref to parameters (no text rewriting) ----------------------------
    def _coref_to_params(self, user_text: str, session: dict) -> dict:
        """
        [UPGRADED] Resolve pronouns (he/she/his/her) to the
        'active_person_entity' from the session's structured context.
        """
        import re

        text = (user_text or "").strip().lower()
        # Only trigger if a clear singular pronoun exists
        if not re.search(r"\b(he|she|his|her)\b", text):
            return {}

        # Get the single, topic-aware active person
        ctx = session.get("structured_context", {}) or {}
        active_person = ctx.get("active_person_entity")

        if not active_person:
            self.debug("-> Pronoun detected, but no 'active_person_entity' in context. Cannot resolve.")
            return {}

        self.debug(f"-> Pronoun detected. Resolving 'he/she/his/her' to '{active_person}'.")
        return {"person_name": active_person}
    




    # --- REPLACE THIS ENTIRE METHOD ---
    def _summarize_conversation(self, session_id: str):
        """
        [UPGRADED] Updates the structured context using an LLM.
        This version passes the full context to the prompt, which
        is instructed to preserve state flags like `clarification_pending`.
        """
        self.debug(f"Updating structured context for session: {session_id}")
        session = self._get_or_create_session(session_id)
        
        if len(session["chat_history"]) < 2:
            return

        # Pass the ENTIRE current context, not just a subset
        previous_context_str = json.dumps(session.get("structured_context", {}), indent=2)
        
        latest_exchange = "\n".join([
            f"User: {session['chat_history'][-2]['content']}",
            f"Assistant: {session['chat_history'][-1]['content']}"
        ])

        # Use the new, state-aware summarizer prompt
        prompt = PROMPT_TEMPLATES["conversation_summarizer_v2"].format(
            context=previous_context_str,
            latest_exchange=latest_exchange
        )

        response_str = self.planner_llm.execute(
            system_prompt="You are a context analysis AI that only outputs valid JSON.",
            user_prompt=prompt,
            json_mode=True,
            phase="planner"
        )

        new_context_data = self._repair_json(response_str)
        
        if new_context_data and isinstance(new_context_data, dict):
            # This is the fix: Update the session's context object
            # with the new data, which preserves flags like `clarification_pending`.
            session["structured_context"].update(new_context_data)
            session["updated_at"] = datetime.now(timezone.utc)
            
            # Save the entire, updated session object
            self.sessions_collection.update_one(
                {"session_id": session_id},
                {"$set": session}, # Save the whole session
                upsert=True
            )
            self.debug(f"New structured context for {session_id}: {session['structured_context']}")
        else:
            self.debug(f"Summarizer returned invalid JSON: {response_str}")




    
        

    def _get_unique_document_types(self) -> List[str]:
        """Queries the database to get all unique, non-empty document types."""
        self.debug("Discovering unique document types from the database...")
        # This calls the existing helper method to find unique values for a specific field
        return self._get_unique_values_for_field(['document_type'])
    




    def _get_unique_faculty_types(self) -> List[str]:
        """Queries the database to get all unique, non-empty faculty types."""
        self.debug("Discovering unique faculty types from the database...")
        unique_types = set()
        # The 'fields' parameter tells the tool which metadata field to look for
        results = self.get_distinct_combinations(
            collection_filter="faculty", 
            fields=['faculty_type'], 
            filters={}
        )
        
        if results.get("status") == "success":
            for item in results.get("combinations", []):
                # We check for the 'faculty_type' key in each result
                if 'faculty_type' in item and item['faculty_type']:
                    unique_types.add(str(item['faculty_type']))
        
        found_types = sorted(list(unique_types))
        self.debug(f"Found {len(found_types)} types: {found_types}")
        return found_types

    def _get_unique_values_for_field(self, fields: List[str], collection_filter: Optional[str] = None) -> List[str]:
        unique_values = set()
        
        # Translate AI-friendly field names to the actual DB field names
        db_fields = []
        for field in fields:
            if field in ['program', 'course']:
                db_fields.append('course')
            elif field == 'year_level':
                db_fields.append('year')
            else:
                db_fields.append(field)
        db_fields = list(set(db_fields)) # Remove duplicates

        for name, coll_adapter in self.collections.items():
            if collection_filter and collection_filter not in name:
                continue
            try:
                for db_field in db_fields:
                    # Use pymongo's distinct() method for efficiency
                    values = coll_adapter.collection.distinct(db_field)
                    for val in values:
                        if val: # Ensure value is not None or empty
                            unique_values.add(str(val).strip().upper())
            except Exception as e:
                self.debug(f"⚠️ Error during _get_unique_values_for_field in {name}: {e}")
                
        return sorted(list(unique_values))
        
    

    def get_data_by_id(self, pdm_id: str) -> List[dict]:
        """
        A highly specific tool to retrieve a person's profile using their unique PDM ID.
        """
        self.debug(f"🛠️ Running tool: get_profile_by_id for ID: {pdm_id}")
        
        # The system's schema mapping automatically handles variations like 
        # 'student_number' or 'stud_id', making this a robust filter.
        filters = {"$or": [{"student_id": {"$eq": pdm_id}}, {"student_number": {"$eq": pdm_id}}]}
        
        # Search all collections, as an ID could theoretically belong to anyone.
        results = self.search_database(filters=filters)
        
        if not results:
            return [{"status": "empty", "summary": f"Could not find a profile for anyone with the ID '{pdm_id}'."}]
            
        return results
    
    def compare_schedules(self, person_a_name: str, person_b_name: str) -> List[dict]:
        """
        Tool: Compares the schedules of two people by retrieving schedule documents for both.
        """
        self.debug(f"Running tool: compare_schedules for '{person_a_name}' and '{person_b_name}'")
        docs_a = self.get_person_schedule(person_name=person_a_name)
        docs_b = self.get_person_schedule(person_name=person_b_name)
        return docs_a + docs_b
    

    def request_clarification(self, question_for_user: str, missing_information: List[str]) -> List[dict]:
        """
        Tool: Signals that the AI needs to ask the user for more information
        before it can proceed. The execution loop will intercept this.
        """
        # This tool's purpose is to return a signal, not real data.
        # The main loop will handle the state change.
        return [{
            "source_collection": "system_clarification",
            "content": question_for_user,
            "metadata": {"status": "clarification_needed", "missing": missing_information}
        }]
    

    # Add this new method inside the AIAnalyst class
    
    def answer_conversational_query(self) -> list[dict]:
        """
        A simple tool that acknowledges a conversational query (like a greeting).
        It returns a placeholder document that signals a standard response is needed.
        """
        return [{
            "source_collection": "conversational_response",
            "content": "The user provided a conversational query. A standard greeting is appropriate.",
            "metadata": {"status": "success"}
        }]


        
    
    def get_school_info(self, info_type: Any = None) -> List[dict]:
        """
        [UPGRADED] A tool for retrieving general school information.
        """
        self.debug(f"🛠️ Running upgraded tool: get_school_info for topic: {info_type}")

        filters = {}
        document_type_to_find = None

        if isinstance(info_type, str):
            # --- ✨ NEW FIX: Check for keywords within the string ---
            info_type_lower = info_type.lower()
            if 'mission' in info_type_lower or 'vision' in info_type_lower:
                document_type_to_find = 'mission_vision'
            else:
                document_type_to_find = info_type_lower
            # --- END NEW FIX ---

        elif isinstance(info_type, list) and info_type:
            # This handles the case where the planner correctly sends a list
            document_type_to_find = "_".join(t.lower() for t in info_type)

        elif not info_type:
            # Wildcard search for Institutional Identity.
            self.debug("-> No topic provided. Performing wildcard search for Institutional Identity.")
            filters = {'department': 'INSTITUTIONAL_IDENTITY'}
            return self.search_database(filters=filters)

        # The doc_type_map now acts as a final validation/mapping step
        doc_type_map = {
            "mission": "mission_vision",
            "vision": "mission_vision",
            "objectives": "objectives",
            "history": "history",
            "mission_vision": "mission_vision"
        }

        # This line will now correctly map the pre-processed topic
        document_type_to_find = doc_type_map.get(document_type_to_find, document_type_to_find)

        filters = {'info_type': document_type_to_find} # <-- CORRECT FIELD NAME
        return self.search_database(filters=filters)
    
    # In backend/utils/ai_core/analyst.py

    def query_curriculum(
        self,
        program: str = None,
        year_level: int = None,
        semester: str = None,
        subject_code: str = None,
        subject_name: str = None,
        subject_type: str = None
    ) -> List[dict]:
        """
        Tool: Queries academic curriculum data based on various filters like program,
        year, semester, or subject details.
        """
        self.debug(f"Running tool: query_curriculum")

        filters = {}
        doc_filters = []
 

        # --- START MODIFICATION ---
        # Only set specific query_text if specific filters are provided
        query_text = None 
        if program and not (subject_code or subject_name):
            query_text = f"curriculum for the {program} program"
        elif subject_code:
            query_text = f"curriculum for subject {subject_code}"
        elif subject_name:
            query_text = f"curriculum containing subject {subject_name}"
        # If no specific filters, query_text remains None (will trigger wildcard search)
        # --- END MODIFICATION ---

        # Build metadata filters for precise collection matching
        if program:
            query_text = f"curriculum for the {program} program"

        # --- Build Metadata Filters (for precise matching on the collection) ---
        if program:
            filters['program'] = program

        # Build document content filters for searching within the document text
        if year_level:
            year_str = str(year_level)
            if year_str.endswith('1') and not year_str.endswith('11'): suffix = 'st'
            elif year_str.endswith('2') and not year_str.endswith('12'): suffix = 'nd'
            elif year_str.endswith('3') and not year_str.endswith('13'): suffix = 'rd'
            else: suffix = 'th'
            
            doc_filters.append({
                "$or": [
                    {"$contains": f"{year_str}{suffix} Year"}, # e.g., "1st Year"
                    {"$contains": f"Year {year_str}"},          # e.g., "Year 1"
                    {"$contains": f"{year_str} Year"}           # e.g., "1 Year"
                ]
            })

        if semester:
            semester_str = str(semester).lower()
            if "1" in semester_str or "first" in semester_str:
                doc_filters.append({"$contains": "1st Semester"})
            elif "2" in semester_str or "second" in semester_str:
                doc_filters.append({"$contains": "2nd Semester"})
            elif "sum" in semester_str:
                doc_filters.append({"$contains": "Summer"})
    
        if subject_type:
            doc_filters.append({"$contains": subject_type})
            
        if subject_code:
            doc_filters.append({"$contains": subject_code})
            query_text = f"curriculum for subject {subject_code}" 
            
        if subject_name:
            doc_filters.append({"$contains": subject_name})
            query_text = f"curriculum containing subject {subject_name}"

        # Combine multiple document filters with an "$and" condition
        document_filter = None
        if len(doc_filters) > 1:
            document_filter = {"$and": doc_filters}
        elif len(doc_filters) == 1:
            document_filter = doc_filters[0]

        # Execute the search
        self.debug(f"-> Searching 'curriculum' collections with metadata_filters={filters} and document_filter={document_filter}")
        results = self.search_database(
            query_text=query_text,
            filters=filters,
            document_filter=document_filter,
            collection_filter=self.curriculum_collections # <-- FIX
        )

        if not results:
            return [{"status": "empty", "summary": "I could not find any curriculum data that matches your criteria."}]
            
        return results
    
    
    def find_person_or_group(
    self,
    name: str = None,
    question: str = None,
    role: str = None,
    program: str = None,
    year_level: int = None,
    section: str = None,
    department: str = None,
    employment_status: str = None
) -> List[dict]:
        """
        Tool (Consolidated): A powerful tool to find information about a specific person or a group.
        - If 'name' and 'question' are provided, it answers a specific question.
        - If only 'name' is provided, it performs a deep search for that person.
        - If group filters are provided, it lists all matching people.
        """
        self.debug(f"Running consolidated tool: find_person_or_group")

        # Priority 1: Answer a specific question about a person
        if name and question:
            self.debug(f"-> Handling specific question: '{question}' for '{name}'")
            return self.answer_question_about_person(person_name=name, question=question)

        # Priority 2: Find a specific person and all their related info
        if name:
            self.debug(f"-> Performing deep search for person: '{name}'")
            entity = self.resolve_person_entity(name=name)
            
            if not entity or not entity.get("primary_document"):
                return [{"status": "empty", "summary": f"Could not find anyone matching the name '{name}'."}]

            primary_doc = entity["primary_document"]
            aliases = entity["aliases"]
            source_collection = primary_doc.get("source_collection", "")
            all_related_docs = [primary_doc]

            # Gather related documents (schedule, grades)
            if "student" in source_collection:
                meta = primary_doc.get("metadata", {})
                student_id = meta.get("student_id")
                schedule_filters = {k: v for k, v in {"program": meta.get("program"), "year_level": meta.get("year_level"), "section": meta.get("section")}.items() if v}
                if schedule_filters:
                    all_related_docs.extend(self.search_database(filters=schedule_filters, collection_filter="schedules"))
                if student_id:
                    all_related_docs.extend(self.search_database(filters={"student_id": student_id}, collection_filter="grades_"))
            
            elif "faculty" in source_collection:
                schedule_filters = {"$or": [{"adviser": {"$in": aliases}}, {"staff_name": {"$in": aliases}}]}
                all_related_docs.extend(self.search_database(filters=schedule_filters, collection_filter="schedules"))
                all_related_docs.extend(self.search_database(filters=schedule_filters, collection_filter="faculty_library_non_teaching_schedule"))

            return all_related_docs

        # Priority 3: Find a group of people using filters
        filters = {}
        collection_filter = None
        
        if role == 'student' or program or year_level or section:
            collection_filter = "students"
            if program: filters['program'] = program
            if year_level: filters['year_level'] = year_level
            if section: filters['section'] = section
        
        elif role == 'faculty' or department or employment_status:
            collection_filter = "faculty"
            if role: filters['position'] = role
            if department: filters['department'] = department
            if employment_status: filters['employment_status'] = employment_status

        if filters and collection_filter:
            self.debug(f"-> Searching for group in '{collection_filter}' with filters: {filters}")
            results = self.search_database(filters=filters, collection_filter=collection_filter)
            if not results:
                return [{"status": "empty", "summary": f"Found no people matching the specified criteria."}]
            return results

        return [{"status": "error", "summary": "To find a person or group, please provide a name or filters like role, program, or department."}]
    




    def get_database_summary(self) -> List[dict]:
        """
        [MODIFIED] Provides a high-level summary of the database. This version is adapted
        to correctly unpack the data structure from the MongoCollectionAdapter.
        """
        self.debug("Running upgraded tool: get_database_summary")
        summary_docs = []
        
        if not self.collections:
            return [{"source_collection": "system_summary", "content": "The database has no collections loaded.", "metadata": {}}]

        overall_summary = f"The database contains {len(self.collections)} collections. Here is a summary of each one:"
        summary_docs.append({"source_collection": "system_summary", "content": overall_summary, "metadata": {}})

        for name, coll_adapter in self.collections.items():
            try:
                count = coll_adapter.count()
                # Use the adapter's .peek() method to get a sample
                sample = coll_adapter.peek(limit=3)
                
                # --- THIS IS THE FIX ---
                # Correctly unpack the nested list format from the adapter's output
                metadatas_list = (sample.get("metadatas") or [[]])[0]
                
                sample_keys = list(metadatas_list[0].keys()) if metadatas_list else []
                # --- END OF FIX ---

                # Clean up the keys for better readability
                keys_to_show = sorted([key for key in sample_keys if not key.startswith('_') and key not in ['content', 'audio', 'image', 'field_status']])[:7]
                
                summary_docs.append({
                    "source_collection": "collection_info",
                    "content": f"Collection '{name}' has {count} documents. Key information includes: {', '.join(keys_to_show)}.",
                    "metadata": {
                        "collection_name": name, 
                        "item_count": count,
                        "sample_fields": keys_to_show
                    }
                })
            except Exception as e:
                self.debug(f"Could not get info for collection {name}: {e}")
        
        return summary_docs
    

    # In backend/utils/ai_core/analyst.py

    # --- REPLACE THE ENTIRE get_student_grades METHOD WITH THIS ---


    # --- ADD THIS ENTIRE NEW METHOD ---
    def _run_query_triage(self, query: str, session: dict) -> dict:
        """
        [NEW] Uses a "mini-LLM" call to classify the user's query *before*
        the main planner. Replaces all old policy_engine ambiguity checks.
        """
        self.debug("Running Query Triage (Mini-LLM)...")
        context = session.get("structured_context", {})
        
        triage_prompt = PROMPT_TEMPLATES["triage_prompt"].format(
            current_topic=context.get("current_topic", "None."),
            clarification_pending=context.get("clarification_pending", False),
            original_ambiguous_query=context.get("original_ambiguous_query", ""),
            query=query
        )
        
        # Use the fast planner LLM for this
        triage_raw = self.planner_llm.execute(
            system_prompt="You are a triage AI that only outputs valid JSON.",
            user_prompt=triage_prompt,
            json_mode=True,
            history=[], # Triage should be context-light
            phase="planner"
        )
        
        triage_json = self._repair_json(triage_raw)
        
        if not triage_json or "intent" not in triage_json:
            self.debug(f"Triage FAILED. Received: {triage_raw}. Defaulting to VALID_NEW_QUERY.")
            return {"intent": "VALID_NEW_QUERY"}
            
        self.debug(f"Triage Result: {triage_json}")
        return triage_json
    

    # --- REPLACE THE ENTIRE get_student_grades METHOD WITH THIS ---

    def get_student_grades(self, student_name: str = None, program: str = None, year_level: int = None, section: str = None) -> List[dict]:
        """
        Tool (UPGRADED V3 - Optimized): Finds grade documents and the *specific* students they belong to.
        - If a student_name is given, it works as before.
        - If a group (program/year/section) is given, it searches for *grades* first,
          then retrieves *only* the profiles for students who had those grades.
        """
        self.debug(f"Running OPTIMIZED grade tool for name='{student_name}', program='{program}', year='{year_level}', section='{section}'")
        
        # Normalize year_level=0 to None for broader matching
        if year_level == 0:
            year_level = None

        # --- Priority 1: Search by Name (This logic is fine and can stay) ---
        if student_name:
            self.debug(f"-> Prioritizing search by name: {student_name}")
            entity = self.resolve_person_entity(name=student_name)
            if not entity or not entity.get("primary_document"):
                return [{"status": "error", "summary": f"Could not find a student named '{student_name}'."}]
            
            student_docs = entity["primary_document"]
            # Get all unique student IDs from all resolved documents (profiles, schedules, etc.)
            student_ids = set()
            for doc in student_docs:
                s_id = doc.get("metadata", {}).get("student_id")
                if s_id:
                    student_ids.add(s_id)
            
            if not student_ids:
                return student_docs + [{"status": "empty", "summary": f"Found student(s) named '{student_name}' but they are missing student IDs needed to find grades."}]
            
            grade_docs = self.search_database(filters={"student_id": {"$in": list(student_ids)}}, collection_filter=self.grades_collections)
            if not grade_docs:
                return student_docs + [{"status": "empty", "summary": f"Found student(s) named '{student_name}' but could not find any grade information for them."}]
            
            return student_docs + grade_docs

        # --- Priority 2: Optimized Search by Group ---
        if program or year_level or section:
            self.debug(f"-> OPTIMIZED: Searching 'grades' collection *first* for group: program={program}, year_level={year_level}, section={section}")
            
            # 1. Build filters for the GRADES collection
            # (The log confirms grades_ccs has course, year, and section fields)
            grade_filters = {}
            if program: grade_filters['program'] = program # Assumes 'program' or alias exists in grades
            if year_level: grade_filters['year_level'] = year_level # Assumes 'year_level' or alias exists
            if section: grade_filters['section'] = section # Assumes 'section' or alias exists

            # 2. Search for the grade documents first
            grade_docs = self.search_database(filters=grade_filters, collection_filter=self.grades_collections)
            
            if not grade_docs or (grade_docs and "status" in (grade_docs[0] or {})):
                return [{"status": "empty", "summary": f"I couldn't find any grade records for students matching those criteria."}]
            
            # 3. Get the *specific* student IDs from the grades found
            student_ids_with_grades = list(set(
                doc.get("metadata", {}).get("student_id") 
                for doc in grade_docs 
                if doc.get("metadata", {}).get("student_id")
            ))
            
            if not student_ids_with_grades:
                # This is a safeguard
                return grade_docs + [{"status": "empty", "summary": "Found grade records, but they are missing student IDs, so I cannot find who they belong to."}]
            
            # 4. Now, find *only* the profiles for those specific students
            profile_filters = {"student_id": {"$in": student_ids_with_grades}}
            student_docs = self.search_database(filters=profile_filters, collection_filter=self.student_collections)

            # 5. Return the highly-focused list
            self.debug(f"-> Found {len(grade_docs)} grade docs and {len(student_docs)} matching profiles. Returning focused list.")
            return student_docs + grade_docs
        # --- END OF OPTIMIZED BLOCK ---
        
        # Priority 3: No filters provided (no change here)
        if not student_name and not program and not year_level and not section:
            self.debug("-> No filters provided. Retrieving all grade documents.")
            all_grade_docs = self.search_database(collection_filter=self.grades_collections)
            if not all_grade_docs:
                return [{"status": "empty", "summary": "I could not find any grade documents in the database."}]
            return all_grade_docs

        return [{"status": "error", "summary": "To get grades, please provide a specific student's name, a program, a year level, or a section."}]



    # In backend/utils/ai_core/analyst.py
    # --- REPLACE THE ENTIRE 'answer_question_about_person' METHOD WITH THIS ---

    def answer_question_about_person(self, person_name: str, question: str) -> List[dict]:
        """
        Tool: Answers a specific question about a person. It first finds all documents
        related to the person, then uses the Synthesizer LLM to answer the question
        based only on that retrieved information.
        """
        self.debug(f"Running QA tool: Answering '{question}' for '{person_name}'")

        # Step 1: Find the person using robust entity resolution
        self.debug(f"-> Resolving entity for '{person_name}'")
        entity = self.resolve_person_entity(name=person_name)
        
        if not entity or not entity.get("primary_document"):
            return [{"status": "empty", "summary": f"I could not find any information for a person named '{person_name}'."}]

        initial_person_docs = entity["primary_document"]
        person_docs = list(initial_person_docs) 
        aliases = entity["aliases"]

        # Step 2: Loop through each found person to gather ALL their related documents
        self.debug(f"-> Found '{entity['primary_name']}'. Gathering all related documents for {len(person_docs)} match(es)...")
        
        for person_record in initial_person_docs:
            source_collection = person_record.get("source_collection", "")
            meta = person_record.get("metadata", {})

            # Find related documents (schedule, grades) based on person type
            if "student" in source_collection:
                student_id = meta.get("student_id")
                
                # --- THIS IS THE FIX ---
                # Use the alias-aware logic to find the student's group
                schedule_filters = {
                    "program": meta.get("program") or meta.get("course"),
                    "year_level": meta.get("year_level") or meta.get("year"),
                    "section": meta.get("section")
                }
                # Remove keys that are None or empty
                schedule_filters = {k: v for k, v in schedule_filters.items() if v}
                # --- END OF FIX ---

                if schedule_filters:
                    person_docs.extend(self.search_database(filters=schedule_filters, collection_filter=self.student_schedule_collections))
                if student_id:
                    person_docs.extend(self.search_database(filters={"student_id": student_id}, collection_filter=self.grades_collections))
            
            elif "faculty" in source_collection:
                schedule_filters = {"$or": [{"adviser": {"$in": aliases}}, {"staff_name": {"$in": aliases}}]}
                person_docs.extend(self.search_database(filters=schedule_filters, collection_filter=self.faculty_schedule_collections))
                person_docs.extend(self.search_database(filters=schedule_filters, collection_filter=self.staff_schedule_collections))

        self.debug(f"-> Collected {len(person_docs)} total documents for the QA context.")

        # Step 3: Create a focused context for the Synthesizer
        context_for_qa = json.dumps({
            "status": "success",
            "data": person_docs
        }, indent=2, ensure_ascii=False)
        
        # Step 4: Call the Synthesizer LLM to perform the specific QA task
        qa_user_prompt = f"Based ONLY on the Factual Documents provided, please answer the following question concisely.\n\Factual Documents:\n{context_for_qa}\n\nQuestion: {question}"
        
        specific_answer = self.synth_llm.execute(
            system_prompt="You are a helpful assistant that answers specific questions based ONLY on the provided Factual Documents. Do not use any outside knowledge.",
            user_prompt=qa_user_prompt,
            phase="synth"
        )

        # Step 5: Return the specific answer along with the source documents
        return [
            {"source_collection": "qa_answer", "content": specific_answer, "metadata": {"question": question}}
        ] + person_docs

    # backend/utils/ai_core/analyst.py



    # --- REPLACE THE ENTIRE find_people METHOD WITH THIS (V6 - Dynamic Expansion) ---

    def find_people(self, name: str = None, position: str = None, program: str = None, year_level: int = None, section: str = None, department: str = None, employment_status: str = None, n_results: int = 1000) -> List[dict]:
        """
        Tool (Unified & DYNAMIC V6 - Dynamic Role Expansion):
        A powerful, single tool to find any person or group.
        
        [NEW] This version is "AI-Friendly." It uses a dynamically generated
        'self.role_expansion_map' to translate generic AI queries (like "admin")
        into a specific list of database positions.
        """
        try:
            n_results = int(n_results)
        except (ValueError, TypeError):
            n_results = 1000

        self.debug(f"Running DYNAMIC V6 (Dynamic Expansion) tool: find_people with params: name='{name}', position='{position}', program='{program}', dept='{department}'")
        filters = {}
        collection_filter = None
        
        if isinstance(position, list) and len(position) == 1:
            self.debug(f"-> Normalizing single-item position list {position} to a string.")
            position = position[0]

        pos_str = str(position).lower().strip() if position else ""

        # --- WILDCARD SEARCH ---
        if not any([name, position, program, year_level, section, department, employment_status]):
            self.debug(f"-> No parameters provided. Searching ALL people collections: {self.all_people_collections}")
            return self.search_database(query_text="*", collection_filter=self.all_people_collections, n_results=n_results)

        # --- INTELLIGENT COLLECTION ROUTING ---
        is_student_query = (
            pos_str == 'student' or
            program or 
            year_level or 
            section
        )
        
        if is_student_query:
            self.debug(f"-> Query identified as a STUDENT search. Using: {self.student_collections}")
            collection_filter = self.student_collections
            if program: filters['program'] = program
            if year_level: filters['year_level'] = year_level
            if section: filters['section'] = section
            
            if not filters and is_student_query and not name:
                return self.search_database(query_text="*", collection_filter=self.student_collections)
        
        else: # This handles faculty, admin, staff, registrar, librarian, etc.
            collection_filter = self.staff_collections
            
            # --- THIS IS THE NEW DYNAMIC ROLE EXPANSION LOGIC ---
            if position and pos_str in self.role_expansion_map:
                # AI sent a generic word (e.g., "admin"). Expand it to the dynamic list.
                expanded_roles = self.role_expansion_map.get(pos_str)
                
                if expanded_roles:
                    self.debug(f"-> Detected GENERIC position '{position}'. Dynamically expanding to: {expanded_roles}")
                    # Create a regex-friendly list for the $in query to be case-insensitive
                    role_regex_list = [re.compile(f"^{re.escape(role)}$", re.IGNORECASE) for role in expanded_roles]
                    filters['position'] = {'$in': role_regex_list}
                else:
                    # AI sent a generic word, but our map is empty. Fallback.
                    self.debug(f"-> Generic position '{position}' has no roles in map. Applying as specific filter.")
                    filters['position'] = {'$regex': position, '$options': 'i'}

            elif position:
                # AI sent a SPECIFIC title (e.g., "Professor").
                self.debug(f"-> Applying SPECIFIC position filter for: {position}")
                if isinstance(position, list):
                    filters['position'] = {'$in': [re.compile(p, re.IGNORECASE) for p in position]}
                elif isinstance(position, str):
                    filters['position'] = {'$regex': f"^{re.escape(position)}$", '$options': 'i'}
            # --- END OF NEW LOGIC ---
                
            if department and department.lower() != 'all':
                filters['department'] = department
            if employment_status:
                filters['employment_status'] = employment_status

            if not filters and not name and position:
                 return self.search_database(query_text="*", collection_filter=self.staff_collections)

        # --- NAME SEARCH LOGIC (No changes needed) ---
        if name:
            self.debug(f"-> Name provided. Using robust entity resolution for '{name}'.")
            # Use V5.4 (Strict) for accurate name matching
            entity = self.resolve_person_entity(name=name) 
            
            if entity and entity.get("aliases"):
                # We build a regex list to be case-insensitive
                name_regex_list = [re.compile(re.escape(alias), re.IGNORECASE) for alias in entity["aliases"]]
                filters['full_name'] = {"$in": name_regex_list}
                
                if not any([position, program, year_level, section, department]):
                    collection_filter = self.all_people_collections
                    self.debug(f"-> Name search with no role, searching all collections: {self.all_people_collections}")
            else:
                return [{"status": "empty", "summary": f"Could not find anyone named '{name}'."}]

        if not filters:
             return [{"status": "error", "summary": "Please provide criteria to find people."}]
        
        
        self.debug(f"-> Executing search on collections: '{collection_filter}' with filters: {filters}")
        
        # The search_database function will handle the aliasing (e.g. program -> course)
        results = self.search_database(filters=filters, collection_filter=collection_filter, n_results=n_results)
        
        if not results:
            self.debug("-> Search returned no documents. Returning 'empty' status.")
            return [{"status": "empty", "summary": "I could not find any people matching those criteria."}]
            
        return results



    def get_person_schedule(self, person_name: str = None, program: str = None, year_level: int = None, section: str = None, position: str = None, department: str = None) -> List[dict]:
        """
        Tool (Unified & DYNAMIC V7 - Role-Aware):
        - Finds schedule for a student group (program, year, section)
        - Finds schedule for a specific person (person_name)
        - Finds schedules for ALL people in a role (position, department)
        
        The ambiguity check is now smarter: it only triggers if
        the search was by a *person_name* and found multiple people.
        A search by *position* is expected to return multiple people.
        """
        self.debug(f"Running DYNAMIC V7 (Role-Aware) schedule tool for person='{person_name}', program={program}, year={year_level}, section={section}, position={position}")

        # --- Case 1: Student Group Schedule Logic ---
        if not person_name and (program or year_level is not None or section):
            filters = {}
            if program: filters["program"] = program
            if year_level is not None: filters["year_level"] = year_level
            if section: filters["section"] = section

            self.debug(f"-> Case 1: Running group schedule search with filters={filters}")
            schedule_docs = self.search_database(filters=filters, collection_filter=self.student_schedule_collections)
            if not schedule_docs:
                return [{"status": "empty", "summary": "No student schedules found for the specified group."}]
            return schedule_docs
        
        # --- THIS IS THE NEW BLOCK ---
        # --- Case 2: Role-Based Schedule Logic (e.g., "all librarians") ---
        if not person_name and (position or department):
            self.debug(f"-> Case 2: Running role-based search for position={position}, dept={department}")
            
            # 1. Find all people matching the role
            profile_docs = self.find_people(position=position, department=department)

            # --- THIS IS THE FIX ---
            # If find_people returns no profiles or an 'empty' status,
            # we must stop here to prevent a crash.
            if not profile_docs or "status" in (profile_docs[0] or {}):
                 self.debug("   -> No profiles found for this role. Searching for schedules directly by role.")

                 # --- THIS IS THE FIX ---
                 schedule_filters = {}
                 if position: 
                     # Use regex for partial/case-insensitive matching on position
                     schedule_filters['position'] = {'$regex': position, '$options': 'i'}
                 if department: 
                     schedule_filters['department'] = {'$regex': department, '$options': 'i'}

                 # Search all faculty and staff schedule collections
                 all_staff_schedule_collections = ",".join(
                     self.faculty_schedule_collection_list + self.staff_schedule_collection_list
                 )

                 schedule_docs = self.search_database(
                     filters=schedule_filters,
                     collection_filter=all_staff_schedule_collections
                 )

                 if not schedule_docs:
                     return [{"status": "empty", "summary": f"Could not find any people or schedules for {position or ''} in {department or ''}."}]

                 # If schedules are found, return them directly
                 return schedule_docs
                 # --- END OF FIX ---
            
            # 2. Collect all their aliases for a single, efficient search
            all_aliases = set()
            for doc in profile_docs:
                meta = doc.get("metadata", {})
                
                # This 'if' check prevents the crash if a bad doc (like status:empty)
                # somehow got through the check above.
                if meta.get("full_name"):
                    all_aliases.add(meta.get("full_name"))
                
                    # This was the line that crashed (name=None).
                    # Now it's safely inside the 'if' block.
                    entity = self.resolve_person_entity(name=meta.get("full_name"))
                    if entity and entity.get("aliases"):
                        all_aliases.update(entity["aliases"])

            if not all_aliases:
                return profile_docs + [{"status": "empty", "summary": "Found people, but could not identify their names to find schedules."}]

            self.debug(f"   -> Found {len(profile_docs)} people. Searching for schedules using aliases: {all_aliases}")

            # 3. Find all schedules matching any of these people
            schedule_filters = {"$or": [
                {"full_name": {"$in": list(all_aliases)}},
                {"adviser_name": {"$in": list(all_aliases)}},
                {"staff_name": {"$in": list(all_aliases)}}
            ]}
            all_schedule_collections = ",".join(self.student_schedule_collection_list + self.faculty_schedule_collection_list + self.staff_schedule_collection_list)
            
            schedule_docs = self.search_database(
                filters=schedule_filters,
                collection_filter=all_schedule_collections
            )
            
            if not schedule_docs:
                return profile_docs + [{"status": "empty", "summary": "Found people but could not find any matching schedules."}]
            
            # 4. Return all profiles + all schedules. No ambiguity check needed.
            return profile_docs + schedule_docs
        # --- END OF NEW BLOCK ---

        # --- Case 3: Specific Person's Schedule ---
        if person_name:
            self.debug(f"-> Case 3: Calling V5 entity resolver for: {person_name}")
            
            entity = self.resolve_person_entity(name=person_name)
            
            if not entity or not entity.get("primary_document"):
                return [{"status": "empty", "summary": f"Could not find anyone matching '{person_name}'."}]
            
            all_found_docs = entity["primary_document"]
            
            # Filter into profiles and schedules
            schedule_docs = []
            profile_docs = []
            
            all_schedule_collections = (
                self.student_schedule_collection_list +
                self.faculty_schedule_collection_list +
                self.staff_schedule_collection_list
            )

            for doc in all_found_docs:
                source_coll = doc.get("source_collection", "")
                if any(s == source_coll for s in all_schedule_collections):
                    schedule_docs.append(doc)
                else:
                    profile_docs.append(doc)

            # --- MODIFIED AMBIGUITY CHECK ---
            # Only trigger ambiguity if the search was BY NAME
            unique_profile_ids = set()
            for doc in profile_docs:
                meta = doc.get("metadata", {})
                pid = meta.get("faculty_id", meta.get("student_id", meta.get("full_name")))
                if pid:
                    unique_profile_ids.add(pid)
            
            # THIS IS THE CHANGE: We only run this check IF person_name was provided.
            if person_name and len(unique_profile_ids) > 1:
                self.debug(f"-> Ambiguity detected: Search for *name* '{person_name}' found {len(unique_profile_ids)} people.")
                return profile_docs + [{
                    "source_collection": "system_signal",
                    "content": "Ambiguity detected",
                    "metadata": {"status": "clarification_needed"}
                }]
            # --- END OF MODIFIED CHECK ---

            if not schedule_docs:
                return profile_docs + [{"source_collection": "system_note", "content": f"Found person '{person_name}' but could not find a matching schedule.", "metadata": {"status": "empty"}}]
            
            return profile_docs + schedule_docs

        return [{"status": "error", "summary": "Please provide a person's name, a student group (program, year), or a role (position)."}]


    def get_adviser_info(self, person_name: str = None, program: str = None, year_level: int = None, section: str = None) -> List[dict]:
        """
        Tool (UPGRADED): Finds the adviser for a specific student (by name) or a
        student group (by filters), and retrieves the adviser's faculty profile.
        
        [FIX]: Now injects the found 'adviser' name into a 'full_name' key
        to ensure placeholder resolution in multi-step plans.
        """
        self.debug(f"Running UPGRADED adviser tool for person='{person_name}', program={program}, year={year_level}, section={section}")

        schedule_docs = []
        all_found_docs = [] # To store student profiles, etc.
        schedule_filters = {}

        # --- Case 1: Search by a specific student's name ---
        if person_name:
            self.debug(f"-> Prioritizing search by name: {person_name}")
            entity = self.resolve_person_entity(name=person_name)
            
            if not entity or not entity.get("primary_document"):
                return [{"status": "error", "summary": f"Could not find a student named '{person_name}'."}]
            
            # Get the top match. We only support finding the adviser for one person at a time.
            student_doc = entity["primary_document"][0] 
            all_found_docs.append(student_doc) # Add student profile to results
            
            meta = student_doc.get("metadata", {})
            source_collection = student_doc.get("source_collection", "")

            # Check if this person is a student
            if any(s == source_collection for s in self.student_collection_list):
                schedule_filters = {
                    "program": meta.get("program") or meta.get("course"),
                    "year_level": meta.get("year_level") or meta.get("year"),
                    "section": meta.get("section")
                }
                if not all(schedule_filters.values()):
                    return all_found_docs + [{"status": "empty", "summary": f"Student record for '{person_name}' is missing key details (program/year/section) to find their adviser's schedule."}]
            else:
                return all_found_docs + [{"status": "error", "summary": f"The person '{person_name}' was found, but they are not a student and do not have an adviser."}]
        
        # --- Case 2: Search by group filters (or filters from Case 1) ---
        elif program and (year_level is not None): # <-- Added 'is not None' for robustness
            self.debug(f"-> Searching for group: program={program}, year_level={year_level}, section={section}")
            schedule_filters["program"] = program
            schedule_filters["year_level"] = year_level
            if section:
                schedule_filters["section"] = section
        
        else:
            return [{"status": "error", "summary": "To find an adviser, please provide a student's name or a program and year level."}]

        # --- Shared Logic: Get schedule using the determined filters ---
        self.debug(f"-> Searching for schedule with filters: {schedule_filters}")
        schedule_docs = self.search_database(
            filters=schedule_filters,
            collection_filter=self.student_schedule_collections
        )

        if not schedule_docs:
            summary = "Could not find a matching schedule to determine the adviser."
            return all_found_docs + [{"status": "empty", "summary": summary}]
        
        # --- Shared Logic: Get adviser from schedule and find their profile ---
        # We only need to look at the first matching schedule
        first_schedule_doc = schedule_docs[0]
        adviser_name = first_schedule_doc.get("metadata", {}).get("adviser")

        if not adviser_name:
            return all_found_docs + schedule_docs + [{"status": "empty", "summary": f"Found a schedule but it is missing an adviser's name."}]
        
        # --- THIS IS THE FIX ---
        # Inject the 'adviser' name as 'full_name' into the schedule doc's
        # metadata. This guarantees the placeholder $full_name_from_step_1
        # will resolve successfully in the next step.
        first_schedule_doc.setdefault("metadata", {})["full_name"] = adviser_name
        self.debug(f"   -> Injected 'full_name: {adviser_name}' into schedule doc for placeholder.")
        # --- END OF FIX ---
        
        self.debug(f"-> Found adviser '{adviser_name}'. Resolving their profile.")
        adviser_entity = self.resolve_person_entity(name=adviser_name)
        faculty_docs = adviser_entity.get("primary_document", [])
        
        if not faculty_docs:
             # This is now safe, because schedule_docs[0] has the 'full_name' key
             return all_found_docs + schedule_docs + [{"status": "empty", "summary": f"Found adviser '{adviser_name}' on the schedule but could not find their faculty profile."}]

        # Return all collected documents
        return all_found_docs + schedule_docs + faculty_docs



    def find_faculty_by_class_count(self, find_most: bool = True) -> List[dict]:
        """
        Tool: Finds the faculty member who teaches the most or fewest subjects by analyzing
        all class schedule documents.
        """
        self.debug(f"Running tool: find_faculty_by_class_count (find_most={find_most})")
        
        schedule_docs = self.search_database(collection_filter="schedules", query_text="class schedule")
        if not schedule_docs:
            return [{"status": "empty", "summary": "No schedule documents were found to analyze."}]

        adviser_counts = {}
        for doc in schedule_docs:
            meta = doc.get("metadata", {})
            adviser = meta.get("adviser")
            subject_count = meta.get("subject_count", 0)
            if adviser and subject_count > 0:
                adviser_counts[adviser] = adviser_counts.get(adviser, 0) + subject_count

        if not adviser_counts:
            return [{"status": "empty", "summary": "Found schedules, but could not determine adviser counts."}]

        sorted_advisers = sorted(adviser_counts.items(), key=lambda item: item[1], reverse=find_most)
        target_adviser_name, count = sorted_advisers[0]
        
        summary_doc = {
            "source_collection": "analysis_result",
            "content": f"The faculty with the {'most' if find_most else 'fewest'} classes is {target_adviser_name} with {count} subject(s).",
            "metadata": {"status": "success"}
        }
        
        faculty_profile = self.search_database(query=target_adviser_name, collection_filter="faculty")
        
        return [summary_doc] + faculty_profile

    def verify_student_adviser(self, student_name: str, adviser_name: str) -> List[dict]:
        """
        Tool: Verifies if a given adviser is the correct one for a student by comparing
        the claimed adviser's name with the official adviser on the student's schedule.
        """
        self.debug(f"Running tool: verify_student_adviser for '{student_name}' and '{adviser_name}'")
        
        # 1. Get the student's schedule to find their official adviser.
        student_schedule_docs = self.get_person_schedule(person_name=student_name)
        
        actual_adviser_name = None
        for doc in student_schedule_docs:
            if "schedule" in doc.get("source_collection", ""):
                actual_adviser_name = doc.get("metadata", {}).get("adviser")
                break
                
        if not actual_adviser_name:
            return [{"status": "empty", "summary": f"Could not find an official adviser for {student_name}."}]

        # 2. Resolve both the official and claimed advisers to get all their name aliases.
        self.debug(f"   -> Official adviser is '{actual_adviser_name}'. Resolving...")
        official_adviser_entity = self.resolve_person_entity(name=actual_adviser_name)
        
        self.debug(f"   -> Claimed adviser is '{adviser_name}'. Resolving...")
        claimed_adviser_entity = self.resolve_person_entity(name=adviser_name)
        
        official_aliases = set(official_adviser_entity.get("aliases", []))
        claimed_aliases = set(claimed_adviser_entity.get("aliases", []))
        
        # 3. Compare alias sets. If there's an overlap, it's a match.
        is_match = not official_aliases.isdisjoint(claimed_aliases)
        
        summary_content = (
            f"Verification result: The claim that {adviser_name} advises {student_name} is {'CORRECT' if is_match else 'INCORRECT'}. "
            f"The official adviser on record is {actual_adviser_name}."
        )
        summary_doc = {"source_collection": "analysis_result", "content": summary_content, "metadata": {"status": "success"}}
        
        return [summary_doc] + student_schedule_docs

    def get_distinct_combinations(self, collection_filter: str, fields: List[str], filters: dict) -> dict:
        """
        Retrieves unique combinations of values for specified fields from the database,
        optionally applying a filter.
        """
        self.debug(f"get_distinct_combinations | collection='{collection_filter}' | fields={fields} | filters={filters}")
        
        where_clause = {}
        if filters:
            key, value = next(iter(filters.items()))
            standard_key = self.REVERSE_SCHEMA_MAP.get(key, key)
            possible_keys = list(set([standard_key] + [orig for orig, std in self.REVERSE_SCHEMA_MAP.items() if std == standard_key]))
            where_clause = {"$or": [{k: {"$eq": value}} for k in possible_keys]}

        unique_combinations = set()
        field_map = {
            std_field: list(set([std_field] + [orig for orig, std in self.REVERSE_SCHEMA_MAP.items() if std == std_field]))
            for std_field in fields
        }

        for name, coll in self.collections.items():
            if collection_filter == "." or collection_filter in name:
                try:
                    # Only use the 'where' parameter if a filter clause was built
                    if where_clause:
                        results = coll.get(where=where_clause, include=["metadatas"])
                    else:
                        results = coll.get(include=["metadatas"])

                    for meta in results.get("metadatas", []):
                        combo_values = []
                        for std_field in fields:
                            found_value = None
                            for original_key in field_map[std_field]:
                                if original_key in meta:
                                    found_value = meta[original_key]
                                    break
                            combo_values.append(found_value)
                        
                        combo = tuple(combo_values)
                        if all(item is not None for item in combo):
                            unique_combinations.add(combo)
                except Exception as e:
                    self.debug(f"Error during get_distinct_combinations in {name}: {e}")

        combinations_list = [dict(zip(fields, combo)) for combo in sorted(list(unique_combinations))]
        self.debug(f"Found {len(combinations_list)} distinct combinations.")
        return {"status": "success", "combinations": combinations_list}
        
    def _fuzzy_name_match(self, name1: str, name2: str, threshold=0.5) -> bool:
        """
        Performs a robust fuzzy name comparison that handles titles, punctuation,
        and middle initials by checking if one name's parts are a subset of the other's.
        """
        if not name1 or not name2:
            return False
        
        def clean_name_to_set(name: str) -> set:
            """Helper to clean a name string and return a set of its component words."""
            # Remove common titles and suffixes
            name = re.sub(r'\b(DR|PROF|MR|MS|MRS|JR|SR|I|II|III|IV)\b\.?', '', name.upper(), flags=re.IGNORECASE)
            # Remove all punctuation
            name = re.sub(r'[^\w\s]', '', name)
            return set(part for part in name.strip().split() if part)

        name1_parts = clean_name_to_set(name1)
        name2_parts = clean_name_to_set(name2)
        
        if not name1_parts or not name2_parts:
            return False
        
        # Check if the shorter name's parts are all contained within the longer name's parts.
        if len(name1_parts) <= len(name2_parts):
            return name1_parts.issubset(name2_parts)
        else:
            return name2_parts.issubset(name1_parts)

            # --- REPLACE THE ENTIRE resolve_person_entity METHOD WITH THIS ---

            # --- REPLACE THE ENTIRE resolve_person_entity METHOD WITH THIS (V5.1) ---

            # --- REPLACE THE ENTIRE resolve_person_entity METHOD WITH THIS (V5.2 - Corrected) ---

            # In backend/utils/ai_core/analyst.py
    
    # --- REPLACE THE ENTIRE resolve_person_entity METHOD WITH THIS (V5.4) ---

    # --- REPLACE THE ENTIRE resolve_person_entity METHOD WITH THIS (V5.4 - Strict) ---

    def resolve_person_entity(self, name: str, **kwargs) -> dict:
        """
        Tool (UPGRADED V5.4 - Strict):
        Finds all documents and name variations for a person.
        
        This version is much stricter. It performs a high-confidence,
        full-name regex search first. It will only fall back to a
        partial-word search if the strict search yields no results.
        This fixes ambiguity when a student and faculty share a similar name.
        """
        self.debug(f"Resolving entity (V5.4 - Strict) for: '{name}' with filters: {kwargs}")
        
        # 1. Clean the main 'name' parameter
        aggressive_clean_pattern = r'\b(PROF|PROFESSOR|DR|DOCTOR|MR|MS|MRS)\b\.?|[^\w\s]'
        cleaned_name = re.sub(aggressive_clean_pattern, '', name, flags=re.IGNORECASE).strip()
        
        # Create a regex pattern that matches the *full name* as a whole string.
        # This is much stricter than searching for individual words.
        # It handles names like "Reyes, Miguel S." or "Miguel S. Reyes".
        # We escape the name and require all parts to be present.
        name_parts_for_regex = [re.escape(part) for part in re.split(r'[\s,]+', cleaned_name) if part]
        full_name_regex_pattern = r'.*'.join(name_parts_for_regex)
        search_query_regex = re.compile(full_name_regex_pattern, re.IGNORECASE)

        # 2. Get other filters (e.g., department, program) from kwargs
        active_filters = {}
        for key, value in kwargs.items():
            if key not in ["name", "person_name", "student_name"] and value:
                active_filters[key] = value
        
        self.debug(f"   -> Pass 1: Searching with STRICT full-name regex: '{search_query_regex.pattern}' and filters: {active_filters}")

        # --- Pass 1: High-Confidence Profile Search ---
        # Search for the *full name regex* in the 'full_name' field.
        profile_collections = self.all_people_collections
        profile_filters = dict(active_filters)
        profile_filters['full_name'] = {"$regex": search_query_regex}
        
        profile_docs = self.search_database(
            filters=profile_filters,
            collection_filter=profile_collections
        )
        
        # --- Fallback: If full name search fails, try partial words (V5.3 logic) ---
        if not profile_docs:
            self.debug("   -> No exact profile match. Falling back to V5.3 partial word search.")
            
            all_name_parts = set(part for part in cleaned_name.lower().split() if len(part) > 2)
            if not all_name_parts:
                return {} # No valid name parts to search for
            
            search_regex_list = [re.compile(re.escape(word), re.IGNORECASE) for word in all_name_parts]
            
            profile_filters = dict(active_filters)
            name_and_conditions = []
            for regex in search_regex_list:
                name_and_conditions.append({
                    "$or": [
                        {"full_name": {"$regex": regex}},
                        {"position": {"$regex": regex}},
                        {"department": {"$regex": regex}}
                    ]
                })
            
            if name_and_conditions:
                profile_filters["$and"] = name_and_conditions
            else:
                # No valid partial words, so we can't search
                return {}
            
            profile_docs = self.search_database(
                filters=profile_filters,
                collection_filter=profile_collections
            )
        # --- END OF FALLBACK ---

        # De-duplicate profiles
        unique_profiles = list({doc.get("metadata", {}).get("student_id", doc.get("metadata", {}).get("faculty_id", doc.get("content"))): doc for doc in profile_docs}.values())
        self.debug(f"   -> Found {len(unique_profiles)} unique profile(s).")
        
        if not unique_profiles:
            self.debug(f"   -> No profiles found. Aborting.")
            return {}

        # --- Pass 2: Find all related SCHEDULES for each profile ---
        all_related_schedules = []
        all_aliases = set()
        
        all_aliases.add(name)
        all_aliases.add(cleaned_name)
        
        student_schedule_collections = ",".join(self.student_schedule_collection_list)
        staff_schedule_collections = ",".join(self.faculty_schedule_collection_list + self.staff_schedule_collection_list)

        for profile in unique_profiles:
            meta = profile.get("metadata", {})
            source_coll = profile.get("source_collection", "")
            
            profile_full_name = meta.get("full_name")
            if profile_full_name:
                all_aliases.add(profile_full_name)

            if any(s == source_coll for s in self.student_collection_list):
                student_filters = {
                    "program": meta.get("program") or meta.get("course"),
                    "year_level": meta.get("year_level") or meta.get("year"),
                    "section": meta.get("section")
                }
                # Only search for schedules if we have all the keys
                if all(student_filters.values()):
                    all_related_schedules.extend(self.search_database(
                        filters=student_filters,
                        collection_filter=student_schedule_collections
                    ))
            
            elif any(s == source_coll for s in self.staff_collection_list):
                # We have a faculty/staff profile. Search for their schedule
                # using their *full name*. This is the key.
                if not profile_full_name:
                    continue
                    
                # Create a strict regex for the *exact* full name
                name_parts = [re.escape(part) for part in re.split(r'[\s,]+', profile_full_name) if part]
                strict_regex = re.compile(r'.*'.join(name_parts), re.IGNORECASE)
                
                schedule_filters = {"$or": [
                    {"full_name": {"$regex": strict_regex}},
                    {"adviser_name": {"$regex": strict_regex}},
                    {"staff_name": {"$regex": strict_regex}}
                ]}
                all_related_schedules.extend(self.search_database(
                    filters=schedule_filters,
                    collection_filter=staff_schedule_collections
                ))

        # Add aliases from any schedules we found
        for sched in all_related_schedules:
            meta = sched.get("metadata", {})
            for key in ["full_name", "adviser_name", "staff_name"]:
                if meta.get(key):
                    all_aliases.add(meta.get(key))

        all_docs = unique_profiles + all_related_schedules
        
        # De-duplicate all found documents
        final_unique_docs = list({
            doc.get("metadata", {}).get("schedule_id", 
            doc.get("metadata", {}).get("student_id", 
            doc.get("metadata", {}).get("faculty_id", doc.get("metadata", {}).get("full_name"))))
            : doc for doc in all_docs
        }.values())

        primary_name = name
        if unique_profiles:
            primary_name = unique_profiles[0].get("metadata", {}).get("full_name", name)

        self.debug(f"   -> Entity resolved: Primary='{primary_name}', Aliases={all_aliases}, Found {len(final_unique_docs)} total docs.")
        
        return {
            "primary_name": primary_name,
            "aliases": list(all_aliases),
            "primary_document": final_unique_docs
        }


    def resolve_person_entity(self, name: str, **kwargs) -> dict:
        """
        Tool (UPGRADED V5.3 - Filter-Aware):
        Finds all documents and name variations for a person.
        
        This version now accepts **kwargs to receive active_filters
        (like program, department) to resolve ambiguity.
        """
        self.debug(f"Resolving entity (V5.3) for: '{name}' with filters: {kwargs}")
        
        # --- THIS IS THE NEW FILTER-HANDLING BLOCK ---
        # 1. Separate name filters from other filters
        active_filters = {}
        name_parts_from_filters = []
        for key, value in kwargs.items():
            if key in ["name", "person_name", "student_name"]:
                name_parts_from_filters.append(str(value))
            elif value: # Add any other filter (program, department, etc.)
                active_filters[key] = value
        
        # 2. Clean the main 'name' parameter
        aggressive_clean_pattern = r'\b(PROF|PROFESSOR|DR|DOCTOR|MR|MS|MRS)\b\.?|[^\w\s]'
        cleaned_name = re.sub(aggressive_clean_pattern, '', name, flags=re.IGNORECASE)
        cleaned_query = ' '.join(cleaned_name.split()).lower()
        
        # 3. Combine all name parts
        all_name_parts = set(part for part in cleaned_query.split() if len(part) > 2)
        for name_part in name_parts_from_filters:
             all_name_parts.add(name_part.lower())
        
        search_words = all_name_parts
        if not search_words:
            return {}
            
        self.debug(f"   -> Performing multi-pass search for all words: {search_words}")

        # --- Pass 1: Find all matching PROFILES ---
        profile_collections = self.all_people_collections
        profile_docs = []
        
        # Build a regex for each individual word
        search_regex_list = [re.compile(re.escape(word), re.IGNORECASE) for word in search_words]
        
        # --- THIS IS THE FIX ---
        # Build a filter that requires ALL words to be present
        # across several key fields.
        
        # We assume one word is the name, the others are descriptors.
        # This is complex. Let's simplify.
        # We will search for ANY document that contains ALL these words
        # in its full_name OR position OR department.
        
        # --- THIS IS THE MODIFIED FILTER LOGIC ---
        # Start with the active_filters (e.g., {"program": "bscs"})
        profile_filters = dict(active_filters)
        
        # Now, add the name filters. We need to match ALL name words.
        name_and_conditions = []
        for regex in search_regex_list:
            name_and_conditions.append({
                "$or": [
                    {"full_name": {"$regex": regex}},
                    {"position": {"$regex": regex}}, # Keep this broad
                    {"department": {"$regex": regex}} # Keep this broad
                ]
            })
        
        # Add the name conditions to the main filter
        profile_filters["$and"] = name_and_conditions
        # --- END OF MODIFIED FILTER LOGIC ---
        # --- END OF FIX ---
        
        profile_docs.extend(self.search_database(
            filters=profile_filters,
            collection_filter=profile_collections
        ))
        
        # De-duplicate profiles
        unique_profiles = list({doc.get("metadata", {}).get("student_id", doc.get("metadata", {}).get("faculty_id", doc.get("content"))): doc for doc in profile_docs}.values())
        self.debug(f"   -> Pass 1: Found {len(unique_profiles)} unique profile(s).")
        
        if not unique_profiles:
            # Fallback... (the rest of the fallback logic is fine)
            self.debug(f"   -> No profiles found. Fallback to searching all schedule collections.")
            all_schedule_collections = ",".join(self.student_schedule_collection_list + self.faculty_schedule_collection_list + self.staff_schedule_collection_list)
            
            schedule_filters = {"$or": [
                {"full_name": {"$in": search_regex_list}},
                {"adviser_name": {"$in": search_regex_list}},
                {"staff_name": {"$in": search_regex_list}}
            ]}
            
            schedule_docs = self.search_database(
                filters=schedule_filters,
                collection_filter=all_schedule_collections
            )
            
            if not schedule_docs:
                self.debug(f"   -> No profiles or schedules found.")
                return {}
            
            unique_schedules = list({doc.get("metadata", {}).get("schedule_id"): doc for doc in schedule_docs}.values())
            primary_name = unique_schedules[0].get("metadata", {}).get("full_name", name)
            
            return {
                "primary_name": primary_name,
                "aliases": list(set([meta.get("full_name", "") for doc in unique_schedules for meta in [doc.get("metadata", {})]])),
                "primary_document": unique_schedules
            }

        # --- Pass 2: Find all related SCHEDULES for each profile (if profiles were found) ---
        # ... (The rest of the function is 100% correct as written in V5.1) ...
        all_related_schedules = []
        all_aliases = set(search_words)
        
        student_schedule_collections = ",".join(self.student_schedule_collection_list)
        staff_schedule_collections = ",".join(self.faculty_schedule_collection_list + self.staff_schedule_collection_list)

        for profile in unique_profiles:
            meta = profile.get("metadata", {})
            source_coll = profile.get("source_collection", "")
            
            if meta.get("full_name"):
                all_aliases.add(meta.get("full_name"))

            if any(s == source_coll for s in self.student_collection_list):
                student_filters = {
                    "program": meta.get("program") or meta.get("course"),
                    "year_level": meta.get("year_level") or meta.get("year"),
                    "section": meta.get("section")
                }
                if all(student_filters.values()):
                    all_related_schedules.extend(self.search_database(
                        filters=student_filters,
                        collection_filter=student_schedule_collections
                    ))
            
            elif any(s == source_coll for s in self.staff_collection_list):
                name_parts = set()
                if meta.get("full_name"):
                    cleaned = re.sub(aggressive_clean_pattern, '', meta.get("full_name"), flags=re.IGNORECASE)
                    name_parts.update(part for part in cleaned.split() if len(part) > 2)
                
                if name_parts:
                    regex_list = [re.compile(re.escape(part), re.IGNORECASE) for part in name_parts]
                    schedule_filters = {"$or": [
                        {"full_name": {"$in": regex_list}},
                        {"adviser_name": {"$in": regex_list}},
                        {"staff_name": {"$in": regex_list}}
                    ]}
                    all_related_schedules.extend(self.search_database(
                        filters=schedule_filters,
                        collection_filter=staff_schedule_collections
                    ))

        for sched in all_related_schedules:
            meta = sched.get("metadata", {})
            for key in ["full_name", "adviser_name", "staff_name"]:
                if meta.get(key):
                    all_aliases.add(meta.get(key))

        all_docs = unique_profiles + all_related_schedules
        final_unique_docs = list({doc.get("metadata", {}).get("schedule_id", doc.get("metadata", {}).get("student_id", doc.get("metadata", {}).get("faculty_id"))): doc for doc in all_docs}.values())

        primary_name = name
        if unique_profiles:
            primary_name = unique_profiles[0].get("metadata", {}).get("full_name", name)

        self.debug(f"   -> Entity resolved: Primary='{primary_name}', Aliases={all_aliases}, Found {len(final_unique_docs)} total docs.")
        
        return {
            "primary_name": primary_name,
            "aliases": list(all_aliases),
            "primary_document": final_unique_docs
        }


    # --- REPLACE THE CURRENT get_person_profile METHOD WITH THIS ---

    def get_person_profile(self, person_name: str) -> List[dict]:
        """
        Tool (UPGRADED V2 - Ambiguity Aware): Retrieves the main profile
        document for a specific person.
        
        If it finds profiles for multiple different people, it will
        withhold them and return a clarification signal.
        """
        self.debug(f"Running FOCUSED (V2 Ambiguity Aware) tool: get_person_profile for '{person_name}'")
        
        entity = self.resolve_person_entity(name=person_name)
        
        if not entity or not entity.get("primary_document"):
            return [{"status": "empty", "summary": f"I could not find a profile for anyone named '{person_name}'."}]

        all_found_docs = entity["primary_document"]

        # --- THIS IS THE NEW AMBIGUITY CHECK (from V6 schedule tool) ---
        profile_docs = []
        
        # Define all profile collections
        all_profile_collections = (
            self.student_collection_list +
            self.staff_collection_list
        )
        
        for doc in all_found_docs:
            source_coll = doc.get("source_collection", "")
            if any(s == source_coll for s in all_profile_collections):
                profile_docs.append(doc)

        # Count how many unique people we found profiles for
        unique_profile_ids = set()
        for doc in profile_docs:
            meta = doc.get("metadata", {})
            pid = meta.get("faculty_id", meta.get("student_id", meta.get("full_name")))
            if pid:
                unique_profile_ids.add(pid)
        
        # AMBIGUITY HIT: We found more than one person.
        if len(unique_profile_ids) > 1:
            self.debug(f"-> Ambiguity detected: Found {len(unique_profile_ids)} different people. Asking for clarification.")
            # Send ONLY the profiles and the signal.
            return profile_docs + [{
                "source_collection": "system_signal",
                "content": "Ambiguity detected",
                "metadata": {"status": "clarification_needed"}
            }]
        # --- END OF NEW CHECK ---

        # Success: We found one person (or 0 profiles).
        # Return all documents found (profiles + any related schedules).
        return all_found_docs



    # Add this new method anywhere inside the AIAnalyst class in analyst.py

    def handle_user_recognized_event(self, event_data: dict) -> str:
        """
        Handles the 'user_recognized' system event from the UI.
        Bypasses the Planner to directly fetch a profile and generate a personalized greeting.
        """
        self.debug(f"⚙️ Handling 'user_recognized' event: {event_data}")
        
        student_id = event_data.get("student_id")
        full_name = event_data.get("full_name")
        person_profile_docs = None

        if student_id:
            self.debug(f"-> Received student_id '{student_id}'. Using fast path.")

            person_profile_docs = self.get_data_by_id(pdm_id=student_id)

        # IF Faculty, A name requires validation.
        elif full_name:
            self.debug(f"-> Received full_name '{full_name}'. Using ambiguity-aware path.")
            # Using another tool to find all matches since faculty has no id
            entity = self.resolve_person_entity(name=full_name)
            
            # Only proceed if exactly ONE unique person is found.
            if entity and len(entity.get("primary_document", [])) == 1:
                self.debug("-> Found exactly one match for the name. Proceeding.")
                person_profile_docs = entity.get("primary_document")
            else:
                self.debug(f"-> Ambiguity detected or no match found for '{full_name}'. Falling back to generic greeting.")

        # If successfully found a unique profile, generate a personalized greeting.
        if person_profile_docs:
            # Prepare the context for the Synthesizer AI.
            context_for_greeting = json.dumps({
                "status": "success",
                "data": person_profile_docs
            }, indent=2)

            # Call the Synthesizer with the new personalized greeting prompt.
            final_greeting = self.synth_llm.execute(
                system_prompt="You are a friendly and welcoming AI assistant for PDM.",
                user_prompt=PROMPT_TEMPLATES["personalized_greeting_prompt"].format(context=context_for_greeting),
                phase="synth"
            )
            return final_greeting
        
        # If no unique profile was found, return a safe, generic greeting.
        else:
            return "Hello! Welcome to PDM. How can I assist you today?"
        
    def debug(self, *args):
        """Prints messages only if the analyst is in debug mode."""
        if self.debug_mode:
            print(*args)

    # In analyst.py
    def _is_query_complete_nlp(self, query: str) -> bool:
        """
        [UPGRADED v2] Uses SpaCy to validate sentence completeness, now with gibberish detection.
        """
        self.debug("Running NLP Completeness Validator...")
        q_lower = query.strip().lower()

        if not self.policy_engine.nlp or not q_lower:
            return True

        if len(q_lower.split()) == 1:
            if q_lower in ['hello', 'hi', 'hey', 'thanks', 'ok', 'yes', 'no', 'insights']:
                self.debug("Query validated as a complete single-word command/greeting.")
                return True
            else:
                self.debug(f"Query flagged as incomplete. Reason: It is a single, non-command word ('{q_lower}').")
                return False

        doc = self.policy_engine.nlp(q_lower)
        last_token = doc[-1]

        if last_token.pos_ in ['ADP', 'SCONJ']:
            self.debug(f"Query flagged as incomplete. Reason: Ends with '{last_token.text}' ({last_token.pos_}).")
            return False

        # --- NEW GIBBERISH DETECTION RULE ---
        # SpaCy tags unknown words with the Part-of-Speech tag 'X'.
        unknown_words = [token for token in doc if token.pos_ == 'X']
        if len(doc) > 0 and (len(unknown_words) / len(doc)) > 0.5:
            # If more than 50% of the words are unknown, flag it as incomplete/gibberish.
            self.debug(f"Query flagged as incomplete. Reason: High percentage of unknown words (gibberish).")
            return False
        # --- END OF NEW RULE ---

        self.debug("Query validated as a complete sentence.")
        return True
            
# File: backend/utils/ai_core/analyst.py

# --- Replace the entire _load_dynamic_examples method with this new version ---
# In backend/utils/ai_core/analyst.py

# In backend/utils/ai_core/analyst.py

# In backend/utils/ai_core/analyst.py

# In backend/utils/ai_core/analyst.py

# In backend/utils/ai_core/analyst.py

# --- REPLACE THIS ENTIRE METHOD ---

    def _load_dynamic_examples(self, query: str) -> str:
        """
        [UPGRADED - V4] Finds, ranks, and correctly formats abstract
        templates from MongoDB using a precise-intent-first strategy.
        """
        if not query:
            return ""
        
        # --- 1. Import lightweight, built-in text similarity library ---
        from difflib import SequenceMatcher

        # --- 2. Get fast, local intent prediction ---
        predicted_intent = self.policy_engine.get_intent(query)
        if not predicted_intent:
            predicted_intent = "unknown" 
        self.debug(f"Predicted intent for example retrieval (local): '{predicted_intent}'")

        try:
            candidates = []
            
            # --- 3. New Strategy: Search by INTENT first ---
            if predicted_intent != "unknown":
                self.debug(f"-> Searching examples by PRECISE INTENT: {predicted_intent}")
                candidates = list(self.dynamic_examples_collection.find(
                    {"intent": predicted_intent}
                ).limit(20)) # Get all examples for this tool

            # --- 4. Fallback Strategy: Text search if no intent ---
            if not candidates:
                self.debug("-> No specific intent or no examples found. Falling back to TEXT search.")
                candidates = list(self.dynamic_examples_collection.find(
                    {"$text": {"$search": query}},
                    {"score": {"$meta": "textScore"}}
                ).limit(20))

            if not candidates:
                self.debug("No relevant dynamic examples found via any method.")
                return ""

            # --- 5. Rank all candidates (from either search) ---
            ranked_candidates = []
            half_life_days = 30.0
            decay_rate = -0.693 / half_life_days
            now_aware = datetime.now(timezone.utc)

            for doc in candidates:
                intent_boost = 1.5 if doc.get("intent") == predicted_intent else 1.0
                
                # Calculate freshness
                last_used_aware = doc["last_used_at"].replace(tzinfo=timezone.utc)
                days_old = (now_aware - last_used_aware).total_seconds() / (60 * 60 * 24)
                freshness_score = math.exp(days_old * decay_rate) # 'math' is imported at top of file
                
                # Calculate relevance score
                relevance_score = 0.0
                if "score" in doc: 
                    # Use the score from $text search
                    relevance_score = doc["score"]
                else: 
                    # Manually score similarity for intent-based matches
                    # How similar is "WHO IS MARK GARCIA" to "WHO IS {PERSON_NAME}"?
                    relevance_score = SequenceMatcher(None, query, doc["user_pattern"]).ratio() * 10.0 # Scale to ~1-10

                doc["final_score"] = relevance_score * intent_boost * freshness_score
                ranked_candidates.append(doc)

            ranked_candidates.sort(key=lambda x: x["final_score"], reverse=True)
            examples_list = ranked_candidates[:3]
            
            retrieved_ids = [ex['_id'] for ex in examples_list]
            if retrieved_ids:
                self.dynamic_examples_collection.update_many(
                    {"_id": {"$in": retrieved_ids}},
                    {"$set": {"last_used_at": datetime.now(timezone.utc)}}
                )

            example_strings = []
            for example in examples_list:
                example_strings.append(
                    f"EXAMPLE (from memory):\n"
                    f"User Query: \"{example['user_pattern']}\"\n"
                    f"Your JSON Response:\n"
                    f"{json.dumps(example['plan_template'], indent=2, ensure_ascii=False)}"
                )
            
            self.debug(f"Loaded {len(example_strings)} relevant examples from memory using smart ranking.")
            return "\n---\n".join(example_strings)
            
        except Exception as e:
            self.debug(f"⚠️ Error during smart example loading: {e}. Falling back to simple text search.")
            # Your original fallback logic is good to keep as a final safety net
            try:
                examples_cursor = self.dynamic_examples_collection.find(
                    { "$text": { "$search": query } },
                    { "score": { "$meta": "textScore" } }
                ).sort([("score", { "$meta": "textScore" })]).limit(3)
                
                examples_list = list(examples_cursor)
                if not examples_list: return ""

                example_strings = []
                for example in examples_list:
                    example_strings.append(
                        f"EXAMPLE (from memory):\n"
                        f"User Query: \"{example['user_pattern']}\"\n"
                        f"Your JSON Response:\n"
                        f"{json.dumps(example['plan_template'], indent=2, ensure_ascii=False)}"
                    )
                return "\n---\n".join(example_strings)
            except Exception as fallback_e:
                self.debug(f"⚠️ Fallback search also failed: {fallback_e}")
                return ""

    
    # In backend/utils/ai_core/analyst.py

    # In backend/utils/ai_core/analyst.py


# In backend/utils/ai_core/analyst.py

    # --- REPLACE THIS ENTIRE METHOD ---

    def _save_dynamic_example(self, query: str, plan: dict, session: dict, outcome: str) -> Optional[str]:
        """
        [UPGRADED w/ HASHING V2] De-lexicalizes successful MULTI-STEP plans and saves them,
        using a unique hash of the *entire plan_template* for robust de-duplication.
        
        Returns the plan_hash if successful, otherwise None.
        """
        if not outcome.startswith("SUCCESS"):
            self.debug(f"Skipping memory save. Reason: Outcome was '{outcome}'.")
            return None
        
        try:
            plan_steps = plan.get("plan", [])
            if not plan_steps:
                self.debug("Could not extract a valid plan to save.")
                return None

            # --- THIS IS THE NEW LOGIC ---
            # 1. Gather ALL parameters from ALL steps (except finish_plan)
            # This creates a complete picture of all "entities" in the query.
            all_params = {}
            for step in plan_steps:
                tool_call = step.get("tool_call", {})
                if tool_call.get("tool_name") != "finish_plan":
                    all_params.update(tool_call.get("parameters", {}))
            
            # 2. Delexicalize the user query based on ALL found parameters
            # We create a "dummy" tool_call with all params to use the existing delexicalizer
            master_tool_call = {"tool_name": "multi_step_plan", "parameters": all_params}
            templates = self.policy_engine.delexicalize(query, master_tool_call)
            user_pattern = templates["user_pattern"]

            # 3. Delexicalize EACH STEP in the plan to create the full template
            full_plan_template = []
            intent = "unknown"
            for step in plan_steps:
                step_tool_call = step.get("tool_call", {})
                
                # Get the delexicalized *plan* (not the user_pattern)
                step_template = self.policy_engine.delexicalize(query, step_tool_call)["plan_template"]
                full_plan_template.append({"tool_call": step_template})
                
                # Set the intent to the first non-conversational tool
                if intent == "unknown" and step_tool_call.get("tool_name") not in ["finish_plan", "answer_conversational_query"]:
                    intent = step_tool_call.get("tool_name")
            
            # 4. Create a unique hash of the ENTIRE delexicalized plan
            canonical_plan_str = json.dumps(full_plan_template, sort_keys=True)
            plan_hash = hashlib.sha256(canonical_plan_str.encode('utf-8')).hexdigest()
            # --- END OF NEW LOGIC ---

            # 5. Check for duplicates using this new, reliable hash
            if self.dynamic_examples_collection.find_one({"plan_hash": plan_hash}):
                self.debug(f"Duplicate multi-step example plan hash found. Not saving to memory, but returning hash.")
                return plan_hash
                
            example_doc = {
                "user_pattern": user_pattern,
                "plan_template": full_plan_template, # <-- Save the full list
                "plan_hash": plan_hash,
                "intent": intent, # <-- Use the new intent
                "topic": session.get("conversation_summary", "general"),
                "quality_label": outcome,
                "created_at": datetime.now(timezone.utc),
                "last_used_at": datetime.now(timezone.utc)
            }
            
            self.dynamic_examples_collection.insert_one(example_doc)
            self.debug(f"✅ New abstract template saved to AI memory for pattern: '{user_pattern}'")
            
            return plan_hash
            
        except Exception as e:
            self.debug(f"⚠️ Error saving dynamic example: {e}")
            return None
    


    def _repair_json(self, text: str) -> Optional[dict]:
        """
        Extracts a valid JSON object from a string that may contain surrounding text or markdown.
        """
        if not text: return None
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m: return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        
    def _create_reverse_schema_map(self) -> dict:
        """
        Creates a mapping from common alternative field names (e.g., 'course', 'yr')
        to their standard equivalents (e.g., 'program', 'year_level').
        """
        mappings = {
            'program': ('course',),
            'year_level': ('year', 'yr', 'yearlvl'),
            'full_name': ('name', 'student_name'),
            'section': ('sec',),
            'adviser': ('advisor', 'faculty'),
            'student_id': ('stud_id', 'id', 'student_number'),
            'pdm_id': ('student_id', 'id', 'student_number')
        }
        reverse_map = {}
        for standard_name, original_names in mappings.items():
            for original_name in original_names:
                reverse_map[original_name] = standard_name
        return reverse_map


    def _normalize_schema(self, schema_dict: dict) -> dict:
        """
        Uses the reverse schema map to standardize field names in the database schema,
        making it easier for the AI to understand.
        """
        def std(field: str) -> str:
            return self.REVERSE_SCHEMA_MAP.get(field.lower(), field)
            
        norm = {}
        for coll, fields in schema_dict.items():
            norm[coll] = sorted(list({std(f) for f in fields}))
        return norm

    def _generate_db_schema(self):
        """
        [MODIFIED] Inspects the MongoDB collections to create a simplified, human-readable schema summary.
        This version is adapted to handle the output format of the MongoCollectionAdapter.
        """
        if not self.collections:
            self.db_schema_summary = "No collections loaded."
            return

        raw = {}
        # This function no longer uses value hints as it's less efficient with MongoDB's flat structure
        # and we already get this data in the pre-loading step.

        for name, coll_adapter in self.collections.items():
            try:
                # Use the adapter's get() method to fetch a sample
                sample = coll_adapter.get(limit=1)
                
                # Correctly extract the metadata from the adapter's nested list format
                metadatas_list = (sample.get("metadatas") or [[]])[0]

                if metadatas_list:
                    # Get keys from the first document's metadata
                    raw[name] = list(metadatas_list[0].keys())
                else:
                    raw[name] = []
            except Exception as e:
                self.debug(f"Schema inspect failed for {name}: {e}")
                raw[name] = []

        norm = self._normalize_schema(raw)
        
        parts = []
        for name, fields in norm.items():
            # Clean up the fields for better readability in the prompt
            fields_to_show = sorted([f for f in fields if not f.startswith('_') and f != 'content'])
            parts.append(f"- {name}: {fields_to_show}")

        self.db_schema_summary = "\n".join(parts)
        self.debug("DB Schema for planner:\n", self.db_schema_summary)

        # (Add this new method anywhere in the AIAnalyst class)


# In backend/utils/ai_core/analyst.py

    # --- REPLACE THIS ENTIRE METHOD ---

    def _clean_documents_for_synthesizer(self, docs: List[dict]) -> List[dict]:
        """
        [CORRECTED V3] Prepares documents for the Synthesizer.
        This version fixes the 'grouped_students' hallucination by
        pre-parsing the student list into a lightweight, name-only list.
        """
        cleaned_docs = []
        blacklist = getattr(self, 'METADATA_FIELD_BLACKLIST', set())
             
        for doc in docs:
            source_coll = doc.get("source_collection")

            # --- THIS IS THE FIX ---
            if source_coll == "grouped_students":
                
                # We will NOT recursively clean. We will extract ONLY the names.
                # This makes the context for the Synthesizer extremely clean and small.
                original_student_list = doc.get("students", [])
                
                # Create a new, clean list of student objects
                # containing ONLY the metadata the Synthesizer needs.
                cleaned_student_list = []
                for student_doc in original_student_list:
                    student_meta = student_doc.get("metadata", {})
                    # Add only students that actually have a name
                    if student_meta.get("full_name"):
                        cleaned_student_list.append({
                            "metadata": {
                                "full_name": student_meta.get("full_name")
                            }
                        })

                cleaned_docs.append({
                    "source_collection": "grouped_students",
                    "group_name": doc.get("group_name"),
                    # NEW: Pass the pre-calculated count directly
                    "student_count": len(cleaned_student_list), 
                    # NEW: Pass the lightweight name-only list
                    "students": cleaned_student_list 
                })
                continue # Skip the rest of the loop for this special doc
            # --- END OF FIX ---

            # --- This is the original logic (which is correct for normal docs) ---
            rich_content = doc.get("formatted_text")
            if not rich_content:
                rich_content = doc.get("metadata", {}).get("formatted_text")
            final_content = rich_content if rich_content else doc.get("content")
            # --- END OF FIX ---

            new_doc = {
                "source_collection": doc.get("source_collection"),
                "content": final_content 
            }
            
            original_metadata = doc.get("metadata", {})
            cleaned_metadata = {}
            if original_metadata:
                for key, value in original_metadata.items():
                    if key not in blacklist:
                        cleaned_metadata[key] = value
            
            new_doc["metadata"] = cleaned_metadata
            cleaned_docs.append(new_doc)
            
        return cleaned_docs


    
    def _resolve_placeholders(self, params: dict, step_results: dict) -> dict:
        """
        [CORRECTED V2] Recursively searches for and replaces placeholders
        (e.g., '$program_from_step_1') in a step's parameters with actual
        values from the results of previous steps.
        
        This version returns the RAW value, not a normalized filter.
        """
        # Deep copy to avoid modifying the original plan structure
        resolved_params = json.loads(json.dumps(params))

        # Map standard field names to their original variants
        forward_map = {}
        for original, standard in self.REVERSE_SCHEMA_MAP.items():
            forward_map.setdefault(standard, []).append(original)

        # This helper function is no longer needed here, as we return raw values.
        # def normalize_for_search(key: str, value: Any): ...

        def resolve(obj):
            if isinstance(obj, dict):
                for k, v_item in list(obj.items()):
                    obj[k] = resolve(v_item)
            elif isinstance(obj, list):
                for i, item in enumerate(list(obj)):
                    obj[i] = resolve(item)
            elif isinstance(obj, str) and obj.startswith('$'):
                parts = obj.strip('$').split('_from_step_')
                if len(parts) == 2:
                    key_to_find, step_num_str = parts
                    try:
                        step_num = int(step_num_str)
                    except ValueError:
                        self.debug(f"   -> Invalid placeholder format: {obj}")
                        return obj # Not a valid placeholder

                    self.debug(f"   -> Resolving placeholder: looking for '{key_to_find}' in results of step {step_num}")
                    if step_num in step_results and step_results[step_num]:
                        step_result = step_results[step_num]
                        
                        if isinstance(step_result, dict):
                            # This handles simple dict returns
                            if key_to_find in step_result:
                                return step_result[key_to_find]
                        
                        elif isinstance(step_result, list) and len(step_result) > 0:
                            # --- START OF FIX ---
                            # Iterate through ALL documents in the step result to find the key
                            for doc in step_result:
                                metadata = doc.get("metadata", {})
                                if not metadata:
                                    continue

                                # 1. Check for the exact key
                                if key_to_find in metadata:
                                    self.debug(f"   -> Found value '{metadata[key_to_find]}' for '{key_to_find}' in metadata.")
                                    return metadata[key_to_find] # <-- FIX: Return raw value

                                # 2. Check for aliased keys
                                for original_key in forward_map.get(key_to_find, []):
                                    if original_key in metadata:
                                        self.debug(f"   -> Found value '{metadata[original_key]}' for '{key_to_find}' (as alias '{original_key}') in metadata.")
                                        return metadata[original_key] # <-- FIX: Return raw value
                            
                            # If we looped through all docs and found nothing:
                            self.debug(f"   -> FAILED to find '{key_to_find}' in any of the {len(step_result)} documents from step {step_num}.")
                            # --- END OF FIX ---

            return obj # Return the original placeholder if not found
        
        return resolve(resolved_params)
    
    def analyze_query_intent(self, query):
        """Enhanced query analysis with better person name extraction"""
        query_upper = query.upper()
        intent = {
            'intent': 'general',
            'target_course': None,
            'target_year': None,
            'target_section': None,
            'target_person': None,
            'target_subject': None,
            'data_type': None,
            'specificity': 'medium',
            'query': query
        }
        
        # ENHANCED DETECTION 1: Academic subject detection (universal patterns)
        if re.search(r'\b[A-Z]{2,5}\s*\d{3}[A-Z]?\b', query_upper):  # Any subject code pattern
            subject_match = re.search(r'\b[A-Z]{2,5}\s*\d{3}[A-Z]?\b', query_upper)
            intent['target_subject'] = subject_match.group(0)
            intent['intent'] = 'subject_search'
            intent['data_type'] = 'schedule'
            print(f"   Detected subject search: {intent['target_subject']}")
            return intent
        
        # ENHANCED DETECTION 2: "WHO IS" pattern with better name extraction
        if 'WHO IS' in query_upper:
            # Extract name after "WHO IS"
            name_part = query_upper.split('WHO IS', 1)[1].strip()
            # Remove question mark and clean the name
            name_part = name_part.rstrip('?').strip()
            if name_part:
                intent['target_person'] = name_part.title()
                intent['intent'] = 'person_search'
                print(f"   Detected person search: {intent['target_person']}")
                return intent
        
        # ENHANCED DETECTION 3: Faculty/Title detection with better patterns
        faculty_patterns = [
            r'\b(DR\.?\s+[A-Z][A-Za-z]+)\b',  # Dr. Smith
            r'\b(PROF\.?\s+[A-Z][A-Za-z]+)\b',  # Prof. Johnson
            r'\b(MR\.?\s+[A-Z][A-Za-z]+)\b',   # Mr. Davis
            r'\b(MS\.?\s+[A-Z][A-Za-z]+)\b',   # Ms. Wilson
            r'\b(MRS\.?\s+[A-Z][A-Za-z]+)\b', # Mrs. Brown
        ]
        
        for pattern in faculty_patterns:
            match = re.search(pattern, query_upper)
            if match:
                intent['target_person'] = match.group(1).replace('.', '. ').title()
                intent['intent'] = 'person_search'  # Changed from faculty_search to person_search
                intent['data_type'] = 'schedule'
                print(f"   Detected faculty/adviser search: {intent['target_person']}")
                return intent
        
        # ENHANCED DETECTION 4: Simple name detection (improved)
        # Look for capitalized names that might be faculty or students
        name_patterns = [
            r'\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b',  # First Last
            r'\b([A-Z][a-z]+)\b(?=\s*$)',        # Single name at end
        ]
        
        for pattern in name_patterns:
            matches = re.findall(pattern, query)
            if matches:
                # Filter out common non-name words
                non_names = ['YEAR', 'COURSE', 'SECTION', 'STUDENT', 'FACULTY', 'SCHEDULE', 'CLASS']
                for match in matches:
                    if match.upper() not in non_names and len(match) > 2:
                        intent['target_person'] = match.title()
                        intent['intent'] = 'person_search'
                        print(f"   Detected name search: {intent['target_person']}")
                        return intent
        
        # ENHANCED DETECTION 5: Course program detection (universal patterns)
        if re.search(r'\b(BS|AB|B)[A-Z]{2,4}\b', query_upper):
            course_match = re.search(r'\b(BS|AB|B)[A-Z]{2,4}\b', query_upper)
            intent['target_course'] = course_match.group(0)
            intent['intent'] = 'course_specific'
        
        # ENHANCED DETECTION 6: Year level detection (universal patterns)
        if re.search(r'\b([1-4])(?:ST|ND|RD|TH)?\s*YEAR\b', query_upper):
            year_match = re.search(r'\b([1-4])(?:ST|ND|RD|TH)?\s*YEAR\b', query_upper)
            intent['target_year'] = year_match.group(1)
            intent['intent'] = 'year_specific'
        elif re.search(r'\bYEAR\s*([1-4])\b', query_upper):
            year_match = re.search(r'\bYEAR\s*([1-4])\b', query_upper)
            intent['target_year'] = year_match.group(1)
            intent['intent'] = 'year_specific'
        
        # ENHANCED DETECTION 7: Section detection (universal patterns)
        if re.search(r'\bSECTION\s*([A-Z0-9]+)\b', query_upper):
            section_match = re.search(r'\bSECTION\s*([A-Z0-9]+)\b', query_upper)
            intent['target_section'] = section_match.group(1)
            intent['intent'] = 'section_specific'
        
        # ENHANCED DETECTION 8: Schedule context detection
        schedule_keywords = ['SCHEDULE', 'COR', 'CLASS', 'SUBJECT', 'UNIT', 'COURSE', 'TIME', 'ROOM']
        if any(keyword in query_upper for keyword in schedule_keywords):
            intent['data_type'] = 'schedule'
            intent['intent'] = 'schedule_search'
        
        # ENHANCED SPECIFICITY CALCULATION
        specific_elements = sum([
            1 for x in [intent['target_course'], intent['target_year'], 
                    intent['target_section'], intent['target_person'], intent['target_subject']] if x
        ])
        
        if specific_elements >= 3:
            intent['specificity'] = 'high'
        elif specific_elements >= 1:
            intent['specificity'] = 'medium'
        else:
            intent['specificity'] = 'low'
        
        return intent

    def determine_search_strategy(self, query_intent):
        """Universal smart search strategy determination"""
        
        # Base universal strategy
        strategy = {
            'type': 'balanced',
            'broad': True,
            'threshold': 30
        }
        
        # SMART STRATEGY: Adjust based on query characteristics
        
        # High specificity = precise search regardless of intent type
        if query_intent['specificity'] == 'high':
            strategy = {
                'type': 'precise',
                'broad': False,
                'threshold': 70
            }
        
        # Person search = lower threshold to catch faculty names
        elif query_intent['intent'] == 'person_search':
            strategy = {
                'type': 'person_focused',
                'broad': False,
                'threshold': 25  # Lower threshold from 40 to 25 for person searches
            }
        
        # Medium specificity with clear target = focused search
        elif query_intent['specificity'] == 'medium' and any([
            query_intent['target_person'], 
            query_intent['target_subject'],
            query_intent['target_course']
        ]):
            strategy = {
                'type': 'focused',
                'broad': False,
                'threshold': 50
            }
        
        # Low specificity = broader search with lower threshold
        elif query_intent['specificity'] == 'low':
            strategy = {
                'type': 'broad',
                'broad': True,
                'threshold': 25
            }
        
        return strategy

    def build_smart_filters(self, query_intent, collection_name):
        """Build dynamic filters based on AI analysis"""
        where_clause = {}
        
        # Only apply filters if we have specific targets
        if query_intent['target_course']:
            where_clause['course'] = query_intent['target_course']
        
        if query_intent['target_year']:
            where_clause['year_level'] = query_intent['target_year']
        
        if query_intent['target_section']:
            where_clause['section'] = query_intent['target_section']
        
        # Collection-specific filtering
        if query_intent['data_type']:
            if query_intent['data_type'] == 'student' and 'faculty' in collection_name:
                return {'impossible_filter': 'skip'}  # Skip this collection
            elif query_intent['data_type'] == 'faculty' and 'student' in collection_name:
                return {'impossible_filter': 'skip'}
            elif query_intent['data_type'] == 'schedule' and 'student' in collection_name:
                return {'impossible_filter': 'skip'}
        
        return where_clause


    def calculate_ai_relevance(self, query_intent, document, metadata, chroma_distance):
        """Enhanced relevance calculation with better person name matching"""
        score = 0
        doc_upper = document.upper()

        # Convert ChromaDB distance to semantic score
        semantic_base_score = max(0, 70 - (chroma_distance * 2))
        score += semantic_base_score

        # ENHANCED Subject search scoring
        if query_intent['target_subject']:
            target_subject_upper = query_intent['target_subject'].upper()
            
            if target_subject_upper in doc_upper:
                score += 40
            
            subject_patterns = [
                rf'\b{re.escape(target_subject_upper)}\b',
                rf'{re.escape(target_subject_upper)}',
                rf'{re.escape(target_subject_upper[:-1])}',
            ]
            
            for pattern in subject_patterns:
                if re.search(pattern, doc_upper):
                    score += 35
                    break

        # ENHANCED Person search scoring with much better faculty detection
        if query_intent['target_person']:
            target_person_upper = query_intent['target_person'].upper()
            
            print(f"Looking for person: '{target_person_upper}' in document")
            
            # ENHANCED: Handle titles like "DR. SMITH" -> also search for "SMITH"
            name_parts = []
            if target_person_upper.startswith(('DR.', 'PROF.', 'MR.', 'MS.', 'MRS.')):
                # Extract the actual name without title
                title_removed = re.sub(r'^(DR\.?|PROF\.?|MR\.?|MS\.?|MRS\.?)\s*', '', target_person_upper).strip()
                name_parts = [target_person_upper, title_removed]  # Search for both full and name-only
            else:
                name_parts = [target_person_upper]
            
            found_match = False
            
            for search_name in name_parts:
                if not search_name:
                    continue
                    
                print(f"Searching for: '{search_name}'")
                
                # Very high boost for exact matches in faculty metadata
                if metadata.get('full_name') and search_name in metadata['full_name'].upper():
                    score += 80
                    found_match = True
                    print(f"Found in full_name metadata: +80")
                elif metadata.get('surname') and search_name in metadata['surname'].upper():
                    score += 75
                    found_match = True
                    print(f"Found in surname metadata: +75")
                elif metadata.get('first_name') and search_name in metadata['first_name'].upper():
                    score += 75
                    found_match = True
                    print(f"Found in first_name metadata: +75")
                
                # ENHANCED: Check adviser field specifically for COR schedules
                if metadata.get('adviser') and search_name in metadata['adviser'].upper():
                    score += 90  # Higher score for adviser matches
                    found_match = True
                    print(f" Found in adviser metadata: +90")
                
                # High boost for names in document content
                if search_name in doc_upper:
                    score += 60
                    found_match = True
                    print(f" Found in document content: +60")
                
                # Check for faculty-specific context
                if any(term in doc_upper for term in ['FACULTY', 'PROFESSOR', 'INSTRUCTOR', 'TEACHER', 'ADVISER', 'ADVISOR']):
                    if search_name in doc_upper:
                        score += 70
                        found_match = True
                        print(f" Found in faculty context: +70")
                
                # Enhanced partial name matching - MORE AGGRESSIVE
                individual_name_parts = search_name.split()
                partial_matches = 0
                for part in individual_name_parts:
                    if len(part) > 2:
                        # Check document content
                        if part in doc_upper:
                            partial_matches += 1
                            score += 35
                            found_match = True
                            print(f"Partial match '{part}' in document: +35")
                        
                        # Check metadata fields more thoroughly
                        for field in ['full_name', 'surname', 'first_name', 'adviser']:
                            if metadata.get(field) and part in metadata[field].upper():
                                partial_matches += 1
                                score += 40
                                found_match = True
                                print(f"Partial match '{part}' in {field}: +40")
                                break
                
                # Boost score if multiple name parts match
                if partial_matches > 1:
                    score += 25
                    found_match = True
                    print(f" Multiple name parts matched: +25")
                
                # If we found a match with this search term, we can break
                if found_match:
                    break
            
            # Special boost for single name searches in faculty context
            if len(target_person_upper.split()) == 1 and any(term in doc_upper for term in ['FACULTY', 'PROFESSOR', 'TEACHING', 'ADVISER']):
                score += 30
                print(f" Single name in faculty context: +30")

        # Rest of the scoring logic remains the same...
        if query_intent['target_course'] and query_intent['target_course'] in doc_upper:
            score += 25
        
        if query_intent['target_year'] and str(query_intent['target_year']) in doc_upper:
            score += 20
        
        if query_intent['target_section'] and query_intent['target_section'] in doc_upper:
            score += 20

        if metadata:
            if query_intent['target_course'] and metadata.get('course') and query_intent['target_course'] in metadata['course'].upper():
                score += 15
            if query_intent['target_year'] and str(metadata.get('year_level')) == str(query_intent['target_year']):
                score += 15
            if query_intent['target_section'] and metadata.get('section') and query_intent['target_section'] in metadata['section'].upper():
                score += 15
        
        final_score = max(0, min(100, score))
        self.debug(f"🔍 Final relevance score: {final_score} (raw: {score})")
        return final_score

    def rank_and_filter_results(self, results, query_intent, max_results):
        """AI-powered ranking and filtering of results"""
        
        # FIX: Lower minimum relevance and add debug info
        if query_intent['specificity'] == 'high':
            min_relevance = 8
        elif query_intent['intent'] == 'person_search':
            min_relevance = 5  # Lower threshold for person searches
        else:
            min_relevance = 5  # Lower default threshold
        
        print(f" Filtering {len(results)} results with min_relevance: {min_relevance}")
        
        # Remove results that don't meet minimum relevance
        filtered_results = []
        for r in results:
            print(f" Result relevance: {r['relevance']} (min: {min_relevance})")
            if r['relevance'] >= min_relevance:
                filtered_results.append(r)
            else:
                print(f" Filtered out result with relevance {r['relevance']}")
        
        print(f" After filtering: {len(filtered_results)} results remain")
        
        # Sort by relevance score
        filtered_results.sort(key=lambda x: x['relevance'], reverse=True)
        
        # Apply intelligent deduplication if needed
        if query_intent['intent'] == 'person_search':
            # For person searches, prioritize unique individuals
            seen_names = set()
            unique_results = []
            for result in filtered_results:
                doc_upper = result['content'].upper()
                # Extract name from document
                if 'FULL NAME:' in doc_upper:
                    name_start = doc_upper.find('FULL NAME:') + len('FULL NAME:')
                    name_line = doc_upper[name_start:doc_upper.find('\n', name_start) if '\n' in doc_upper[name_start:] else len(doc_upper)].strip()
                    if name_line not in seen_names:
                        seen_names.add(name_line)
                        unique_results.append(result)
                else:
                    # If no FULL NAME found, include the result
                    unique_results.append(result)
            filtered_results = unique_results
        
        final_results = filtered_results[:max_results]
        print(f" Final results count: {len(final_results)}")
        return final_results

    def explain_match(self, query_intent, document, metadata):
        """Explain why this result matches the query"""
        reasons = []
        
        if query_intent['target_course'] and metadata.get('course') == query_intent['target_course']:
            reasons.append(f"Matches course: {query_intent['target_course']}")
        
        if query_intent['target_year'] and metadata.get('year_level') == query_intent['target_year']:
            reasons.append(f"Matches year: {query_intent['target_year']}")
        
        if query_intent['target_section'] and metadata.get('section') == query_intent['target_section']:
            reasons.append(f"Matches section: {query_intent['target_section']}")
        
        return " | ".join(reasons) if reasons else "General relevance match"
    
# In backend/utils/ai_core/analyst.py
    # --- REPLACE THE ENTIRE 'search_database' METHOD WITH THIS ---

    def search_database(self, query_text: Optional[str] = None, query: Optional[str] = None,
                    filters: Optional[dict] = None, document_filter: Optional[dict] = None,
                    collection_filter: Optional[str] = None, n_results: int = 200) -> List[dict]:
        """
        The core database search function. It can handle semantic queries, metadata filters,
        and document content filters, with robust normalization for filter values.
        [CORRECTED to handle string-based year levels like '2nd year']
        """
        qt = query or query_text
        final_query_texts: Optional[List[str]] = None
        if isinstance(qt, list):
            final_query_texts = qt
        elif isinstance(qt, str):
            final_query_texts = [qt]

        self.debug(f"search_database | query(s)='{final_query_texts}' | filters={filters} | doc_filter={document_filter} | coll_filter='{collection_filter}'")
        all_hits: List[dict] = []

        where_clause: Optional[dict] = None
        if filters:
            if '$or' in filters and isinstance(filters.get('$or'), list):
                where_clause = filters
            else:
                COURSE_ALIASES = {
                    "BSCS": ["BSCS", "BS COMPUTER SCIENCE", "BS Computer Science"],
                    "BSTM": ["BSTM", "BS TOURISM MANAGEMENT", "BS Tourism Management"],
                    "BSOA": ["BSOA", "BS OFFICE ADMINISTRATION", "BS Office Administration" , "BSOFFICE"],
                    "BECED": ["BECED", "BACHELOR OF EARLY CHILDHOOD EDUCATION", "Bachelor of Early Childhood Education"],
                    "BSIT": ["BSIT", "BS INFORMATION TECHNOLOGY", "BS Information Technology" , "BS INFORMATION", "BSINFORMATION"],
                    "BSHM": ["BSHM", "BS HOSPITALITY MANAGEMENT", "BS Hospitality Management"],
                    "BTLE": ["BTLE", "BACHELOR OF TECHNOLOGY AND LIVELIHOOD EDUCATION", "Bachelor of Technology and Livelihood Education"]
                }

                and_conditions: List[dict] = []
                for k, v in filters.items():
                    standard_key = self.REVERSE_SCHEMA_MAP.get(k, k)
                    possible_keys = list(set([standard_key] + [orig for orig, std in self.REVERSE_SCHEMA_MAP.items() if std == standard_key]))

                    filter_for_this_key = None

                    if standard_key == "program":
                        value_from_placeholder = v.get('$in') if isinstance(v, dict) else [v]
                        all_aliases = set(value_from_placeholder)
                        for item in value_from_placeholder:
                            item_upper = str(item).upper()
                            for alias_key, alias_list in COURSE_ALIASES.items():
                                if item_upper == alias_key or item_upper in [a.upper() for a in alias_list]:
                                    all_aliases.update(alias_list)
                                    break
                        or_list = [{key: {"$in": list(all_aliases)}} for key in possible_keys]
                        filter_for_this_key = {"$or": or_list} if len(or_list) > 1 else or_list[0]

                    # --- START OF YEAR_LEVEL FIX ---
                    elif standard_key == "year_level":
                        or_conditions_for_year = []
                        
                        # Extract the first digit from the value (e.g., "2nd year" -> "2")
                        year_num_str = None
                        match = re.search(r'\d+', str(v))
                        if match:
                            year_num_str = match.group(0)
                        
                        if year_num_str:
                            # Search for the string number (e.g., "2")
                            year_variations_str = {year_num_str, f"Year {year_num_str}"}
                            for key in possible_keys:
                                or_conditions_for_year.append({key: {"$in": list(year_variations_str)}})
                            
                            # Also search for the integer number (e.g., 2)
                            try:
                                year_int = int(year_num_str)
                                for key in possible_keys:
                                    or_conditions_for_year.append({key: {"$eq": year_int}})
                            except (ValueError, TypeError):
                                pass # This is fine, we'll just search by string
                        else:
                            # Fallback if no digit is found (e.g., "First Year")
                            year_variations_str = {str(v), f"Year {v}"}
                            for key in possible_keys:
                                or_conditions_for_year.append({key: {"$in": list(year_variations_str)}})
                        
                        filter_for_this_key = {"$or": or_conditions_for_year}
                    # --- END OF YEAR_LEVEL FIX ---

                    elif standard_key == "section":
                        section_value = str(v)
                        match = re.search(r'\b([A-Z0-9]+)\b$', section_value, re.IGNORECASE)
                        
                        if match:
                            section_letter = match.group(1).upper()
                            section_variations = {section_letter, section_value.upper()}
                            or_list = [{key: {"$in": list(section_variations)}} for key in possible_keys]
                            filter_for_this_key = {"$or": or_list}
                        else:
                            or_list = [{key: section_value} for key in possible_keys]
                            filter_for_this_key = {"$or": or_list}

                    else: # Generic logic for all other filters
                        query_value = v
                        if isinstance(v, str):
                            query_value = {"$regex": f"^{re.escape(v)}$", "$options": "i"}

                        if len(possible_keys) > 1:
                            or_list = [{key: query_value} for key in possible_keys]
                            filter_for_this_key = {"$or": or_list}
                        else:
                            filter_for_this_key = {possible_keys[0]: query_value}

                    and_conditions.append(filter_for_this_key)

                if len(and_conditions) > 1:
                    where_clause = {"$and": and_conditions}
                elif and_conditions:
                    where_clause = and_conditions[0]

        if not final_query_texts and not where_clause and not document_filter:
            final_query_texts = ["*"]
            self.debug("No query or filters provided. Using wildcard '*' to retrieve all documents.")
        elif (where_clause or document_filter) and not final_query_texts:
            final_query_texts = ["*"]
            self.debug("No query text provided with filters. Using wildcard '*' search.")

        if self.debug_mode:
            try: self.debug("Final where_clause:", json.dumps(where_clause, ensure_ascii=False))
            except Exception: self.debug("Final where_clause (non-serializable):", where_clause)

        for name, coll in self.collections.items():
            if collection_filter and isinstance(collection_filter, str) and name not in collection_filter:
                continue
            try:
                res = coll.query(
                    query_texts=final_query_texts, n_results=n_results,
                    where=where_clause, where_document=document_filter
                )
                docs = (res.get("documents") or [[]])[0]
                metas = (res.get("metadatas") or [[]])[0]
                for i, doc in enumerate(docs):
                    all_hits.append({
                        "source_collection": name, "content": doc,
                        "metadata": metas[i] if i < len(metas) else {}
                    })
            except Exception as e:
                self.debug(f"Query error in {name}: {e}")
                if "hnsw segment reader" in str(e):
                    self.corruption_warnings.add(name)

        return all_hits
    

    def _translate_or_filter_for_mongo(self, filters: dict) -> dict:
        """Helper to translate complex $or filters with aliases."""
        or_conditions = filters.get('$or', [])
        mongo_or_list = []
        for condition in or_conditions:
            if not isinstance(condition, dict): continue
            for k, v in condition.items():
                standard_key = self.REVERSE_SCHEMA_MAP.get(k, k)
                db_key = standard_key
                if standard_key == 'program': db_key = 'course'
                if standard_key == 'year_level': db_key = 'year'
                mongo_or_list.append({db_key: v})
        return {"$or": mongo_or_list} if mongo_or_list else {}
    

    def _validate_plan(self, plan_json: Optional[dict]) -> tuple[bool, Optional[str]]:
        """
        Validates the structure and content of the planner's JSON output before execution.
        Returns a tuple: (is_valid: bool, error_message: Optional[str]).
        """
        if not isinstance(plan_json, dict):
            return False, "The plan is not a valid JSON object (expected a dictionary)."

        plan_list = plan_json.get("plan")
        if not isinstance(plan_list, list):
            return False, "The plan is missing a 'plan' key with a list of steps."
            
        if not plan_list:
            return False, "The plan is empty and contains no steps."

        for i, step in enumerate(plan_list):
            step_num = i + 1
            if not isinstance(step, dict):
                return False, f"Step {step_num} is not a valid object (expected a dictionary)."

            tool_call = step.get("tool_call")
            if not isinstance(tool_call, dict):
                return False, f"Step {step_num} is missing or has an invalid 'tool_call' section."

            tool_name = tool_call.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                return False, f"Step {step_num} is missing a 'tool_name'."

            if tool_name == "search_database":
                params = tool_call.get("parameters")
                if not isinstance(params, dict) and params is not None:
                    return False, f"Step {step_num} has invalid 'parameters' (expected a dictionary)."
                
                if isinstance(params, dict):
                    filters = params.get("filters")
                    if filters is not None and not isinstance(filters, dict):
                        return False, f"Step {step_num} has an invalid 'filters' parameter (expected a dictionary)."
                    if isinstance(filters, dict) and "$or" in filters:
                        or_conditions = filters.get("$or")
                        if isinstance(or_conditions, list):
                            for condition_index, condition in enumerate(or_conditions):
                                if isinstance(condition, dict) and len(condition) > 1:
                                    return False, (f"Step {step_num} contains an invalid complex '$or' filter. "
                                                   f"Each condition inside '$or' must have only one key.")

                    doc_filter = params.get("document_filter")
                    if doc_filter is not None and not isinstance(doc_filter, dict):
                        return False, f"Step {step_num} has an invalid 'document_filter' parameter (expected a dictionary)."
                    if isinstance(doc_filter, dict) and "$contains" in doc_filter and not isinstance(doc_filter["$contains"], str):
                        return False, f"Step {step_num} has an invalid value for '$contains' (expected a string)."

                    # Auto-rewrite unsupported operators like $gt/$lt to prevent errors
                    if isinstance(filters, dict):
                        unsupported_ops = {"$gt", "$lt", "$gte", "$lte"}
                        bad_keys = [k for k, v in filters.items() if isinstance(v, dict) and any(op in v for op in unsupported_ops)]
                        if bad_keys:
                            for key in bad_keys:
                                filters.pop(key, None)
                            if "sort" in params: params.pop("sort")
                            if "limit" in params: params.pop("limit")
                            self.debug(f"Step {step_num}: Removed unsupported operators ($gt/$lt) from filters.")

            elif tool_name not in self.available_tools and tool_name != "finish_plan":
                return False, f"Step {step_num} uses an unknown tool: '{tool_name}'."
        
        last_step = plan_list[-1]
        if not (isinstance(last_step, dict) and last_step.get("tool_call", {}).get("tool_name") == "finish_plan"):
            return False, "The plan must conclude with a 'finish_plan' step."

        return True, None

    def _execute_smart_fallback_search(self, query: str) -> List[dict]:
        """
        A dedicated, AI-powered fallback search that uses intent analysis and relevance
        scoring to find the best possible matches when a primary tool fails.
        """
        self.debug("🚀 Activating Smart Fallback Search...")
        
        # 1. Analyze intent and determine search strategy using your helpers
        query_intent = self.analyze_query_intent(query)
        search_strategy = self.determine_search_strategy(query_intent)
        self.debug(f"   -> Fallback Strategy: {search_strategy['type']} | Threshold: {search_strategy['threshold']}")

        all_results = []
        for name, collection_obj in self.collections.items():
            try:
                where_clause = self.build_smart_filters(query_intent, name)
                if where_clause and 'impossible_filter' in where_clause:
                    continue

                results = collection_obj.query(
                    query_texts=[query],
                    n_results=50, # Retrieve a large pool for re-ranking
                    where=where_clause if where_clause else None
                )

                # 2. Score and collect results that meet the dynamic threshold
                if results.get("documents") and results["documents"][0]:
                    for i, doc in enumerate(results["documents"][0]):
                        metadata = results["metadatas"][0][i]
                        distance = results["distances"][0][i] if results.get("distances") else 1.0

                        relevance_score = self.calculate_ai_relevance(query_intent, doc, metadata, distance)

                        if relevance_score >= search_strategy['threshold']:
                            all_results.append({
                                "source_collection": name,
                                "content": doc,
                                "metadata": metadata,
                                "relevance": relevance_score # Keep the score for ranking
                            })
            except Exception as e:
                self.debug(f"   -> Smart search error in {name}: {e}")
                if "hnsw segment reader" in str(e):
                    self.corruption_warnings.add(name)
        
        # 3. Rank all collected results by their smart score
        self.debug(f"   -> Re-ranking {len(all_results)} candidates from smart search.")
        sorted_results = sorted(all_results, key=lambda x: x.get('relevance', 0), reverse=True)
        
        return sorted_results
    


    # backend/utils/ai_core/analyst.py
# Find the existing 'execute_reasoning_plan' method and replace it with this entire block:



# --- REPLACE THIS ENTIRE METHOD ---
    def execute_reasoning_plan(self, query: str, session: dict) -> tuple[str, Optional[dict], List[dict]]:
        """
        [UPGRADED WITH OFFLINE MODE] The main orchestration method.
        - If 'offline', it runs a simple Planner -> Synth loop.
        - If 'online', it first runs a "mini-LLM" triage to classify
          the query, handles ambiguity, and uses chat history.
        """
        start_time = time.time()
        start_datetime = datetime.now(timezone.utc)
        
        planner_duration = 0.0
        retrieval_duration = 0.0
        synth_duration = 0.0
        plan_hash = None
        
        context = session.get("structured_context", {})

        # --- NEW OFFLINE/ONLINE LOGIC ---
        if self.execution_mode == 'offline':
            # --- START OFFLINE EXECUTION PATH ---
            # This path skips Triage, History, and Coreference AI steps.
            self.debug("Offline mode: Skipping Triage, History, and Coreference AI steps.")
            chat_history = [] # No history for offline mode
            
            plan_json = None
            final_context = {}
            error_msg = None
            results_count = 0
            
            outcome = "FAIL_UNKNOWN"
            execution_mode = "primary"
            collected_docs = []

            try:
                max_retries = 5
                planner_start_time = time.time()
                
                # No Coreference-to-Parameter Injection
                
                filters_cleared_on_retry = False # Not really used, but kept for consistency

                for attempt in range(max_retries):
                    self.debug(f"Planner Attempt {attempt + 1}/{max_retries}...")
                    
                    # --- Simplified Prompt Generation (Offline) ---
                    self.debug("-> Using 'Offline Planner Prompt' (no context, no examples).")
                    dynamic_examples = "" # No examples for offline
                    
                    # Use a minimal, empty context
                    planner_context = {"current_topic": "None.", "active_filters": {}}
                    structured_context_str = json.dumps(planner_context, indent=2)
                    sys_prompt_template = PROMPT_TEMPLATES["planner_agent"]
                    history_for_llm = [] # No history for offline
                    
                    # Format the system prompt
                    prompt_safe_positions = list(self.all_positions) + ["Faculty", "Staff", "Admin"]
                    sys_prompt = sys_prompt_template.format(
                        all_programs_list=self.all_programs, all_departments_list=self.all_departments,
                        all_positions_list=sorted(list(set(prompt_safe_positions))),
                        all_doc_types_list=self.all_doc_types, all_statuses_list=self.all_statuses,
                        dynamic_examples=dynamic_examples,
                        structured_context_str=structured_context_str
                    )
                    planner_user_prompt = query
                    # --- End Simplified Prompt Generation ---

                    plan_raw = self.planner_llm.execute(
                        system_prompt=sys_prompt, user_prompt=planner_user_prompt,
                        json_mode=True, phase="planner",
                        history=history_for_llm
                    )
            
                    plan_json = self._repair_json(plan_raw)
                    
                    # --- Plan Validation (Same as Online) ---
                    is_valid_plan, validation_error = self._validate_plan(plan_json)

                    if is_valid_plan:
                        self.debug(f"Valid multi-step plan received on attempt {attempt + 1}.")
                        break # Success!
                    
                    self.debug(f"Plan validation failed: {validation_error}")
                    plan_json = None # Invalidate the broken plan
                    time.sleep(1)
                
                planner_duration = time.time() - planner_start_time
                
                if not plan_json:
                    outcome = "FAIL_PLANNER"
                    raise ValueError(f"AI failed to select a valid plan after {max_retries} attempts. Last error: {plan_raw}")

                # --- Multi-Step Execution Loop (Same as Online) ---
                retrieval_start_time = time.time()
                step_results = {}
                collected_docs = []
                plan_steps = plan_json.get("plan", [])

                for i, step in enumerate(plan_steps):
                    step_num = i + 1
                    tool_call = step.get("tool_call", {})
                    tool_name = tool_call.get("tool_name")
                    params = tool_call.get("parameters", {})
                    
                    if not tool_name:
                        self.debug(f"Step {step_num} is missing a tool_name. Stopping plan.")
                        break
                    
                    try:
                        resolved_params = self._resolve_placeholders(params, step_results)
                        params_str = json.dumps(resolved_params)
                        if '$' in params_str:
                            unresolved = [v for v in re.findall(r'"(\$.*?)"', params_str)]
                            if unresolved:
                                raise ValueError(f"Plan failed at step {step_num}: Required value {unresolved[0]} was not found.")
                        self.debug(f"   -> Executing Step {step_num}: {tool_name} with params: {resolved_params}")
                    
                    except Exception as e:
                        self.debug(f"Error during placeholder resolution or execution for step {step_num}: {e}")
                        raise
                    
                    if tool_name == "finish_plan":
                        self.debug("Plan execution complete.")
                        break
                    
                    if tool_name in self.available_tools:
                        tool_function = self.available_tools[tool_name]
                        sig = inspect.signature(tool_function)
                        valid_params = {k: v for k, v in resolved_params.items() if k in sig.parameters}
                        dropped = [k for k in resolved_params if k not in sig.parameters]
                        if dropped:
                            self.debug(f"Dropping unexpected parameters for {tool_name}: {dropped}")
                        
                        step_output = tool_function(**valid_params)
                        step_output_list = step_output if isinstance(step_output, list) else [step_output]
                        step_results[step_num] = step_output_list
                        collected_docs.extend(step_output_list)
                    else:
                        raise ValueError(f"Plan step {step_num} uses an unknown tool: '{tool_name}'")
                
                retrieval_duration = time.time() - retrieval_start_time
                collected_docs = [doc for doc in collected_docs if doc.get("source_collection") not in ("system_signal", "system_note")]
                
                has_errors = any("error" in doc.get("status", "") for doc in collected_docs)
                is_empty = (not collected_docs or all("empty" in doc.get("status", "") for doc in collected_docs)) and not has_errors

                if has_errors:
                    execution_mode = "primary"
                    self.debug(f"Primary plan failed with an error. Reporting as FAIL_EXECUTION.")
                    outcome = "FAIL_EXECUTION"
                
                elif is_empty:
                    self.debug("Primary plan executed successfully but found no results. (FAIL_EMPTY)")
                    outcome = "FAIL_EMPTY"
                
                else:
                    outcome = "SUCCESS_DIRECT"

                # --- De-duplication (Same as Online) ---
                if collected_docs:
                    self.debug(f"Original unfiltered doc count: {len(collected_docs)}. Starting de-duplication...")
                    unique_docs = {}
                    for doc in collected_docs:
                        try: content_key = json.dumps(doc.get('content'), sort_keys=True)
                        except TypeError: content_key = str(doc.get('content')) 
                        if content_key and content_key not in unique_docs:
                            unique_docs[content_key] = doc
                    collected_docs = list(unique_docs.values())
                    self.debug(f"Found {len(collected_docs)} unique documents after de-duplication.")

                # --- Student Grouping (Same as Online) ---
                if len(collected_docs) > 5:
                    primary_tool_name = ""
                    if plan_json and plan_json.get("plan"):
                        primary_tool_name = plan_json.get("plan", [{}])[0].get("tool_call", {}).get("tool_name", "")
                    
                    first_doc_meta = collected_docs[0].get("metadata", {})
                    is_student_data = "student_id" in first_doc_meta

                    if is_student_data and primary_tool_name == "find_people":
                        self.debug(f"-> Student result set ({len(collected_docs)} docs) detected for 'find_people'. Restructuring into groups.")
                        grouped_students = defaultdict(list)
                        for doc in collected_docs:
                            meta = doc.get("metadata", {})
                            group_key = f"{meta.get('course', 'N/A')} - Year {meta.get('year', 'N/A')} - Section {meta.get('section', 'N/A')}"
                            grouped_students[group_key].append(doc)
                        grouped_data = [{"source_collection": "grouped_students", "group_name": name, "students": docs} for name, docs in sorted(grouped_students.items())]
                        collected_docs = grouped_data
                    elif is_student_data:
                        self.debug(f"-> Skipping student grouping. Primary tool was '{primary_tool_name}', not 'find_people'.")

                self.debug("\n" + "="*50)
                self.debug(f"📑 Final {len(collected_docs)} documents being sent to Synthesizer:")
                try:
                    debug_output = json.dumps(collected_docs, indent=2)
                    print(debug_output)
                except Exception as e:
                    print(f"Could not print debug output: {e}")
                self.debug("="*50 + "\n")

                if outcome in ["SUCCESS_DIRECT", "SUCCESS_FALLBACK"]:
                    results_count = len(collected_docs)
                    self.debug(f"Cleaning {results_count} docs for Synthesizer...")
                    cleaned_docs_for_synth = self._clean_documents_for_synthesizer(collected_docs)
                    final_context = {
                        "status": "success",
                        "summary": f"Found {results_count} relevant document(s).",
                        "data": cleaned_docs_for_synth[:30]
                    }
                else:
                    final_context = {"status": "empty", "summary": "I tried a precise search, but could not find any relevant documents."}

            except Exception as e:
                if planner_duration == 0.0 and 'planner_start_time' in locals():
                    planner_duration = time.time() - planner_start_time
                if retrieval_duration == 0.0 and 'retrieval_start_time' in locals():
                    retrieval_duration = time.time() - retrieval_start_time
                import traceback
                self.debug(f"An unexpected error occurred: {e}")
                self.debug(f"Error Type: {type(e)}")
                self.debug(f"Traceback: {traceback.format_exc()}")
                error_msg = str(e)
                if outcome == "FAIL_UNKNOWN":
                    outcome = "FAIL_EXECUTION"
                final_context = {"status": "error", "summary": f"I ran into a technical problem: {e}"}

            # --- Synthesizer Block (Same as Online) ---
            self.debug("Synthesizing final answer...")
            synth_start_time = time.time()
            context_for_llm = json.dumps(final_context, indent=2, ensure_ascii=False)
            synth_prompt = PROMPT_TEMPLATES["final_synthesizer"].format(context=context_for_llm, query=query)
            
            final_answer = self.synth_llm.execute(
                system_prompt="You are a careful AI analyst who provides conversational answers based only on the provided facts.",
                user_prompt=synth_prompt, 
                history=chat_history, # Will be [] for offline mode
                phase="synth"
            )
            synth_duration = time.time() - synth_start_time

            # --- Post-Synthesis Block (Same as Online) ---
            corruption_details = sorted(list(self.corruption_warnings)) if self.corruption_warnings else None
            final_plan_hash = None 

            try:
                failure_keywords = ["i'm sorry", "unfortunately", "i couldn't find", "i am unable", "not available", "technical problem"]
                is_successful_answer = not any(keyword in final_answer.lower() for keyword in failure_keywords)
                if outcome.startswith("SUCCESS") and is_successful_answer and plan_json:
                    self.debug("Saving example to memory: SUCCESS and final answer looks good.")
                    final_plan_hash = self._save_dynamic_example(query, plan_json, session, outcome)
                elif outcome.startswith("SUCCESS"):
                    self.debug("Skipping example save: Final answer looked like a soft failure.")
            except Exception as e:
                self.debug(f"Post-synthesis evaluation or example saving failed: {e}")

            # --- Logging Block (Same as Online) ---
            execution_time = time.time() - start_time
            self.training_system.record_query_result(
                query=query, plan=plan_json, results_count=results_count,
                execution_time=execution_time, error_msg=error_msg,
                execution_mode=execution_mode, outcome=outcome, analyst_mode=self.execution_mode,
                final_answer=final_answer, corruption_details=corruption_details,
                timestamp=start_datetime, session_id=session.get('session_id'),
                planner_duration=planner_duration, retrieval_duration=retrieval_duration,
                synth_duration=synth_duration, planner_model=self.planner_llm.planner_model,
                synth_model=self.synth_llm.synth_model, plan_hash=final_plan_hash
            )
            
            return final_answer, plan_json, collected_docs
            # --- END OFFLINE EXECUTION PATH ---

        else:
            # --- START ONLINE/SPLIT EXECUTION PATH (Original Code) ---
            self.debug("Starting reasoning plan execution...")
            # Note: start_time, start_datetime, durations, and context were initialized above
            
            # --- "MINI-LLM" TRIAGE STEP ---
            triage_result = self._run_query_triage(query, session)
            intent = triage_result.get("intent")
            
            if intent == "ANSWER_TO_CLARIFICATION":
                query = triage_result.get("combined_query", query)
                context["clarification_pending"] = False
                self.debug(f"Triage: Proceeding with combined query: {query}")
                
            elif intent == "CONVERSATIONAL":
                self.debug("Triage: Query is conversational. Routing to dedicated synth call.")
                planner_start_time = time.time()
                chat_history = self._get_topic_scoped_history(session, self.max_history_turns)
                planner_duration = time.time() - planner_start_time

                synth_start_time = time.time()
                final_answer = self.synth_llm.execute(
                    system_prompt="You are a friendly and helpful AI assistant for PDM. Respond naturally and conversationally to the user.",
                    user_prompt=query,
                    history=chat_history or [],
                    phase="synth"
                )
                synth_duration = time.time() - synth_start_time
                execution_time = time.time() - start_time
                
                self.training_system.record_query_result(
                    query=query, plan={"plan": [{"tool_call": {"tool_name": "answer_conversational_query"}}]}, 
                    outcome="SUCCESS_CONVERSATIONAL", 
                    execution_time=execution_time, final_answer=final_answer, results_count=0,
                    timestamp=start_datetime, session_id=session.get('session_id'),
                    planner_duration=planner_duration, retrieval_duration=0.0, synth_duration=synth_duration,
                    planner_model=self.planner_llm.planner_model, synth_model=self.synth_llm.synth_model,
                    plan_hash=None
                )
                return final_answer, {"plan": [{"tool_call": {"tool_name": "answer_conversational_query"}}]}, []

            elif intent == "NEW_AMBIGUOUS_QUERY":
                self.debug("Triage: Query is new and ambiguous. Forcing clarification.")
                planner_start_time = time.time()
                sys_prompt = PROMPT_TEMPLATES["ambiguity_resolver_prompt"].format(db_schema_summary=self.db_schema_summary)
                plan_raw = self.planner_llm.execute(
                    system_prompt=sys_prompt, user_prompt=query,
                    json_mode=True, phase="planner", history=[]
                )
                plan_json = self._repair_json(plan_raw)
                planner_duration = time.time() - planner_start_time
                
                try:
                    tool_call = plan_json.get("plan", [{}])[0].get("tool_call", {})
                    if tool_call.get("tool_name") == "request_clarification":
                        question_for_user = tool_call.get("parameters", {}).get("question_for_user", "Could you provide more details?")
                    else:
                        question_for_user = "I'm sorry, I'm not sure what you mean. Could you provide more details?"
                except Exception:
                    question_for_user = "I'm not sure what you mean. Could you rephrase that?"
                
                context["clarification_pending"] = True
                context["original_ambiguous_query"] = query
                self.sessions_collection.update_one(
                    {"session_id": session["session_id"]},
                    {"$set": {"structured_context": context, "updated_at": datetime.now(timezone.utc)}},
                    upsert=True
                )
                self._update_session_history(session['session_id'], query, question_for_user)
                return question_for_user, plan_json, []

            # --- END OF TRIAGE LOGIC ---
            
            chat_history = self._get_topic_scoped_history(session, self.max_history_turns)
            summary = session.get("conversation_summary", "No summary yet.")
            
            plan_json = None
            final_context = {}
            error_msg = None
            results_count = 0
            
            outcome = "FAIL_UNKNOWN"
            execution_mode = "primary"
            collected_docs = []
            
            try:
                max_retries = 5
                planner_start_time = time.time()

                coref_params = self._coref_to_params(query, session)
                if coref_params:
                    self.debug(f"Injecting coreference params into query: {coref_params}")
                    query = f"{query}\n\n[System Hint: The pronoun in the query (he/she/his/her) refers to: {coref_params.get('person_name')}]"
                
                filters_cleared_on_retry = False

                for attempt in range(max_retries):
                    self.debug(f"Planner Attempt {attempt + 1}/{max_retries}...")
                    
                    if filters_cleared_on_retry:
                        self.debug("!!! 422 Recovery: Retrying with a minimal prompt (no context, no examples).")
                        planner_context = {"current_topic": "None.", "active_filters": {}}
                        structured_context_str = json.dumps(planner_context, indent=2)
                        dynamic_examples = ""
                        sys_prompt_template = PROMPT_TEMPLATES["planner_agent"]
                        history_for_llm = []
                    else:
                        self.debug("-> Using 'Full Planner Prompt' with context.")
                        dynamic_examples = self._load_dynamic_examples(query)
                        
                        full_context = session.get("structured_context", {})
                        planner_context = {
                            "current_topic": full_context.get("current_topic"),
                            "active_filters": {}
                        }
                        query_lower = query.strip().lower()
                        new_topic_starters = ["who is", "what is", "what are", "show me", "list", "find", "get", "compare"]
                        is_new_topic = any(query_lower.startswith(starter) for starter in new_topic_starters)
                        
                        if not is_new_topic:
                            self.debug("Query seems like a follow-up. Passing active filters.")
                            planner_context["active_filters"] = full_context.get("active_filters", {})
                        else:
                            self.debug("Query seems like a new topic. Wiping active filters for Planner.")
                        
                        structured_context_str = json.dumps(planner_context, indent=2)
                        self.debug(f"Sending pruned context to Planner: {structured_context_str}")
                        sys_prompt_template = PROMPT_TEMPLATES["planner_agent"]
                        history_for_llm = chat_history
                    
                    prompt_safe_positions = list(self.all_positions) + ["Faculty", "Staff", "Admin"]
                    sys_prompt = sys_prompt_template.format(
                        all_programs_list=self.all_programs, all_departments_list=self.all_departments,
                        all_positions_list=sorted(list(set(prompt_safe_positions))),
                        all_doc_types_list=self.all_doc_types, all_statuses_list=self.all_statuses,
                        dynamic_examples=dynamic_examples,
                        structured_context_str=structured_context_str
                    )
                    planner_user_prompt = query

                    plan_raw = self.planner_llm.execute(
                        system_prompt=sys_prompt, user_prompt=planner_user_prompt,
                        json_mode=True, phase="planner",
                        history=history_for_llm
                    )
            
                    plan_json = self._repair_json(plan_raw)
                    
                    is_valid_plan, validation_error = self._validate_plan(plan_json)

                    if is_valid_plan:
                        self.debug(f"Valid multi-step plan received on attempt {attempt + 1}.")
                        break
                    
                    self.debug(f"Plan validation failed: {validation_error}")
                    plan_json = None
                    
                    if "422" in plan_raw:
                        self.debug("!!! 422 Error detected. The API rejected the context.")
                        if not filters_cleared_on_retry:
                            self.debug("   -> Will retry ONCE with all active_filters cleared.")
                            filters_cleared_on_retry = True
                        else:
                            self.debug("   -> Already retried with cleared filters. Failing permanently.")
                            break
                    else:
                        self.debug(f"Attempt {attempt + 1} failed (Not a 422). Retrying...")
                    time.sleep(1)
                
                planner_duration = time.time() - planner_start_time
                
                if not plan_json:
                    outcome = "FAIL_PLANNER"
                    raise ValueError(f"AI failed to select a valid plan after {max_retries} attempts. Last error: {plan_raw}")

                # --- Multi-Step Execution Loop ---
                retrieval_start_time = time.time()
                step_results = {}
                collected_docs = []
                plan_steps = plan_json.get("plan", [])

                for i, step in enumerate(plan_steps):
                    step_num = i + 1
                    tool_call = step.get("tool_call", {})
                    tool_name = tool_call.get("tool_name")
                    params = tool_call.get("parameters", {})
                    
                    if not tool_name:
                        self.debug(f"Step {step_num} is missing a tool_name. Stopping plan.")
                        break
                    
                    try:
                        resolved_params = self._resolve_placeholders(params, step_results)
                        params_str = json.dumps(resolved_params)
                        if '$' in params_str:
                            unresolved = [v for v in re.findall(r'"(\$.*?)"', params_str)]
                            if unresolved:
                                raise ValueError(f"Plan failed at step {step_num}: Required value {unresolved[0]} was not found.")
                        self.debug(f"   -> Executing Step {step_num}: {tool_name} with params: {resolved_params}")
                    
                    except Exception as e:
                        self.debug(f"Error during placeholder resolution or execution for step {step_num}: {e}")
                        raise
                    
                    if tool_name == "finish_plan":
                        self.debug("Plan execution complete.")
                        break
                    
                    if tool_name in self.available_tools:
                        tool_function = self.available_tools[tool_name]
                        sig = inspect.signature(tool_function)
                        valid_params = {k: v for k, v in resolved_params.items() if k in sig.parameters}
                        dropped = [k for k in resolved_params if k not in sig.parameters]
                        if dropped:
                            self.debug(f"Dropping unexpected parameters for {tool_name}: {dropped}")
                        
                        step_output = tool_function(**valid_params)
                        step_output_list = step_output if isinstance(step_output, list) else [step_output]
                        step_results[step_num] = step_output_list
                        collected_docs.extend(step_output_list)
                    else:
                        raise ValueError(f"Plan step {step_num} uses an unknown tool: '{tool_name}'")
                
                retrieval_duration = time.time() - retrieval_start_time
                collected_docs = [doc for doc in collected_docs if doc.get("source_collection") not in ("system_signal", "system_note")]
                
                has_errors = any("error" in doc.get("status", "") for doc in collected_docs)
                is_empty = (not collected_docs or all("empty" in doc.get("status", "") for doc in collected_docs)) and not has_errors

                if has_errors:
                    execution_mode = "primary"
                    self.debug(f"Primary plan failed with an error. Reporting as FAIL_EXECUTION.")
                    outcome = "FAIL_EXECUTION"
                
                elif is_empty:
                    self.debug("Primary plan executed successfully but found no results. (FAIL_EMPTY)")
                    outcome = "FAIL_EMPTY"
                
                else:
                    outcome = "SUCCESS_DIRECT"

                if collected_docs:
                    self.debug(f"Original unfiltered doc count: {len(collected_docs)}. Starting de-duplication...")
                    unique_docs = {}
                    for doc in collected_docs:
                        try: content_key = json.dumps(doc.get('content'), sort_keys=True)
                        except TypeError: content_key = str(doc.get('content')) 
                        if content_key and content_key not in unique_docs:
                            unique_docs[content_key] = doc
                    collected_docs = list(unique_docs.values())
                    self.debug(f"Found {len(collected_docs)} unique documents after de-duplication.")

                if len(collected_docs) > 5:
                    primary_tool_name = ""
                    if plan_json and plan_json.get("plan"):
                        primary_tool_name = plan_json.get("plan", [{}])[0].get("tool_call", {}).get("tool_name", "")
                    
                    first_doc_meta = collected_docs[0].get("metadata", {})
                    is_student_data = "student_id" in first_doc_meta

                    if is_student_data and primary_tool_name == "find_people":
                        self.debug(f"-> Student result set ({len(collected_docs)} docs) detected for 'find_people'. Restructuring into groups.")
                        grouped_students = defaultdict(list)
                        for doc in collected_docs:
                            meta = doc.get("metadata", {})
                            group_key = f"{meta.get('course', 'N/A')} - Year {meta.get('year', 'N/A')} - Section {meta.get('section', 'N/A')}"
                            grouped_students[group_key].append(doc)
                        grouped_data = [{"source_collection": "grouped_students", "group_name": name, "students": docs} for name, docs in sorted(grouped_students.items())]
                        collected_docs = grouped_data
                    elif is_student_data:
                        self.debug(f"-> Skipping student grouping. Primary tool was '{primary_tool_name}', not 'find_people'.")

                self.debug("\n" + "="*50)
                self.debug(f"📑 Final {len(collected_docs)} documents being sent to Synthesizer:")
                try:
                    debug_output = json.dumps(collected_docs, indent=2)
                    print(debug_output)
                except Exception as e:
                    print(f"Could not print debug output: {e}")
                self.debug("="*50 + "\n")

                if outcome in ["SUCCESS_DIRECT", "SUCCESS_FALLBACK"]:
                    results_count = len(collected_docs)
                    self.debug(f"Cleaning {results_count} docs for Synthesizer...")
                    cleaned_docs_for_synth = self._clean_documents_for_synthesizer(collected_docs)
                    final_context = {
                        "status": "success",
                        "summary": f"Found {results_count} relevant document(s).",
                        "data": cleaned_docs_for_synth[:30]
                    }
                else:
                    final_context = {"status": "empty", "summary": "I tried a precise search and a broad search, but could not find any relevant documents."}

            except Exception as e:
                if planner_duration == 0.0 and 'planner_start_time' in locals():
                    planner_duration = time.time() - planner_start_time
                if retrieval_duration == 0.0 and 'retrieval_start_time' in locals():
                    retrieval_duration = time.time() - retrieval_start_time
                import traceback
                self.debug(f"An unexpected error occurred: {e}")
                self.debug(f"Error Type: {type(e)}")
                self.debug(f"Traceback: {traceback.format_exc()}")
                error_msg = str(e)
                if outcome == "FAIL_UNKNOWN":
                    outcome = "FAIL_EXECUTION"
                final_context = {"status": "error", "summary": f"I ran into a technical problem: {e}"}

            # --- Synthesizer Block ---
            self.debug("Synthesizing final answer...")
            synth_start_time = time.time()
            context_for_llm = json.dumps(final_context, indent=2, ensure_ascii=False)
            synth_prompt = PROMPT_TEMPLATES["final_synthesizer"].format(context=context_for_llm, query=query)
            
            final_answer = self.synth_llm.execute(
                system_prompt="You are a careful AI analyst who provides conversational answers based only on the provided facts.",
                user_prompt=synth_prompt, 
                history=chat_history if not filters_cleared_on_retry else [],
                phase="synth"
            )
            synth_duration = time.time() - synth_start_time

            # --- Post-Synthesis Block ---
            corruption_details = sorted(list(self.corruption_warnings)) if self.corruption_warnings else None
            final_plan_hash = None 

            try:
                failure_keywords = ["i'm sorry", "unfortunately", "i couldn't find", "i am unable", "not available", "technical problem"]
                is_successful_answer = not any(keyword in final_answer.lower() for keyword in failure_keywords)
                if outcome.startswith("SUCCESS") and is_successful_answer and plan_json:
                    self.debug("Saving example to memory: SUCCESS and final answer looks good.")
                    final_plan_hash = self._save_dynamic_example(query, plan_json, session, outcome)
                elif outcome.startswith("SUCCESS"):
                    self.debug("Skipping example save: Final answer looked like a soft failure.")
            except Exception as e:
                self.debug(f"Post-synthesis evaluation or example saving failed: {e}")

            # --- Logging Block ---
            execution_time = time.time() - start_time
            self.training_system.record_query_result(
                query=query, plan=plan_json, results_count=results_count,
                execution_time=execution_time, error_msg=error_msg,
                execution_mode=execution_mode, outcome=outcome, analyst_mode=self.execution_mode,
                final_answer=final_answer, corruption_details=corruption_details,
                timestamp=start_datetime, session_id=session.get('session_id'),
                planner_duration=planner_duration, retrieval_duration=retrieval_duration,
                synth_duration=synth_duration, planner_model=self.planner_llm.planner_model,
                synth_model=self.synth_llm.synth_model, plan_hash=final_plan_hash
            )
            
            return final_answer, plan_json, collected_docs
            # --- END ONLINE/SPLIT EXECUTION PATH ---




# --- REPLACE THIS ENTIRE METHOD ---
    def execute_reasoning_plan(self, query: str, session: dict) -> tuple[str, Optional[dict], List[dict]]:
        """
        [UPGRADED WITH OFFLINE MODE V2] The main orchestration method.
        - If 'offline', it runs a simple *single-step* Planner -> Synth loop.
        - If 'online', it first runs a "mini-LLM" triage to classify
          the query, handles ambiguity, and uses chat history.
        """
        start_time = time.time()
        start_datetime = datetime.now(timezone.utc)
        
        planner_duration = 0.0
        retrieval_duration = 0.0
        synth_duration = 0.0
        plan_hash = None
        
        context = session.get("structured_context", {})

        # --- NEW OFFLINE/ONLINE LOGIC ---
        if self.execution_mode == 'offline':
            # --- START OFFLINE EXECUTION PATH (Single-Step) ---
            self.debug("Offline mode: Running SINGLE-STEP plan. Skipping Triage, History, and Coreference.")
            chat_history = [] # No history for offline mode
            
            plan_json = None
            final_context = {}
            error_msg = None
            results_count = 0
            
            outcome = "FAIL_UNKNOWN"
            execution_mode = "primary"
            collected_docs = []

            try:
                planner_start_time = time.time()
                
                # --- Simplified Prompt Generation (Offline) ---
                self.debug("-> Using 'planner_agent_offline' (single-step).")
                dynamic_examples = "" # No examples for offline
                
                # Format the system prompt
                prompt_safe_positions = list(self.all_positions) + ["Faculty", "Staff", "Admin"]
                sys_prompt = PROMPT_TEMPLATES["planner_agent_offline"].format(
                    all_programs_list=self.all_programs, all_departments_list=self.all_departments,
                    all_positions_list=sorted(list(set(prompt_safe_positions))),
                    all_doc_types_list=self.all_doc_types, all_statuses_list=self.all_statuses,
                    dynamic_examples=dynamic_examples
                    # Note: No structured_context_str for this prompt
                )
                planner_user_prompt = query
                # --- End Simplified Prompt Generation ---

                plan_raw = self.planner_llm.execute(
                    system_prompt=sys_prompt, user_prompt=planner_user_prompt,
                    json_mode=True, phase="planner",
                    history=chat_history # Will be []
                )
        
                # --- Manually Build Plan ---
                # The offline planner returns a single tool call, not a multi-step plan.
                tool_call_dict = self._repair_json(plan_raw)
                
                if not tool_call_dict or "tool_name" not in tool_call_dict:
                    planner_duration = time.time() - planner_start_time
                    outcome = "FAIL_PLANNER"
                    raise ValueError(f"AI failed to select a valid tool. Last error: {plan_raw}")

                # We manually wrap the single tool call into a multi-step
                # plan structure so the rest of the code (logging, execution loop) works.
                plan_json = {
                    "plan": [
                        {"tool_call": tool_call_dict},
                        {"tool_call": {"tool_name": "finish_plan", "parameters": {}}}
                    ]
                }
                self.debug(f"Offline planner selected tool: {tool_call_dict.get('tool_name')}")
                
                planner_duration = time.time() - planner_start_time

                # --- Multi-Step Execution Loop (now runs our 2-step plan) ---
                retrieval_start_time = time.time()
                step_results = {}
                collected_docs = []
                plan_steps = plan_json.get("plan", []) # Will be [tool_call, finish_plan]

                for i, step in enumerate(plan_steps):
                    step_num = i + 1
                    tool_call = step.get("tool_call", {})
                    tool_name = tool_call.get("tool_name")
                    params = tool_call.get("parameters", {})
                    
                    if not tool_name:
                        self.debug(f"Step {step_num} is missing a tool_name. Stopping plan.")
                        break
                    
                    # No placeholder resolution needed for this simple plan, but we'll keep the logic
                    # in case the offline prompt is ever changed to be multi-step.
                    try:
                        resolved_params = self._resolve_placeholders(params, step_results)
                        self.debug(f"   -> Executing Step {step_num}: {tool_name} with params: {resolved_params}")
                    
                    except Exception as e:
                        self.debug(f"Error during placeholder resolution for step {step_num}: {e}")
                        raise
                    
                    if tool_name == "finish_plan":
                        self.debug("Plan execution complete.")
                        break
                    
                    if tool_name in self.available_tools:
                        tool_function = self.available_tools[tool_name]
                        sig = inspect.signature(tool_function)
                        valid_params = {k: v for k, v in resolved_params.items() if k in sig.parameters}
                        
                        step_output = tool_function(**valid_params)
                        step_output_list = step_output if isinstance(step_output, list) else [step_output]
                        step_results[step_num] = step_output_list
                        collected_docs.extend(step_output_list)
                    else:
                        raise ValueError(f"Plan step {step_num} uses an unknown tool: '{tool_name}'")
                
                retrieval_duration = time.time() - retrieval_start_time
                collected_docs = [doc for doc in collected_docs if doc.get("source_collection") not in ("system_signal", "system_note")]
                
                has_errors = any("error" in doc.get("status", "") for doc in collected_docs)
                is_empty = (not collected_docs or all("empty" in doc.get("status", "") for doc in collected_docs)) and not has_errors

                if has_errors:
                    execution_mode = "primary"
                    self.debug(f"Primary plan failed with an error. Reporting as FAIL_EXECUTION.")
                    outcome = "FAIL_EXECUTION"
                
                elif is_empty:
                    self.debug("Primary plan executed successfully but found no results. (FAIL_EMPTY)")
                    outcome = "FAIL_EMPTY"
                
                else:
                    outcome = "SUCCESS_DIRECT"

                # --- De-duplication (Same as Online) ---
                if collected_docs:
                    self.debug(f"Original unfiltered doc count: {len(collected_docs)}. Starting de-duplication...")
                    unique_docs = {}
                    for doc in collected_docs:
                        try: content_key = json.dumps(doc.get('content'), sort_keys=True)
                        except TypeError: content_key = str(doc.get('content')) 
                        if content_key and content_key not in unique_docs:
                            unique_docs[content_key] = doc
                    collected_docs = list(unique_docs.values())
                    self.debug(f"Found {len(collected_docs)} unique documents after de-duplication.")

                # --- Student Grouping (Same as Online) ---
                if len(collected_docs) > 5:
                    primary_tool_name = ""
                    if plan_json and plan_json.get("plan"):
                        primary_tool_name = plan_json.get("plan", [{}])[0].get("tool_call", {}).get("tool_name", "")
                    
                    first_doc_meta = collected_docs[0].get("metadata", {})
                    is_student_data = "student_id" in first_doc_meta

                    if is_student_data and primary_tool_name == "find_people":
                        self.debug(f"-> Student result set ({len(collected_docs)} docs) detected for 'find_people'. Restructuring into groups.")
                        grouped_students = defaultdict(list)
                        for doc in collected_docs:
                            meta = doc.get("metadata", {})
                            group_key = f"{meta.get('course', 'N/A')} - Year {meta.get('year', 'N/A')} - Section {meta.get('section', 'N/A')}"
                            grouped_students[group_key].append(doc)
                        grouped_data = [{"source_collection": "grouped_students", "group_name": name, "students": docs} for name, docs in sorted(grouped_students.items())]
                        collected_docs = grouped_data
                    elif is_student_data:
                        self.debug(f"-> Skipping student grouping. Primary tool was '{primary_tool_name}', not 'find_people'.")

                self.debug("\n" + "="*50)
                self.debug(f"📑 Final {len(collected_docs)} documents being sent to Synthesizer:")
                try:
                    debug_output = json.dumps(collected_docs, indent=2)
                    print(debug_output)
                except Exception as e:
                    print(f"Could not print debug output: {e}")
                self.debug("="*50 + "\n")

                if outcome in ["SUCCESS_DIRECT", "SUCCESS_FALLBACK"]:
                    results_count = len(collected_docs)
                    self.debug(f"Cleaning {results_count} docs for Synthesizer...")
                    cleaned_docs_for_synth = self._clean_documents_for_synthesizer(collected_docs)
                    final_context = {
                        "status": "success",
                        "summary": f"Found {results_count} relevant document(s).",
                        "data": cleaned_docs_for_synth[:30]
                    }
                else:
                    final_context = {"status": "empty", "summary": "I tried a precise search, but could not find any relevant documents."}

            except Exception as e:
                if planner_duration == 0.0 and 'planner_start_time' in locals():
                    planner_duration = time.time() - planner_start_time
                if retrieval_duration == 0.0 and 'retrieval_start_time' in locals():
                    retrieval_duration = time.time() - retrieval_start_time
                import traceback
                self.debug(f"An unexpected error occurred: {e}")
                self.debug(f"Error Type: {type(e)}")
                self.debug(f"Traceback: {traceback.format_exc()}")
                error_msg = str(e)
                if outcome == "FAIL_UNKNOWN":
                    outcome = "FAIL_EXECUTION"
                final_context = {"status": "error", "summary": f"I ran into a technical problem: {e}"}

            # --- Synthesizer Block (Using OFFLINE prompt) ---
            self.debug("Synthesizing final answer...")
            synth_start_time = time.time()
            context_for_llm = json.dumps(final_context, indent=2, ensure_ascii=False)
            
            # --- THIS IS THE CHANGE ---
            synth_prompt = PROMPT_TEMPLATES["final_synthesizer_offline"].format(context=context_for_llm, query=query)
            
            final_answer = self.synth_llm.execute(
                system_prompt="You are a careful AI analyst who provides conversational answers based only on the provided facts.",
                user_prompt=synth_prompt, 
                history=chat_history, # Will be [] for offline mode
                phase="synth"
            )
            # --- END OF CHANGE ---
            
            synth_duration = time.time() - synth_start_time

            # --- Post-Synthesis Block (Same as Online) ---
            corruption_details = sorted(list(self.corruption_warnings)) if self.corruption_warnings else None
            final_plan_hash = None 

            try:
                failure_keywords = ["i'm sorry", "unfortunately", "i couldn't find", "i am unable", "not available", "technical problem"]
                is_successful_answer = not any(keyword in final_answer.lower() for keyword in failure_keywords)
                if outcome.startswith("SUCCESS") and is_successful_answer and plan_json:
                    self.debug("Saving example to memory: SUCCESS and final answer looks good.")
                    final_plan_hash = self._save_dynamic_example(query, plan_json, session, outcome)
                elif outcome.startswith("SUCCESS"):
                    self.debug("Skipping example save: Final answer looked like a soft failure.")
            except Exception as e:
                self.debug(f"Post-synthesis evaluation or example saving failed: {e}")

            # --- Logging Block (Same as Online) ---
            execution_time = time.time() - start_time
            self.training_system.record_query_result(
                query=query, plan=plan_json, results_count=results_count,
                execution_time=execution_time, error_msg=error_msg,
                execution_mode=execution_mode, outcome=outcome, analyst_mode=self.execution_mode,
                final_answer=final_answer, corruption_details=corruption_details,
                timestamp=start_datetime, session_id=session.get('session_id'),
                planner_duration=planner_duration, retrieval_duration=retrieval_duration,
                synth_duration=synth_duration, planner_model=self.planner_llm.planner_model,
                synth_model=self.synth_llm.synth_model, plan_hash=final_plan_hash
            )
            
            return final_answer, plan_json, collected_docs
            # --- END OFFLINE EXECUTION PATH ---

        else:
            # --- START ONLINE/SPLIT EXECUTION PATH (Original Code) ---
            self.debug("Starting reasoning plan execution...")
            # Note: start_time, start_datetime, durations, and context were initialized above
            
            # --- "MINI-LLM" TRIAGE STEP ---
            triage_result = self._run_query_triage(query, session)
            intent = triage_result.get("intent")
            
            if intent == "ANSWER_TO_CLARIFICATION":
                query = triage_result.get("combined_query", query)
                context["clarification_pending"] = False
                self.debug(f"Triage: Proceeding with combined query: {query}")
                
            elif intent == "CONVERSATIONAL":
                self.debug("Triage: Query is conversational. Routing to dedicated synth call.")
                planner_start_time = time.time()
                chat_history = self._get_topic_scoped_history(session, self.max_history_turns)
                planner_duration = time.time() - planner_start_time

                synth_start_time = time.time()
                final_answer = self.synth_llm.execute(
                    system_prompt="You are a friendly and helpful AI assistant for PDM. Respond naturally and conversationally to the user.",
                    user_prompt=query,
                    history=chat_history or [],
                    phase="synth"
                )
                synth_duration = time.time() - synth_start_time
                execution_time = time.time() - start_time
                
                self.training_system.record_query_result(
                    query=query, plan={"plan": [{"tool_call": {"tool_name": "answer_conversational_query"}}]}, 
                    outcome="SUCCESS_CONVERSATIONAL", 
                    execution_time=execution_time, final_answer=final_answer, results_count=0,
                    timestamp=start_datetime, session_id=session.get('session_id'),
                    planner_duration=planner_duration, retrieval_duration=0.0, synth_duration=synth_duration,
                    planner_model=self.planner_llm.planner_model, synth_model=self.synth_llm.synth_model,
                    plan_hash=None
                )
                return final_answer, {"plan": [{"tool_call": {"tool_name": "answer_conversational_query"}}]}, []

            elif intent == "NEW_AMBIGUOUS_QUERY":
                self.debug("Triage: Query is new and ambiguous. Forcing clarification.")
                planner_start_time = time.time()
                sys_prompt = PROMPT_TEMPLATES["ambiguity_resolver_prompt"].format(db_schema_summary=self.db_schema_summary)
                plan_raw = self.planner_llm.execute(
                    system_prompt=sys_prompt, user_prompt=query,
                    json_mode=True, phase="planner", history=[]
                )
                plan_json = self._repair_json(plan_raw)
                planner_duration = time.time() - planner_start_time
                
                try:
                    tool_call = plan_json.get("plan", [{}])[0].get("tool_call", {})
                    if tool_call.get("tool_name") == "request_clarification":
                        question_for_user = tool_call.get("parameters", {}).get("question_for_user", "Could you provide more details?")
                    else:
                        question_for_user = "I'm sorry, I'm not sure what you mean. Could you provide more details?"
                except Exception:
                    question_for_user = "I'm not sure what you mean. Could you rephrase that?"
                
                context["clarification_pending"] = True
                context["original_ambiguous_query"] = query
                self.sessions_collection.update_one(
                    {"session_id": session["session_id"]},
                    {"$set": {"structured_context": context, "updated_at": datetime.now(timezone.utc)}},
                    upsert=True
                )
                self._update_session_history(session['session_id'], query, question_for_user)
                return question_for_user, plan_json, []

            # --- END OF TRIAGE LOGIC ---
            
            chat_history = self._get_topic_scoped_history(session, self.max_history_turns)
            summary = session.get("conversation_summary", "No summary yet.")
            
            plan_json = None
            final_context = {}
            error_msg = None
            results_count = 0
            
            outcome = "FAIL_UNKNOWN"
            execution_mode = "primary"
            collected_docs = []
            
            try:
                max_retries = 5
                planner_start_time = time.time()

                coref_params = self._coref_to_params(query, session)
                if coref_params:
                    self.debug(f"Injecting coreference params into query: {coref_params}")
                    query = f"{query}\n\n[System Hint: The pronoun in the query (he/she/his/her) refers to: {coref_params.get('person_name')}]"
                
                filters_cleared_on_retry = False

                for attempt in range(max_retries):
                    self.debug(f"Planner Attempt {attempt + 1}/{max_retries}...")
                    
                    if filters_cleared_on_retry:
                        self.debug("!!! 422 Recovery: Retrying with a minimal prompt (no context, no examples).")
                        planner_context = {"current_topic": "None.", "active_filters": {}}
                        structured_context_str = json.dumps(planner_context, indent=2)
                        dynamic_examples = ""
                        sys_prompt_template = PROMPT_TEMPLATES["planner_agent"]
                        history_for_llm = []
                    else:
                        self.debug("-> Using 'Full Planner Prompt' with context.")
                        dynamic_examples = self._load_dynamic_examples(query)
                        
                        full_context = session.get("structured_context", {})
                        planner_context = {
                            "current_topic": full_context.get("current_topic"),
                            "active_filters": {}
                        }
                        query_lower = query.strip().lower()
                        new_topic_starters = ["who is", "what is", "what are", "show me", "list", "find", "get", "compare"]
                        is_new_topic = any(query_lower.startswith(starter) for starter in new_topic_starters)
                        
                        if not is_new_topic:
                            self.debug("Query seems like a follow-up. Passing active filters.")
                            planner_context["active_filters"] = full_context.get("active_filters", {})
                        else:
                            self.debug("Query seems like a new topic. Wiping active filters for Planner.")
                        
                        structured_context_str = json.dumps(planner_context, indent=2)
                        self.debug(f"Sending pruned context to Planner: {structured_context_str}")
                        sys_prompt_template = PROMPT_TEMPLATES["planner_agent"]
                        history_for_llm = chat_history
                    
                    prompt_safe_positions = list(self.all_positions) + ["Faculty", "Staff", "Admin"]
                    sys_prompt = sys_prompt_template.format(
                        all_programs_list=self.all_programs, all_departments_list=self.all_departments,
                        all_positions_list=sorted(list(set(prompt_safe_positions))),
                        all_doc_types_list=self.all_doc_types, all_statuses_list=self.all_statuses,
                        dynamic_examples=dynamic_examples,
                        structured_context_str=structured_context_str
                    )
                    planner_user_prompt = query

                    plan_raw = self.planner_llm.execute(
                        system_prompt=sys_prompt, user_prompt=planner_user_prompt,
                        json_mode=True, phase="planner",
                        history=history_for_llm
                    )
            
                    plan_json = self._repair_json(plan_raw)
                    
                    is_valid_plan, validation_error = self._validate_plan(plan_json)

                    if is_valid_plan:
                        self.debug(f"Valid multi-step plan received on attempt {attempt + 1}.")
                        break
                    
                    self.debug(f"Plan validation failed: {validation_error}")
                    plan_json = None
                    
                    if "422" in plan_raw:
                        self.debug("!!! 422 Error detected. The API rejected the context.")
                        if not filters_cleared_on_retry:
                            self.debug("   -> Will retry ONCE with all active_filters cleared.")
                            filters_cleared_on_retry = True
                        else:
                            self.debug("   -> Already retried with cleared filters. Failing permanently.")
                            break
                    else:
                        self.debug(f"Attempt {attempt + 1} failed (Not a 422). Retrying...")
                    time.sleep(1)
                
                planner_duration = time.time() - planner_start_time
                
                if not plan_json:
                    outcome = "FAIL_PLANNER"
                    raise ValueError(f"AI failed to select a valid plan after {max_retries} attempts. Last error: {plan_raw}")

                # --- Multi-Step Execution Loop ---
                retrieval_start_time = time.time()
                step_results = {}
                collected_docs = []
                plan_steps = plan_json.get("plan", [])

                for i, step in enumerate(plan_steps):
                    step_num = i + 1
                    tool_call = step.get("tool_call", {})
                    tool_name = tool_call.get("tool_name")
                    params = tool_call.get("parameters", {})
                    
                    if not tool_name:
                        self.debug(f"Step {step_num} is missing a tool_name. Stopping plan.")
                        break
                    
                    try:
                        resolved_params = self._resolve_placeholders(params, step_results)
                        params_str = json.dumps(resolved_params)
                        if '$' in params_str:
                            unresolved = [v for v in re.findall(r'"(\$.*?)"', params_str)]
                            if unresolved:
                                raise ValueError(f"Plan failed at step {step_num}: Required value {unresolved[0]} was not found.")
                        self.debug(f"   -> Executing Step {step_num}: {tool_name} with params: {resolved_params}")
                    
                    except Exception as e:
                        self.debug(f"Error during placeholder resolution or execution for step {step_num}: {e}")
                        raise
                    
                    if tool_name == "finish_plan":
                        self.debug("Plan execution complete.")
                        break
                    
                    if tool_name in self.available_tools:
                        tool_function = self.available_tools[tool_name]
                        sig = inspect.signature(tool_function)
                        valid_params = {k: v for k, v in resolved_params.items() if k in sig.parameters}
                        dropped = [k for k in resolved_params if k not in sig.parameters]
                        if dropped:
                            self.debug(f"Dropping unexpected parameters for {tool_name}: {dropped}")
                        
                        step_output = tool_function(**valid_params)
                        step_output_list = step_output if isinstance(step_output, list) else [step_output]
                        step_results[step_num] = step_output_list
                        collected_docs.extend(step_output_list)
                    else:
                        raise ValueError(f"Plan step {step_num} uses an unknown tool: '{tool_name}'")
                
                retrieval_duration = time.time() - retrieval_start_time
                collected_docs = [doc for doc in collected_docs if doc.get("source_collection") not in ("system_signal", "system_note")]
                
                has_errors = any("error" in doc.get("status", "") for doc in collected_docs)
                is_empty = (not collected_docs or all("empty" in doc.get("status", "") for doc in collected_docs)) and not has_errors

                if has_errors:
                    execution_mode = "primary"
                    self.debug(f"Primary plan failed with an error. Reporting as FAIL_EXECUTION.")
                    outcome = "FAIL_EXECUTION"
                
                elif is_empty:
                    self.debug("Primary plan executed successfully but found no results. (FAIL_EMPTY)")
                    outcome = "FAIL_EMPTY"
                
                else:
                    outcome = "SUCCESS_DIRECT"

                if collected_docs:
                    self.debug(f"Original unfiltered doc count: {len(collected_docs)}. Starting de-duplication...")
                    unique_docs = {}
                    for doc in collected_docs:
                        try: content_key = json.dumps(doc.get('content'), sort_keys=True)
                        except TypeError: content_key = str(doc.get('content')) 
                        if content_key and content_key not in unique_docs:
                            unique_docs[content_key] = doc
                    collected_docs = list(unique_docs.values())
                    self.debug(f"Found {len(collected_docs)} unique documents after de-duplication.")

                if len(collected_docs) > 5:
                    primary_tool_name = ""
                    if plan_json and plan_json.get("plan"):
                        primary_tool_name = plan_json.get("plan", [{}])[0].get("tool_call", {}).get("tool_name", "")
                    
                    first_doc_meta = collected_docs[0].get("metadata", {})
                    is_student_data = "student_id" in first_doc_meta

                    if is_student_data and primary_tool_name == "find_people":
                        self.debug(f"-> Student result set ({len(collected_docs)} docs) detected for 'find_people'. Restructuring into groups.")
                        grouped_students = defaultdict(list)
                        for doc in collected_docs:
                            meta = doc.get("metadata", {})
                            group_key = f"{meta.get('course', 'N/A')} - Year {meta.get('year', 'N/A')} - Section {meta.get('section', 'N/A')}"
                            grouped_students[group_key].append(doc)
                        grouped_data = [{"source_collection": "grouped_students", "group_name": name, "students": docs} for name, docs in sorted(grouped_students.items())]
                        collected_docs = grouped_data
                    elif is_student_data:
                        self.debug(f"-> Skipping student grouping. Primary tool was '{primary_tool_name}', not 'find_people'.")

                self.debug("\n" + "="*50)
                self.debug(f"📑 Final {len(collected_docs)} documents being sent to Synthesizer:")
                try:
                    debug_output = json.dumps(collected_docs, indent=2)
                    print(debug_output)
                except Exception as e:
                    print(f"Could not print debug output: {e}")
                self.debug("="*50 + "\n")

                if outcome in ["SUCCESS_DIRECT", "SUCCESS_FALLBACK"]:
                    results_count = len(collected_docs)
                    self.debug(f"Cleaning {results_count} docs for Synthesizer...")
                    cleaned_docs_for_synth = self._clean_documents_for_synthesizer(collected_docs)
                    final_context = {
                        "status": "success",
                        "summary": f"Found {results_count} relevant document(s).",
                        "data": cleaned_docs_for_synth[:30]
                    }
                else:
                    final_context = {"status": "empty", "summary": "I tried a precise search and a broad search, but could not find any relevant documents."}

            except Exception as e:
                if planner_duration == 0.0 and 'planner_start_time' in locals():
                    planner_duration = time.time() - planner_start_time
                if retrieval_duration == 0.0 and 'retrieval_start_time' in locals():
                    retrieval_duration = time.time() - retrieval_start_time
                import traceback
                self.debug(f"An unexpected error occurred: {e}")
                self.debug(f"Error Type: {type(e)}")
                self.debug(f"Traceback: {traceback.format_exc()}")
                error_msg = str(e)
                if outcome == "FAIL_UNKNOWN":
                    outcome = "FAIL_EXECUTION"
                final_context = {"status": "error", "summary": f"I ran into a technical problem: {e}"}

            # --- Synthesizer Block (Using ONLINE prompt) ---
            self.debug("Synthesizing final answer...")
            synth_start_time = time.time()
            context_for_llm = json.dumps(final_context, indent=2, ensure_ascii=False)
            
            # --- THIS IS THE ORIGINAL "ONLINE" PROMPT ---
            synth_prompt = PROMPT_TEMPLATES["final_synthesizer"].format(context=context_for_llm, query=query)
            
            final_answer = self.synth_llm.execute(
                system_prompt="You are a careful AI analyst who provides conversational answers based only on the provided facts.",
                user_prompt=synth_prompt, 
                history=chat_history if not filters_cleared_on_retry else [],
                phase="synth"
            )
            # --- END OF CHANGE ---
            
            synth_duration = time.time() - synth_start_time

            # --- Post-Synthesis Block ---
            corruption_details = sorted(list(self.corruption_warnings)) if self.corruption_warnings else None
            final_plan_hash = None 

            try:
                failure_keywords = ["i'm sorry", "unfortunately", "i couldn't find", "i am unable", "not available", "technical problem"]
                is_successful_answer = not any(keyword in final_answer.lower() for keyword in failure_keywords)
                if outcome.startswith("SUCCESS") and is_successful_answer and plan_json:
                    self.debug("Saving example to memory: SUCCESS and final answer looks good.")
                    final_plan_hash = self._save_dynamic_example(query, plan_json, session, outcome)
                elif outcome.startswith("SUCCESS"):
                    self.debug("Skipping example save: Final answer looked like a soft failure.")
            except Exception as e:
                self.debug(f"Post-synthesis evaluation or example saving failed: {e}")

            # --- Logging Block ---
            execution_time = time.time() - start_time
            self.training_system.record_query_result(
                query=query, plan=plan_json, results_count=results_count,
                execution_time=execution_time, error_msg=error_msg,
                execution_mode=execution_mode, outcome=outcome, analyst_mode=self.execution_mode,
                final_answer=final_answer, corruption_details=corruption_details,
                timestamp=start_datetime, session_id=session.get('session_id'),
                planner_duration=planner_duration, retrieval_duration=retrieval_duration,
                synth_duration=synth_duration, planner_model=self.planner_llm.planner_model,
                synth_model=self.synth_llm.synth_model, plan_hash=final_plan_hash
            )
            
            return final_answer, plan_json, collected_docs
            # --- END ONLINE/SPLIT EXECUTION PATH ---



    def web_start_ai_analyst(self, user_query: str, session_id: str):
        """
        [CORRECTED VERSION] Executes the AI plan for a specific user session.
        """
        user_query = user_query.strip()

        # 1. Get the specific session for this user (removes all old file logic)
        session = self._get_or_create_session(session_id)

        # 2. Execute the AI plan, passing the full session object
        final_answer, plan_json, collected_docs = self.execute_reasoning_plan(user_query, session=session)

        # 3. Update this session's history with the new exchange
        self._update_session_history(session_id, user_query, final_answer)

        # 4. Trigger the conversation summarizer
        self._summarize_conversation(session_id)

        # 5. Perform data reconciliation for the UI (this logic remains the same)
        synced_structured_data = []
        if collected_docs and "system_summary" not in collected_docs[0].get("source_collection", ""):
            # ... (your existing reconciliation logic is correct and does not need to change)
            for doc in collected_docs:
                student_name = doc.get("metadata", {}).get("full_name")
                if student_name:
                    name_parts = [part.strip() for part in student_name.replace(",", "").lower().split()]
                    if all(part in final_answer.lower() for part in name_parts):
                        meta = doc.get("metadata", {})
                        synced_structured_data.append({
                            "full_name": meta.get("full_name"),
                            "student_id": meta.get("student_id"),
                            "program": meta.get("course") or meta.get("program"),
                            "year": meta.get("year") or meta.get("year_level"),
                            "section": meta.get("section"),
                            "image_url": meta.get("image_url"),
                            "raw": doc
                        })

        if not synced_structured_data:
            synced_structured_data = collected_docs
            
        # 6. Assemble and return the final response
        final_response = {
            "ai_response": final_answer,
            "structured_data": synced_structured_data,
        }

        return final_response
    

    # In analyst.py, replace the existing _create_image_map method with this


    # In analyst.py, replace the entire _create_image_map method with this

    def _create_image_map(self, ai_response_text: str) -> dict:
        """
        [CORRECTED] Searches MongoDB for Base64 image data by parsing PDM IDs
        and names directly from the final AI response text.
        """
        image_map = {"by_id": {}, "by_name": {}}
        
        # --- Use regex to find all IDs and Names in the final response string ---
        # Pattern for IDs like PDM-2025-000123
        ids = re.findall(r"(PDM-\d{4}-\d{6})", ai_response_text)
        # Pattern for names formatted as "Lastname, Firstname"
        names = re.findall(r"([A-Z][a-z]+,\s[A-Z][a-z]+)", ai_response_text)

        all_collections = self.mongo_db.list_collection_names()

        def find_image_in_db(filter_query):
            """Helper to search all collections for a document with image data."""
            for coll_name in all_collections:
                coll = self.mongo_db[coll_name]
                record = coll.find_one(filter_query, {"image.data": 1, "student_id": 1})
                if record and record.get("image", {}).get("data"):
                    return record
            return None

        # Map by PDM ID
        for pid in set(ids):
            record = find_image_in_db({"student_id": pid})
            if record:
                image_map["by_id"][pid] = record["image"]["data"]

        # Map by Name (only if not already found via ID)
        for name in set(names):
            record = find_image_in_db({"full_name": name})
            if not record:
                continue
            student_id = record.get("student_id")
            if not student_id or student_id not in image_map["by_id"]:
                image_map["by_name"][name] = record["image"]["data"]

        return image_map


        def find_image_in_db(filter_query):
            """Helper to search all collections for a document with image data."""
            for coll_name in all_collections:
                # Use the class's db connection
                coll = self.mongo_db[coll_name]
                # Projection to only fetch the fields we need for efficiency
                record = coll.find_one(filter_query, {"image.data": 1, "student_id": 1, "full_name": 1})
                if record and record.get("image", {}).get("data"):
                    return record
            return None

        # Map by PDM ID
        for pid in set(ids): # Use set() to avoid duplicate lookups
            record = find_image_in_db({"student_id": pid})
            if record:
                image_map["by_id"][pid] = record["image"]["data"]

        # Map by Name (only if not already found via ID)
        for name in set(names): # Use set() to avoid duplicate lookups
            record = find_image_in_db({"full_name": name})
            if not record:
                continue

            student_id = record.get("student_id")
            if not student_id or student_id not in image_map["by_id"]:
                image_map["by_name"][name] = record["image"]["data"]

        return image_map
        


# -------------------------------
# Function use for terminal
# -------------------------------

    def start_ai_analyst(self):
        """
        [CORRECTED VERSION] Starts an interactive loop with full session management.
        """
        print("\n" + "="*70)
        print("AI SCHOOL ANALYST (In-Memory Session with Summarization)")
        print("   Type 'exit' to quit. Memory will be cleared on exit.")
        print("="*70)

        terminal_session_id = "terminal_user_01"
        session = self._get_or_create_session(terminal_session_id)

        last_query = None
        last_plan_for_training = None

        while True:
            q = input("\nYou: ").strip()
            if not q: continue

            if q.lower() == "exit":
                print("Exiting. Session memory will be cleared.")
                break


            # --- ADD THIS ENTIRE BLOCK TO SIMULATE THE UI EVENT ---
            if q.startswith("_event:"):
                self.debug(f"Event command detected: {q}")
                parts = q.split(':')
                
                # Expected format: _event:recognize:<id_or_name>:<value>
                if len(parts) >= 4 and parts[1] == "recognize":
                    identifier_type = parts[2]
                    value = ":".join(parts[3:]) # Join back in case the name has colons
                    
                    event_data = {}
                    if identifier_type == "id":
                        event_data = {"student_id": value}
                    elif identifier_type == "name":
                        event_data = {"full_name": value}
                    else:
                        print("Analyst: Invalid event format. Use '_event:recognize:id:<student_id>' or '_event:recognize:name:<full_name>'.")
                        continue

                    # Call your new event handler directly
                    greeting_message = self.handle_user_recognized_event(event_data)
                    print("\nAnalyst:", greeting_message)
                    self._update_session_history(terminal_session_id, q, greeting_message)
                    self._summarize_conversation(terminal_session_id)
                    
                else:
                    print("Analyst: Invalid event format.")
                
                continue # Skip the rest of the loop and ask for the next input
            # --- END OF NEW BLOCK ---
            

            # --- ADD THIS NEW BLOCK ---
            if q.lower() == "insights":
                print("\n---  AI Performance Insights ---")
                insights = self.training_system.get_training_insights()
                print(insights)
                print("---------------------------------\n")
                continue
            # --- END OF NEW BLOCK ---

            if q.lower() == "train":
                # ... (this part is fine)
                continue

            # --- FIX 1: Pass the entire 'session' object, not just its history ---
            final_answer, plan_json, collected_docs = self.execute_reasoning_plan(q, session=session)

            # Update the session history in memory and MongoDB
            self._update_session_history(terminal_session_id, q, final_answer)

            if final_answer.strip().endswith("?"):
                self.debug("AI's answer was a question. Setting 'clarification_pending'.")
                # We can just modify the session object in memory for the terminal
                session["structured_context"]["clarification_pending"] = True
                session["structured_context"]["original_ambiguous_query"] = q

            # --- FIX 2: Add the call to the summarizer ---
            self._summarize_conversation(terminal_session_id)

            print("\nAnalyst:", final_answer)

            # The rest of your file-saving logic is correct.
            image_map = self._create_image_map(final_answer)
            output_for_file = {
                "ai_response": final_answer,
                "structured_data": collected_docs,
                "image_map": image_map
            }
            output_filename = "latest_response_data.json"
            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(output_for_file, f, indent=2, default=str)
            print(f" Detailed data and image map saved to '{output_filename}'")

            if plan_json and "plan" in plan_json:
                last_query = q
                last_plan_for_training = plan_json