# backend/utils/ai_core/analyst.py

"""
This module contains the main AIAnalyst class, which orchestrates the entire
AI reasoning and tool-use pipeline.
"""

# Standard library imports
import json
import re
import time
import os
import inspect
import hashlib
from typing import Dict, Any, List, Optional
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Third-party imports
from pymongo import MongoClient

# Local (ai_core) imports
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

    def __init__(self, collections: List[str], llm_config: Optional[dict] = None, execution_mode: str = "split"):
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
            print(f"✅ Successfully connected to MongoDB database: '{mongo_db_name}'")
        except Exception as e:
            print(f"❌ Failed to connect to MongoDB: {e}")
            raise
            
        self.collections = {name: MongoCollectionAdapter(self.mongo_db[name]) for name in collections}
        print(f"📚 AI Analyst is now using MongoDB collections: {list(self.collections.keys())}")
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
        self.all_doc_types = self._get_unique_document_types()
            
        self.training_system = TrainingSystem()
        self.dynamic_examples = self._load_dynamic_examples()
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
        }


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
                "mentioned_entities": [],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            self.sessions_cache[session_id] = new_session
            return new_session

    def _update_session_history(self, session_id: str, user_query: str, ai_response: str):
        """
        [MODIFIED FOR MONGO] Adds the latest exchange to the session's chat history,
        trims it, and saves the entire session object back to MongoDB.
        """
        # Get the current session object (from cache or DB)
        session = self._get_or_create_session(session_id)
        
        # Append the new messages
        session["chat_history"].append({"role": "user", "content": user_query})
        session["chat_history"].append({"role": "assistant", "content": ai_response})
        
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


    

    def _summarize_conversation(self, session_id: str):
        """
        Calls an LLM to create or update a conversation summary for a given session.
        """
        self.debug(f"Updating conversation summary for session: {session_id}")
        session = self._get_or_create_session(session_id)
        
        # We need at least one full user/AI turn to create a summary.
        if len(session["chat_history"]) < 2:
            return

        previous_summary = session.get("conversation_summary", "None.")
        
        # Get the last user/AI exchange
        latest_exchange = "\n".join([
            f"User: {session['chat_history'][-2]['content']}",
            f"Assistant: {session['chat_history'][-1]['content']}"
        ])

        # Prepare the prompt for the summarizer LLM
        prompt = PROMPT_TEMPLATES["conversation_summarizer"].format(
            summary=previous_summary,
            latest_exchange=latest_exchange
        )

        # Use the planner_llm (typically a faster/cheaper model) for this quick task
        new_summary = self.planner_llm.execute(
            system_prompt="You are a conversation summarizer.",
            user_prompt=prompt,
            phase="synth" # Use synth phase if it points to a faster model
        )

        # Update the session object with the new summary and save it to the database
        if new_summary and "error" not in new_summary.lower():
            session["conversation_summary"] = new_summary
            session["updated_at"] = datetime.now(timezone.utc)
            self.sessions_collection.update_one(
                {"session_id": session_id},
                {"$set": {
                    "conversation_summary": new_summary,
                    "updated_at": session["updated_at"]
                }},
                upsert=True
            )
            self.debug(f"New summary for {session_id}: {new_summary}")


    # Add this new method anywhere inside the AIAnalyst class in AI.py

    def _add_entity_to_session(self, session_id: str, entity_name: str):
        """
        Adds a new entity to the session's memory and keeps the list trimmed.
        """
        session = self._get_or_create_session(session_id)
        
        # Add the new entity to the end of the list
        session["mentioned_entities"].append(entity_name)
        
        # Keep only the last 5 mentioned entities to keep the list relevant
        if len(session["mentioned_entities"]) > 5:
            session["mentioned_entities"] = session["mentioned_entities"][-5:]
            
        # Persist the change to the database
        session["updated_at"] = datetime.now(timezone.utc)
        self.sessions_collection.update_one(
            {"session_id": session_id},
            {"$set": {
                "mentioned_entities": session["mentioned_entities"],
                "updated_at": session["updated_at"]
            }},
            upsert=True
        )
        self.debug(f"Updated entity memory for {session_id}: {session['mentioned_entities']}")


    
        

    def _get_unique_document_types(self) -> List[str]:
        """Queries the database to get all unique, non-empty document types."""
        self.debug("🔎 Discovering unique document types from the database...")
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
    
    def get_school_info(self, topic: Any = None) -> List[dict]:
        """
        [UPGRADED] A tool for retrieving general school information.
        """
        self.debug(f"🛠️ Running upgraded tool: get_school_info for topic: {topic}")

        filters = {}
        document_type_to_find = None

        if isinstance(topic, str):
            # --- ✨ NEW FIX: Check for keywords within the string ---
            topic_lower = topic.lower()
            if 'mission' in topic_lower or 'vision' in topic_lower:
                document_type_to_find = 'mission_vision'
            else:
                document_type_to_find = topic_lower
            # --- END NEW FIX ---

        elif isinstance(topic, list) and topic:
            # This handles the case where the planner correctly sends a list
            document_type_to_find = "_".join(t.lower() for t in topic)

        elif not topic:
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

        filters = {'document_type': document_type_to_find}
        return self.search_database(filters=filters)
    
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
        query_text = "academic program curriculum" 

        # Build metadata filters for precise collection matching
        query_text = "academic program curriculum" 
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
            collection_filter="curriculum"
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
                    all_related_docs.extend(self.search_database(filters={"student_id": student_id}, collection_filter="_grades"))
            
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
        self.debug("🛠️ Running upgraded tool: get_database_summary")
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
                self.debug(f"⚠️ Could not get info for collection {name}: {e}")
        
        return summary_docs
    
    def get_student_grades(self, student_name: str = None, program: str = None, year_level: int = None) -> List[dict]:
        """
        Tool: Finds grade documents for a specific student by name, or for a group of students
        by program and/or year level. Can also retrieve all grades if no filters are provided.
        """
        self.debug(f"Running grade tool for name='{student_name}', program='{program}', year='{year_level}'")
        
        # Normalize year_level=0 to None for broader matching
        if year_level == 0:
            year_level = None

        # Priority 1: Search by a specific student's name
        if student_name:
            self.debug(f"-> Prioritizing search by name: {student_name}")
            entity = self.resolve_person_entity(name=student_name)
            if not entity or not entity.get("primary_document"):
                return [{"status": "error", "summary": f"Could not find a student named '{student_name}'."}]
            
            student_docs = entity["primary_document"]
            student_ids = [doc.get("metadata", {}).get("student_id") for doc in student_docs if doc.get("metadata", {}).get("student_id")]
            
            if not student_ids:
                return student_docs + [{"status": "empty", "summary": f"Found student(s) named '{student_name}' but they are missing student IDs needed to find grades."}]
            
            # Find all grades for all found student IDs in a single query
            grade_docs = self.search_database(filters={"student_id": {"$in": student_ids}}, collection_filter="_grades")
            if not grade_docs:
                return student_docs + [{"status": "empty", "summary": f"Found student(s) named '{student_name}' but could not find any grade information for them."}]
            
            return student_docs + grade_docs

        # Priority 2: Search by a group (program and/or year)
        if program or year_level:
            self.debug(f"-> Searching for group: program={program}, year_level={year_level}")
            student_filters = {}
            if program:
                student_filters['program'] = program
            if year_level:
                student_filters['year_level'] = year_level

            student_docs = self.find_people(**student_filters)
            if not student_docs or "status" in (student_docs[0] or {}).get("metadata", {}):
                return [{"status": "empty", "summary": f"I couldn't find any students matching those criteria."}]
                
            student_ids = [doc.get("metadata", {}).get("student_id") for doc in student_docs if doc.get("metadata", {}).get("student_id")]
            if not student_ids:
                return [{"status": "empty", "summary": "Found students, but they are missing IDs needed to find grades."}]

            grade_filters = {"student_id": {"$in": student_ids}}
            grade_docs = self.search_database(filters=grade_filters, collection_filter="_grades")
            
            if not grade_docs:
                return student_docs + [{"status": "empty", "summary": "Could not find any grade information for the specified students."}]

            return student_docs + grade_docs
        
        # Priority 3: No filters provided, retrieve all grade documents
        if not student_name and not program and not year_level:
            self.debug("-> No filters provided. Retrieving all grade documents.")
            all_grade_docs = self.search_database(collection_filter="_grades")
            if not all_grade_docs:
                return [{"status": "empty", "summary": "I could not find any grade documents in the database."}]
            return all_grade_docs

        return [{"status": "error", "summary": "To get grades, please provide a specific student's name, a program, or a year level."}]
    
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
                schedule_filters = {k: v for k, v in {"program": meta.get("program"), "year_level": meta.get("year_level"), "section": meta.get("section")}.items() if v}
                if schedule_filters:
                    person_docs.extend(self.search_database(filters=schedule_filters, collection_filter="schedules"))
                if student_id:
                    person_docs.extend(self.search_database(filters={"student_id": student_id}, collection_filter="_grades"))
            
            elif "faculty" in source_collection:
                schedule_filters = {"$or": [{"adviser": {"$in": aliases}}, {"staff_name": {"$in": aliases}}]}
                person_docs.extend(self.search_database(filters=schedule_filters, collection_filter="schedules"))
                person_docs.extend(self.search_database(filters=schedule_filters, collection_filter="faculty_library_non_teaching_schedule"))

        self.debug(f"-> Collected {len(person_docs)} total documents for the QA context.")

        # Step 3: Create a focused context for the Synthesizer
        context_for_qa = json.dumps({
            "status": "success",
            "data": person_docs
        }, indent=2, ensure_ascii=False)
        
        # Step 4: Call the Synthesizer LLM to perform the specific QA task
        qa_user_prompt = f"Based ONLY on the Factual Documents provided, please answer the following question concisely.\n\nFactual Documents:\n{context_for_qa}\n\nQuestion: {question}"
        
        specific_answer = self.synth_llm.execute(
            system_prompt="You are a helpful assistant that answers specific questions based ONLY on the provided Factual Documents. Do not use any outside knowledge.",
            user_prompt=qa_user_prompt,
            phase="synth"
        )

        # Step 5: Return the specific answer along with the source documents
        return [
            {"source_collection": "qa_answer", "content": specific_answer, "metadata": {"question": question}}
        ] + person_docs
        
    def find_people(self, name: str = None, role: str = None, program: str = None, year_level: int = None, section: str = None, department: str = None, employment_status: str = None, n_results: int = 1000) -> List[dict]: # Add n_results=50 here
        """
        Tool (Unified): A powerful, single tool to find any person or group (students or faculty)
        using a combination of filters.
        """

        try:
            n_results = int(n_results)
        except (ValueError, TypeError):
            n_results = 1000

        self.debug(f"Running MERGED tool: find_people with params: name='{name}', role='{role}', program='{program}', dept='{department}'")
        filters = {}
        collection_filter = None



        if isinstance(role, list) and len(role) == 1:
            self.debug(f"-> Normalizing single-item role list {role} to a string.")
            role = role[0]


        # --- WILDCARD SEARCH: If no parameters are given, return all people ---
        if not any([name, role, program, year_level, section, department, employment_status]):
            self.debug("-> No parameters provided. Searching for all students and faculty.")
            return self.search_database(query_text="*", collection_filter="students,faculty")

        # Intelligently determine if the search is for students or faculty
        is_student_query = False
        if isinstance(role, str) and role.lower() == 'student':
            is_student_query = True
        elif isinstance(role, list) and 'student' in [r.lower() for r in role]:
            is_student_query = True
        
        if is_student_query or program or year_level or section:
            self.debug("-> Query identified as a STUDENT search.")
            collection_filter = "students"
            if program: filters['program'] = program
            if year_level: filters['year_level'] = year_level
            if section: filters['section'] = section
            
            # This is safer than a wildcard search.
            if not filters and is_student_query:
                student_only_filter = {"student_id": {"$exists": True}}
                return self.search_database(filters=student_only_filter, collection_filter=collection_filter)
            
            # If it's a student query but no specific filters were found, search all students.
            if not filters and not name:
                self.debug("-> No specific student filters. Searching for all students.")
                return self.search_database(query_text="*", collection_filter="students")
        else:
            self.debug("-> Query identified as a FACULTY/STAFF search.")
            collection_filter = "faculty"
            
            if role:
                # Handle a list of roles using the '$in' operator
                if isinstance(role, list):
                    filters['position'] = {'$in': role}
                elif isinstance(role, str):
                    role_lower = role.lower()
                    if 'faculty' in role_lower or 'professor' in role_lower:
                        #Dynamically get all faculty types ---
                        all_faculty_types = self._get_unique_faculty_types()
                        if all_faculty_types:
                            filters['faculty_type'] = {'$in': all_faculty_types}
                        else:
                            # Fallback if no types are found, just search by position
                            filters['position'] = role.upper()
                    else:
                        filters['position'] = role.upper()

            if department and department.lower() != 'all':
                filters['department'] = department

            if employment_status:
                filters['employment_status'] = employment_status

            # If it's a faculty query but no specific filters were found, search all faculty.
            if not filters and not name:
                self.debug("-> No specific faculty filters. Searching for all faculty.")
                return self.search_database(query_text="*", collection_filter="faculty")

        # Enhance the search with entity resolution if a name is provided
        if name:
            self.debug(f"-> Name provided. Using robust entity resolution for '{name}'.")
            entity = self.resolve_person_entity(name=name)
            if entity and entity.get("aliases"):
                filters['full_name'] = {"$in": entity["aliases"]}
                if not role and not is_student_query:
                    collection_filter = None # Search all collections if role is ambiguous
                    self.debug("-> Name search with no role, searching all collections.")
            else:
                return [{"status": "empty", "summary": f"Could not find anyone named '{name}'."}]

        if not filters:
            return [{"status": "error", "summary": "Please provide criteria to find people."}]
        
        
        return self.search_database(filters=filters, collection_filter=collection_filter, n_results=n_results) # Pass n_results down
    
    def get_person_schedule(self, person_name: str = None, program: str = None, year_level: int = None, section: str = None) -> List[dict]:
        """
        Tool (Unified): Retrieves schedules for a specific person by name, or for a group of
        students by program, year, and/or section.
        """
        self.debug(f"Running schedule tool for person='{person_name}', program={program}, year={year_level}, section={section}")

        # Normalize year_level input (e.g., '1st year' -> 1)
        if year_level:
            try:
                year_str = str(year_level)
                match = re.search(r'\d+', year_str)
                if match:
                    year_level = int(match.group(0))
                else:
                    year_level = None 
            except (ValueError, TypeError):
                year_level = None

        # Case 1: Search for a group schedule
        if program or year_level or section or (person_name and "student" in person_name.lower() and len(person_name.split()) <= 2):
            filters = {}
            if program: filters["program"] = program
            if year_level: filters["year_level"] = year_level
            if section: filters["section"] = section

            if person_name and "student" in person_name.lower() and not program:
                prog_guess = person_name.split()[0].upper()
                filters["program"] = prog_guess

            self.debug(f"-> Running group schedule search with filters={filters}")
            schedule_docs = self.search_database(filters=filters, collection_filter="schedules")

            if not schedule_docs:
                return [{"status": "empty", "summary": "No schedules found for the specified group."}]
            return schedule_docs

        # Case 2: Search for a specific person's schedule
        if person_name:
            self.debug(f"-> Resolving entity for person: {person_name}")
            entity = self.resolve_person_entity(name=person_name)
            if not entity or not entity.get("primary_document"):
                return [{"status": "error", "summary": f"Could not find anyone matching '{person_name}'."}]
            
            document_list = entity["primary_document"]
            all_found_docs = [] 
            aliases = entity.get("aliases", [])
            primary_name_for_log = entity["primary_name"]
            
            if primary_name_for_log not in aliases:
                aliases = [primary_name_for_log] + aliases

            # Loop through every person document found by the resolver to get their schedule
            for person_record in document_list:
                all_found_docs.append(person_record) # Include the person's profile in results
                
                meta = person_record.get("metadata", {})
                source_collection = person_record.get("source_collection", "")
                current_person_name = meta.get("full_name", "N/A")
                self.debug(f"-> Processing schedule search for '{current_person_name}'...")
                
                schedule_docs = []

                if "student" in source_collection:
                    schedule_filters = {
                        "program": meta.get("program") or meta.get("course"),
                        "year_level": meta.get("year_level") or meta.get("year"),
                        "section": meta.get("section")
                    }
                    if not all(schedule_filters.values()):
                        all_found_docs.append({"source_collection": "system_note", "content": f"Student record for '{current_person_name}' is missing key details (program/year/section) to find a schedule.", "metadata": {"status": "error"}})
                        continue
                    schedule_docs = self.search_database(filters=schedule_filters, collection_filter="schedules")

                elif "faculty" in source_collection:
                    schedule_filters = {
                        "$or": [
                            {"adviser": {"$in": aliases}},
                            {"staff_name": {"$in": aliases}},
                            {"full_name": {"$in": aliases}}
                        ]
                    }
                    schedule_docs = self.search_database(filters=schedule_filters, collection_filter="schedules")
                    if not schedule_docs:
                        schedule_docs = self.search_database(filters=schedule_filters, collection_filter="faculty_library_non_teaching_schedule")

                else:
                    all_found_docs.append({"source_collection": "system_note", "content": f"Could not determine if '{current_person_name}' is a student or faculty.", "metadata": {"status": "error"}})
                    continue

                if not schedule_docs:
                    all_found_docs.append({"source_collection": "system_note", "content": f"Found person '{current_person_name}' but could not find a matching schedule.", "metadata": {"status": "empty"}})
                else:
                    all_found_docs.extend(schedule_docs)

            return all_found_docs

        return [{"status": "error", "summary": "Please provide a person's name or a group filter (program, year, section)."}]

    def get_adviser_info(self, program: str, year_level: int) -> List[dict]:
        """
        Tool: Finds the adviser for a student group and retrieves their faculty profile.
        """
        self.debug(f"Running tool: get_adviser_info for {program} Year {year_level}")
        
        schedule_docs = self.search_database(filters={"program": program, "year_level": year_level}, collection_filter="schedules")
        if not schedule_docs or "adviser" not in schedule_docs[0].get("metadata", {}):
            return [{"status": "empty", "summary": f"Could not find a schedule or adviser for {program} Year {year_level}."}]
        
        adviser_name = schedule_docs[0]["metadata"]["adviser"]
        adviser_entity = self.resolve_person_entity(name=adviser_name)
        faculty_docs = adviser_entity.get("primary_document", [])
        
        return schedule_docs + faculty_docs

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

    def resolve_person_entity(self, name: str) -> dict:
        """
        Tool: Finds all documents and name variations (aliases) for a person using a
        multi-pronged search strategy (semantic + substring) and fuzzy matching.
        This is a core component for accurately finding people.
        """
        self.debug(f"Resolving entity for: '{name}'")
        
        # Clean the input name to create search terms
        original_query = name.lower()
        aggressive_clean_pattern = r'\b(PROFESSOR|DR|DOCTOR|MR|MS|MRS|JR|SR|I|II|III|IV)\b\.?|[^\w\s]'
        cleaned_name = re.sub(aggressive_clean_pattern, '', name, flags=re.IGNORECASE)
        cleaned_query = ' '.join(cleaned_name.split()).lower()
        
        search_terms = list(set([term for term in [original_query, cleaned_query] if term]))
        self.debug(f"   -> Performing multi-pronged search for: {search_terms}")

        # 1. Perform both semantic and substring searches
        all_results = []
        for term in search_terms:
            all_results.extend(self.search_database(query=term))
            all_results.extend(self.search_database(document_filter={"$contains": term}))

        # 2. De-duplicate results
        initial_results = list({doc['content']: doc for doc in all_results}.values())
        
        if not initial_results:
            return {}

        # 3. Gather all potential name aliases from the found documents
        potential_names, primary_name = {name.title()}, name.title()
        for result in initial_results:
            meta = result.get('metadata', {})
            fields = ['full_name', 'adviser', 'staff_name', 'student_name']
            for field in fields:
                if meta.get(field): potential_names.add(str(meta[field]).strip().title())
        
        # 4. Use the fuzzy matcher to build the final alias list
        resolved_aliases = {primary_name}
        for p_name in potential_names:
            if self._fuzzy_name_match(primary_name, p_name):
                resolved_aliases.add(p_name)
                if len(p_name) > len(primary_name): primary_name = p_name

        # --- PATCH START: INTELLIGENT NAME MATCHING ---
        # 5. Filter the initial results to keep only definitive matches
        matching_docs = []
        query_parts = set(cleaned_query.split()) 
        
        for doc in initial_results:
            meta = doc.get("metadata", {})
            full_name_in_doc = meta.get("full_name", "").lower()

            # Create a set of all individual name words from the document for robust matching.
            # This handles formats like "Carpenter, Michael" and "Jared Escobar" equally well.
            doc_name_parts = set(full_name_in_doc.replace(",", "").split())

            # If all parts of the user's search query are found within the document's name parts, consider it a match.
            # This will correctly match a search for "escobar" to the document for "Jared Escobar".
            if query_parts.issubset(doc_name_parts):
                matching_docs.append(doc)
        
        # Determine the best primary name from the actual matches
        final_primary_name = primary_name
        if matching_docs:
            final_primary_name = max([doc.get("metadata", {}).get("full_name", "") for doc in matching_docs], key=len)
            self.current_query_entities.append(final_primary_name) # <-- ADD THIS LINE

        self.debug(f"Entity resolved: Primary='{final_primary_name}', Aliases={list(resolved_aliases)}, Found {len(matching_docs)} docs.")
        
        return {
            "primary_name": final_primary_name,
            "aliases": list(resolved_aliases),
            "primary_document": matching_docs 
        }

    def get_person_profile(self, person_name: str) -> List[dict]:
        """
        Tool: Retrieves only the main profile document for a specific person.
        Used for general 'who is...' queries.
        """
        self.debug(f"Running FOCUSED tool: get_person_profile for '{person_name}'")
        
        entity = self.resolve_person_entity(name=person_name)
        
        if entity and entity.get("primary_document"):
            return entity["primary_document"]
        
        return [{"status": "empty", "summary": f"I could not find a profile for anyone named '{person_name}'."}]
        
    def debug(self, *args):
        """Prints messages only if the analyst is in debug mode."""
        if self.debug_mode:
            print(*args)
            
    def _load_dynamic_examples(self) -> str:
        """
        Loads user-provided training examples from a JSON file to be injected
        into the planner's prompt, improving its accuracy on specific query types.
        """
        file_path = "config/dynamic_examples.json"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                examples_list = json.load(f)
                
                if not isinstance(examples_list, list):
                    self.debug(f"{file_path} is not a list. Ignoring examples.")
                    return ""

                example_strings = []
                for example in examples_list:
                    example_str = f"""
        **EXAMPLE (User-Provided):**
        User Query: "{example['query']}"
        Your JSON Response:
        {json.dumps(example['plan'], indent=2, ensure_ascii=False)}
        """
                    example_strings.append(example_str)
                return "".join(example_strings)
        except (FileNotFoundError, json.JSONDecodeError):
            self.debug(f"{file_path} not found. Starting with no dynamic examples.")
            return ""

    def _save_dynamic_example(self, query: str, plan: dict):
        """
        Saves a successful query and its generalized plan as a new training example
        to the dynamic examples JSON file.
        """
        file_path = "config/dynamic_examples.json"
        
        # Generalize the example by replacing specific names with a placeholder
        name_to_generalize = None
        if plan and isinstance(plan.get("plan"), list) and plan["plan"]:
            first_step_params = plan["plan"][0].get("tool_call", {}).get("parameters", {})
            if "person_name" in first_step_params:
                name_to_generalize = first_step_params["person_name"]
            elif "student_name" in first_step_params:
                name_to_generalize = first_step_params["student_name"]
            elif "name" in first_step_params:
                name_to_generalize = first_step_params["name"]

        if name_to_generalize and isinstance(name_to_generalize, str):
            query = re.sub(name_to_generalize, "[Person's Name]", query, flags=re.IGNORECASE)
            plan_str = json.dumps(plan)
            plan_str = re.sub(f'"{re.escape(name_to_generalize)}"', '"[Person\'s Name]"', plan_str, flags=re.IGNORECASE)
            plan = json.loads(plan_str)

        # Simplify the plan to only include the essential tool call information
        simplified_plan = {}
        try:
            simplified_plan = plan["plan"][0]["tool_call"]
        except (KeyError, IndexError, TypeError):
            self.debug(f"Could not simplify plan structure for saving. Check plan format.")
            return

        # Load existing examples and append the new one, avoiding duplicates
        examples_list = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                examples_list = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass 

        for ex in examples_list:
            if ex["query"] == query:
                self.debug("Duplicate generalized query found. Not saving.")
                return

        examples_list.append({"query": query, "plan": simplified_plan})

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(examples_list, f, indent=2, ensure_ascii=False)
        self.debug("New example saved to dynamic_examples.json.")

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
            'student_id': ('stud_id', 'id', 'student_number')
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
        
    def _resolve_placeholders(self, params: dict, step_results: dict) -> dict:
        """
        Recursively searches for and replaces placeholders (e.g., '$program_from_step_1')
        in a step's parameters with actual values from the results of previous steps.
        """
        resolved_params = json.loads(json.dumps(params))

        # Map standard field names to their original variants
        forward_map = {}
        for original, standard in self.REVERSE_SCHEMA_MAP.items():
            forward_map.setdefault(standard, []).append(original)

        def normalize_for_search(key: str, value: Any):
            """
            Turns a scalar value into a forgiving filter dictionary for the database,
            expanding it to include common aliases (e.g., 'BSCS' -> 'BS COMPUTER SCIENCE').
            """
            COURSE_ALIASES = {
                "BSCS": ["BSCS", "BS COMPUTER SCIENCE", "BS Computer Science"],
                "BSTM": ["BSTM", "BS TOURISM MANAGEMENT", "BS Tourism Management"],
                "BSOA": ["BSOA", "BS OFFICE ADMINISTRATION", "BS Office Administration"],
                "BECED": ["BECED", "BACHELOR OF EARLY CHILDHOOD EDUCATION", "Bachelor of Early Childhood Education"],
                "BSIT": ["BSIT", "BS INFORMATION TECHNOLOGY", "BS Information Technology", "BSINFORMATION"],
                "BSHM": ["BSHM", "BS HOSPITALITY MANAGEMENT", "BS Hospitality Management"],
                "BTLE": ["BTLE", "BACHELOR OF TECHNOLOGY AND LIVELIHOOD EDUCATION", "Bachelor of Technology and Livelihood Education"]
            }
            
            if isinstance(value, dict) and any(op in value for op in ("$in", "$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$nin")):
                return value

            scalars: List[Any] = [value] if value is not None else []
            out: List[Any] = []

            if key == "program":
                for v in scalars:
                    v_str_u = str(v).upper()
                    matched = False
                    for prog_key, alias_list in COURSE_ALIASES.items():
                        alias_upper = [a.upper() for a in alias_list]
                        if v_str_u == prog_key or v_str_u in alias_upper:
                            out.extend(alias_list)
                            matched = True
                            break
                    if not matched:
                        out.append(v)
                return {"$in": [str(x) for x in list(dict.fromkeys(out))]}

            if key == "year_level":
                for v in scalars:
                    vs = str(v).strip()
                    out.extend([vs, f"Year {vs}", f"{vs}st Year", f"{vs}nd Year", f"{vs}rd Year", f"{vs}th Year"])
                    if vs == "1": out.extend(["1st Year", "First Year", "Year I"])
                    if vs == "2": out.extend(["2nd Year", "Second Year", "Year II"])
                    if vs == "3": out.extend(["3rd Year", "Third Year", "Year III"])
                    if vs == "4": out.extend(["4th Year", "Fourth Year", "Year IV"])
                return {"$in": list(dict.fromkeys(out))}
            
            if key == "section":
                for v in scalars:
                    vs = str(v).upper().strip()
                    out.extend([vs, f"SEC {vs}", f"Section {vs}"])
                return {"$in": [str(x) for x in list(dict.fromkeys(out))]}

            return {"$eq": str(value)}

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
                    step_num = int(step_num_str)
                    self.debug(f"   -> Resolving placeholder: looking for '{key_to_find}' in results of step {step_num}")
                    if step_num in step_results and step_results[step_num]:
                        step_result = step_results[step_num]
                        if isinstance(step_result, dict):
                            if key_to_find in step_result:
                                return step_result[key_to_find]
                        elif isinstance(step_result, list) and len(step_result) > 0:
                            metadata = step_result[0].get("metadata", {})
                            if key_to_find in metadata:
                                return metadata[key_to_find]
                            if key_to_find in metadata:
                                return normalize_for_search(key_to_find, metadata[key_to_find])
                            for original_key in forward_map.get(key_to_find, []):
                                if original_key in metadata:
                                    self.debug(f"   -> Found value using original key '{original_key}' for standard key '{key_to_find}'")
                                    return normalize_for_search(key_to_find, metadata[original_key])
            return obj
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
            
            print(f"🔍 Looking for person: '{target_person_upper}' in document")
            
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
                    
                print(f"🔍 Searching for: '{search_name}'")
                
                # Very high boost for exact matches in faculty metadata
                if metadata.get('full_name') and search_name in metadata['full_name'].upper():
                    score += 80
                    found_match = True
                    print(f"🎯 Found in full_name metadata: +80")
                elif metadata.get('surname') and search_name in metadata['surname'].upper():
                    score += 75
                    found_match = True
                    print(f"🎯 Found in surname metadata: +75")
                elif metadata.get('first_name') and search_name in metadata['first_name'].upper():
                    score += 75
                    found_match = True
                    print(f"🎯 Found in first_name metadata: +75")
                
                # ENHANCED: Check adviser field specifically for COR schedules
                if metadata.get('adviser') and search_name in metadata['adviser'].upper():
                    score += 90  # Higher score for adviser matches
                    found_match = True
                    print(f"🎯 Found in adviser metadata: +90")
                
                # High boost for names in document content
                if search_name in doc_upper:
                    score += 60
                    found_match = True
                    print(f"🎯 Found in document content: +60")
                
                # Check for faculty-specific context
                if any(term in doc_upper for term in ['FACULTY', 'PROFESSOR', 'INSTRUCTOR', 'TEACHER', 'ADVISER', 'ADVISOR']):
                    if search_name in doc_upper:
                        score += 70
                        found_match = True
                        print(f"🎯 Found in faculty context: +70")
                
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
                            print(f"🎯 Partial match '{part}' in document: +35")
                        
                        # Check metadata fields more thoroughly
                        for field in ['full_name', 'surname', 'first_name', 'adviser']:
                            if metadata.get(field) and part in metadata[field].upper():
                                partial_matches += 1
                                score += 40
                                found_match = True
                                print(f"🎯 Partial match '{part}' in {field}: +40")
                                break
                
                # Boost score if multiple name parts match
                if partial_matches > 1:
                    score += 25
                    found_match = True
                    print(f"🎯 Multiple name parts matched: +25")
                
                # If we found a match with this search term, we can break
                if found_match:
                    break
            
            # Special boost for single name searches in faculty context
            if len(target_person_upper.split()) == 1 and any(term in doc_upper for term in ['FACULTY', 'PROFESSOR', 'TEACHING', 'ADVISER']):
                score += 30
                print(f"🎯 Single name in faculty context: +30")

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
        
        print(f"🔍 Filtering {len(results)} results with min_relevance: {min_relevance}")
        
        # Remove results that don't meet minimum relevance
        filtered_results = []
        for r in results:
            print(f"🔍 Result relevance: {r['relevance']} (min: {min_relevance})")
            if r['relevance'] >= min_relevance:
                filtered_results.append(r)
            else:
                print(f"🔍 Filtered out result with relevance {r['relevance']}")
        
        print(f"🔍 After filtering: {len(filtered_results)} results remain")
        
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
        print(f"🔍 Final results count: {len(final_results)}")
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
    
    def search_database(self, query_text: Optional[str] = None, query: Optional[str] = None,
                    filters: Optional[dict] = None, document_filter: Optional[dict] = None,
                    collection_filter: Optional[str] = None, n_results: int = 200) -> List[dict]: # Add n_results=50 here
        """
        The core database search function. It can handle semantic queries, metadata filters,
        and document content filters, with robust normalization for filter values.
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

                    elif standard_key == "year_level":
                        or_conditions_for_year = []
                        year_str = str(v)
                        year_variations_str = {year_str, f"Year {year_str}"}
                        for key in possible_keys:
                            or_conditions_for_year.append({key: {"$in": list(year_variations_str)}})
                            try:
                                year_int = int(v)
                                or_conditions_for_year.append({key: {"$eq": year_int}})
                            except (ValueError, TypeError):
                                pass
                        filter_for_this_key = {"$or": or_conditions_for_year} if len(or_conditions_for_year) > 1 else or_conditions_for_year[0]

                    else: # Generic logic for all other filters
                        query_value = v
                        if isinstance(v, str):
                            query_value = {"$in": list(set([v.lower(), v.upper(), v.title()]))}

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
            if collection_filter and isinstance(collection_filter, str) and collection_filter not in name:
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



    # Add this new function anywhere inside your AIAnalyst class

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
        
    def execute_reasoning_plan(self, query: str, session: dict) -> tuple[str, Optional[dict], List[dict]]:
        """
        [MODIFIED FOR SESSIONS & SUMMARY] The main orchestration method.
        """
        self.debug("Starting reasoning plan execution...")
        start_time = time.time()

        self.current_query_entities = []


        # --- NEW BLOCK 1: Reset and perform pronoun resolution ---
        self.current_query_entities = [] # Reset for this query
        
        pronouns = {'his', 'her', 'their', 'him', 'he', 'she'}
        query_words = set(query.lower().split())
        
        if not pronouns.isdisjoint(query_words):
            mentioned_entities = session.get("mentioned_entities", [])
            if mentioned_entities:
                last_entity = mentioned_entities[-1]
                self.debug(f"Pronoun detected. Replacing with last known entity: '{last_entity}'")
                
                # Simple replacement logic
                for pronoun in pronouns:
                    # Handle possessives like "his" -> "Michael Carpenter's"
                    if pronoun.endswith('s'):
                         query = re.sub(r'\b' + pronoun + r'\b', f"{last_entity}'s", query, flags=re.IGNORECASE)
                    else:
                         query = re.sub(r'\b' + pronoun + r'\b', last_entity, query, flags=re.IGNORECASE)
                self.debug(f"Modified query: '{query}'")
        # --- END NEW BLOCK 1 ---
        # --- NEW: Extract context from the full session object ---
        chat_history = session.get("chat_history", [])
        summary = session.get("conversation_summary", "No summary yet.")
        # --- END NEW ---

        plan_json = None
        final_context = {}
        error_msg = None
        results_count = 0
        
        outcome = "FAIL_UNKNOWN"
        execution_mode = "primary"
        collected_docs = []
        
        try:
            max_retries = 5
            tool_call_json = None
            
            for attempt in range(max_retries):
                self.debug(f"Planner Attempt {attempt + 1}/{max_retries}...")
            
                sys_prompt = PROMPT_TEMPLATES["planner_agent"].format(
                    all_programs_list=self.all_programs,
                    all_departments_list=self.all_departments,
                    all_positions_list=self.all_positions,
                    all_doc_types_list=self.all_doc_types,
                    all_statuses_list=self.all_statuses,
                    dynamic_examples=self.dynamic_examples
                )
                
                # --- NEW: Construct a richer user prompt with the summary ---
                planner_user_prompt = (
                    f"CONVERSATION SUMMARY (What we are currently talking about):\n{summary}\n\n"
                    f"---\n"
                    f"USER'S CURRENT QUERY (Your task):\n{query}"
                )
                # --- END NEW ---

                plan_raw = self.planner_llm.execute(
                    system_prompt=sys_prompt,
                    user_prompt=planner_user_prompt, # Use the new prompt
                    json_mode=True, phase="planner",
                    history=chat_history # Still pass short-term history
                )
        
                tool_call_json = self._repair_json(plan_raw)
                if tool_call_json and "tool_name" in tool_call_json:
                    self.debug(f"Valid tool selected on attempt {attempt + 1}.")
                    plan_json = {"plan": [{"tool_call": tool_call_json}]}
                    break
                else:
                    self.debug(f"Attempt {attempt + 1} failed to select a valid tool. Retrying...")
                    time.sleep(1)
            
            if not tool_call_json:
                outcome = "FAIL_PLANNER"
                raise ValueError(f"AI failed to select a valid tool after {max_retries} attempts.")

            # 2. Execute the validated tool call
            tool_name = tool_call_json["tool_name"]
            params = tool_call_json.get("parameters", {})

            # --- NEW: DEDICATED PATH FOR CONVERSATIONAL QUERIES ---
            if tool_name == "answer_conversational_query":
                self.debug("-> Handling conversational query with a dedicated synth call.")
                final_answer = self.synth_llm.execute(
                    system_prompt="You are a friendly and helpful AI assistant for PDM. Respond naturally and conversationally to the user.",
                    user_prompt=query,
                    history=chat_history or [],
                    phase="synth"
                )
                execution_time = time.time() - start_time
                self.training_system.record_query_result(query=query, plan=plan_json, outcome="SUCCESS_CONVERSATIONAL", execution_time=execution_time, final_answer=final_answer, results_count=0)
                return final_answer, plan_json, []
            # --- END OF NEW PATH ---
            
            collected_docs = []
            
            if tool_name in self.available_tools:
                tool_function = self.available_tools[tool_name]

                # Filter out unexpected parameters to prevent errors
                import inspect
                sig = inspect.signature(tool_function)
                valid_params = {k: v for k, v in params.items() if k in sig.parameters}
                dropped = [k for k in params if k not in sig.parameters]
                if dropped:
                    self.debug(f"Dropping unexpected parameters for {tool_name}: {dropped}")

                self.debug(f"   -> Executing primary tool: {tool_name} with params: {valid_params}")
                results = tool_function(**valid_params)
                collected_docs = results if isinstance(results, list) else [results]
            else:
                raise ValueError(f"AI selected an unknown tool: '{tool_name}'")


            # 3. Fallback Logic: If the primary tool fails, try a broad semantic search
            primary_tool_failed = not collected_docs or "error" in collected_docs[0].get("status", "") or "empty" in collected_docs[0].get("status", "")

            if primary_tool_failed:
                execution_mode = "fallback" # Update execution mode
                self.debug(f"Primary tool '{tool_name}' failed or found nothing. Attempting fallback semantic search.")
                fallback_docs = self._execute_smart_fallback_search(query)
                if fallback_docs:
                    self.debug(f"Fallback search found {len(fallback_docs)} documents.")
                    summary_doc = {
                        "source_collection": "system_note",
                        "content": f"Note: The initial targeted search for tool '{tool_name}' failed. The following are broader, semantically related results for your query.",
                        "metadata": {}
                    }
                    collected_docs = [summary_doc] + fallback_docs
                    outcome = "SUCCESS_FALLBACK" # Update outcome
                else:
                    self.debug("Fallback search also found nothing.")
                    outcome = "FAIL_EMPTY" # Update outcome
            else:
                outcome = "SUCCESS_DIRECT" # Primary tool succeeded


                # --- ✨ TEMP FIX: De-duplicate results before sending to Synthesizer ---
            if collected_docs:
                self.debug(f"Original unfiltered doc count: {len(collected_docs)}. Starting de-duplication...")
                unique_docs = {}
                for doc in collected_docs:
                    # Use the document's 'content' as a unique key to filter out duplicates.
                    content_key = doc.get('content')
                    if content_key and content_key not in unique_docs:
                        unique_docs[content_key] = doc
                
                deduplicated_list = list(unique_docs.values())
                self.debug(f"Found {len(deduplicated_list)} unique documents after de-duplication.")
                # Replace the original list with the clean, de-duplicated one.
                collected_docs = deduplicated_list
            # --- ✨ END TEMP FIX ---



            # In AI.py, inside the execute_reasoning_plan method:

            # --- FINAL: COMPLETE & DETAILED GROUPING LOGIC ---
            # Group if we have more than a few results to make the context cleaner for the AI.
            if len(collected_docs) > 5:
                first_doc_meta = collected_docs[0].get("metadata", {})

                # Step 1: Identify if the data is about students by checking for its unique fields.
                is_student_data = "student_id" in first_doc_meta and "guardian_name" in first_doc_meta

                if is_student_data:
                    self.debug(f"-> Student result set ({len(collected_docs)} docs) detected. Grouping with ALL details.")
                    
                    from collections import defaultdict
                    grouped_students = defaultdict(list)
                    
                    # Step 2: Group the full metadata objects for each student.
                    for doc in collected_docs:
                        meta = doc.get("metadata", {})
                        course = meta.get("course", "N/A")
                        year = meta.get("year", "N/A")
                        section = meta.get("section", "N/A")
                        group_key = f"{course} - Year {year} - Section {section}"
                        grouped_students[group_key].append(meta)
                    
                    # Step 3: Build a rich, markdown-formatted summary with ALL relevant details.
                    summary_content = f"Found a total of {len(collected_docs)} students, organized as follows:\n\n"
                    
                    for group, metas in sorted(grouped_students.items()):
                        summary_content += f"## {group} ({len(metas)} students)\n\n"
                        # Create a complete profile card for each student in the group.
                        for i, meta in enumerate(sorted(metas, key=lambda x: x.get('full_name', ''))):
                            summary_content += f"**{i+1}. {meta.get('full_name', 'N/A')}**\n"
                            summary_content += f"- **Student ID:** {meta.get('student_id', 'N/A')}\n"
                            summary_content += f"- **Department:** {meta.get('department', 'N/A')}\n"
                            summary_content += f"- **Contact:** {meta.get('contact_number', 'N/A')}\n"
                            summary_content += f"- **Guardian:** {meta.get('guardian_name', 'N/A')}\n"
                            summary_content += f"- **Guardian Contact:** {meta.get('guardian_contact', 'N/A')}\n\n"

                    # Step 4: Replace the long list of documents with our single, comprehensive summary.
                    collected_docs = [{
                        "source_collection": "system_summary",
                        "content": summary_content,
                        "metadata": {"status": "success", "total_found": len(collected_docs)}
                    }]
            # --- END OF FINAL GROUPING LOGIC ---


                # --- ✨ START: DEBUG CODE TO SHOW RETRIEVED DOCS ---
            self.debug("\n" + "="*50)
            self.debug(f"📑 Final {len(collected_docs)} documents being sent to Synthesizer:")
            # Pretty-print the JSON to the console
            debug_output = json.dumps(collected_docs, indent=2)
            print(debug_output)
            self.debug("="*50 + "\n")
            # --- ✨ END: DEBUG CODE ---

            # 4. Build the final context for the synthesizer
            if outcome in ["SUCCESS_DIRECT", "SUCCESS_FALLBACK"]:
                results_count = len(collected_docs)
                final_context = {
                    "status": "success",
                    "summary": f"Found {results_count} relevant document(s).",
                    "data": collected_docs[:30] # Limit context size
                }
            else:
                final_context = {"status": "empty", "summary": "I tried a precise search and a broad search, but could not find any relevant documents."}


        # In the execute_reasoning_plan function...

        except Exception as e:
            # ⬇️ REPLACE THE EXISTING DEBUG LINE WITH THESE THREE LINES ⬇️
            import traceback
            self.debug(f"An unexpected error occurred: {e}")
            self.debug(f"Error Type: {type(e)}")
            self.debug(f"Traceback: {traceback.format_exc()}")
            # ⬆️ END OF CHANGE ⬆️

            error_msg = str(e)
            # If the outcome hasn't been set by the planner failure, it's an execution failure
            if outcome == "FAIL_UNKNOWN":
                outcome = "FAIL_EXECUTION"
            final_context = {"status": "error", "summary": f"I ran into a technical problem: {e}"}

        # 5. Synthesize the final answer
        self.debug("Synthesizing final answer...")
        context_for_llm = json.dumps(final_context, indent=2, ensure_ascii=False)
        synth_prompt = PROMPT_TEMPLATES["final_synthesizer"].format(context=context_for_llm, query=query)
        final_answer = self.synth_llm.execute(
            system_prompt="You are a careful AI analyst who provides conversational answers based only on the provided facts.",
            user_prompt=synth_prompt, 
            history=chat_history or [],
            phase="synth"
        )

        corruption_details = sorted(list(self.corruption_warnings)) if self.corruption_warnings else None

        # Record the results for training using the fully corrected signature
        execution_time = time.time() - start_time
        self.training_system.record_query_result(
            query=query,
            plan=plan_json,
            results_count=results_count,
            execution_time=execution_time,
            error_msg=error_msg,
            execution_mode=execution_mode,
            outcome=outcome,
            analyst_mode=self.execution_mode,
            final_answer=final_answer,
            corruption_details=corruption_details
        )

        # --- NEW BLOCK 2: Save newly found entities to the session ---
        if self.current_query_entities:
            for entity_name in self.current_query_entities:
                self._add_entity_to_session(session['session_id'], entity_name)
        # --- END NEW BLOCK 2 ---


        
        return final_answer, plan_json, collected_docs
    
    # -------------------------------
# Function use for Web
# -------------------------------
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
            "structured_data": synced_structured_data
        }

        return final_response
        

    def _create_image_map(self, structured_data: list[dict]) -> dict:
        """
        Processes the AI's structured_data to create a JSON map of image URLs.
        """
        by_id = {}
        by_name_temp = defaultdict(list)

        for student_doc in structured_data:
            meta = student_doc.get("metadata", {})
            image_url = meta.get("image_url")
            student_id = meta.get("student_id")
            full_name = meta.get("full_name")

            if not image_url or not student_id or not full_name:
                continue

            by_id[student_id] = image_url

            name_parts = [part.strip() for part in full_name.replace(",", "").lower().split()]
            normalized_full_name = " ".join(reversed(name_parts))
            last_name = name_parts[0]

            by_name_temp[normalized_full_name].append(image_url)
            by_name_temp[last_name].append(image_url)

        by_name_final = {}
        for name, urls in by_name_temp.items():
            by_name_final[name] = urls[0] if len(urls) == 1 else list(set(urls))

        return {"by_id": by_id, "by_name": by_name_final}

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

            if q.lower() == "train":
                # ... (this part is fine)
                continue

            # --- FIX 1: Pass the entire 'session' object, not just its history ---
            final_answer, plan_json, collected_docs = self.execute_reasoning_plan(q, session=session)

            # Update the session history in memory and MongoDB
            self._update_session_history(terminal_session_id, q, final_answer)

            # --- FIX 2: Add the call to the summarizer ---
            self._summarize_conversation(terminal_session_id)

            print("\nAnalyst:", final_answer)

            # The rest of your file-saving logic is correct.
            image_map = self._create_image_map(collected_docs)
            output_for_file = {
                "ai_response": final_answer,
                "structured_data": collected_docs,
                "image_map": image_map
            }
            output_filename = "latest_response_data.json"
            with open(output_filename, "w", encoding="utf-8") as f:
                json.dump(output_for_file, f, indent=2, default=str)
            print(f"✅ Detailed data and image map saved to '{output_filename}'")

            if plan_json and "plan" in plan_json:
                last_query = q
                last_plan_for_training = plan_json
