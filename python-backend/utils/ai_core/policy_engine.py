# backend/utils/ai_core/policy_engine.py

import re
import copy
import spacy # <-- Import spacy
from typing import List, Dict, Any, Optional


class PolicyEngine:
    """
    [HYBRID VERSION] Uses a combination of regex for simple, domain-specific
    entities and a SpaCy NLP model for robust person name recognition.
    """
    def __init__(self, known_programs: List[str]):
        self.known_programs = {p.lower() for p in known_programs}
        
        # Load the small English SpaCy model.
        # This happens once when the AIAnalyst starts.
# Load the small English SpaCy model.
        # This happens once when the AIAnalyst starts.
        try:
            self.nlp = spacy.load("en_core_web_sm")
            # --- THIS IS THE NEW LINE ---
            self.stopwords = self.nlp.Defaults.stop_words
            print(" SpaCy NLP model ('en_core_web_sm') and stopwords loaded successfully for PolicyEngine.")
            self.debug = print
        except OSError:
            print(" SpaCy model not found. Please run: python -m spacy download en_core_web_sm")
            self.nlp = None
            # --- THIS IS THE NEW LINE (FALLBACK) ---
            self.stopwords = set() # Use an empty set as a fallback

    # In policy_engine.py

    # In backend/utils/ai_core/policy_engine.py

# --- REPLACE THE ENTIRE delexicalize METHOD WITH THIS ---

    def is_data_like(self, query: str) -> bool:
            """
            [NEW] Uses SpaCy's POS tagger to determine if a vague query
            contains "data-like" tokens (nouns, numbers, proper nouns)
            vs. gibberish or non-data tokens.
            """
            if not self.nlp:
                return False # Safety check

            doc = self.nlp(query.lower())
            if not doc:
                return False

            # Tags that indicate the user is providing data/entities
            # PROPN: Proper Noun (e.g., "BSCS", "Mark")
            # NOUN: Noun (e.g., "year", "schedule")
            # NUM: Number (e.g., "2", "4")
            # ADJ: Adjective (often used for ordinals like "2nd", "4th")
            data_like_tags = {'PROPN', 'NOUN', 'NUM', 'ADJ'}

            for token in doc:
                if token.pos_ in data_like_tags:
                    self.debug(f"NLP Data-Like Check: Found data token '{token.text}' ({token.pos_}). Flagging as answer-like.")
                    return True
            
            self.debug(f"NLP Data-Like Check: No data-like tokens found. Flagging as ambiguous.")
            return False


    def is_interrogative(self, query: str) -> bool:
        """
        [NEW] Uses SpaCy's Part-of-Speech (POS) tagger to determine
        if a query is a question.
        
        It checks if the first token is a "wh-word" (like what, who,
        where, when, which).
        """
        if not self.nlp:
            return False # Safety check

        doc = self.nlp(query.lower())
        if not doc:
            return False

        first_token = doc[0]
        
        # Check for SpaCy's "wh-word" tags:
        # WDT: Wh-determiner (what, which)
        # WP:  Wh-pronoun (who, whom, what)
        # WP$: Possessive wh-pronoun (whose)
        # WRB: Wh-adverb (where, when, how, why)
        if first_token.tag_ in {'WDT', 'WP', 'WP$', 'WRB'}:
            self.debug(f"NLP Interrogative Check: Query starts with '{first_token.text}' ({first_token.tag_}). Flagging as question.")
            return True
            
        # Add a check for auxiliary-led questions (e.g., "Is this...")
        if first_token.pos_ == 'AUX':
            self.debug(f"NLP Interrogative Check: Query starts with auxiliary '{first_token.text}'. Flagging as question.")
            return True

        self.debug(f"NLP Interrogative Check: Query does not appear to be a question.")
        return False

    def delexicalize(self, query: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        [UPGRADED] Dynamically delexicalizes a query based on the
        parameters in the provided plan.
        """
        user_pattern = query
        plan_template = copy.deepcopy(plan)
        params = plan_template.get("parameters", {})

        if not params:
            return {
                "user_pattern": user_pattern,
                "plan_template": plan_template
            }

        # --- DYNAMIC REPLACEMENT LOGIC ---
        # Create a list of (value, placeholder) tuples, sorted by length
        # so we replace "BS Computer Science" before "BSCS"
        replacements = []
        for key, value in params.items():
            if not isinstance(value, str) and not isinstance(value, int):
                continue # Skip lists, dicts, etc.
            
            val_str = str(value).strip()
            if not val_str: # Skip empty strings
                continue

            # 1. Create the placeholder (e.g., {PROGRAM}, {PERSON_NAME})
            placeholder = f"{{{key.upper()}}}" 
            
            # 2. Add the value and placeholder to our list
            replacements.append((val_str, placeholder))
            
            # 3. Also replace the value in the plan_template
            params[key] = placeholder

        # Sort by length of the value (longest first) to avoid partial-word bugs
        replacements.sort(key=lambda x: len(x[0]), reverse=True)

        # 4. Loop through and replace all occurrences in the user_pattern
        for value, placeholder in replacements:
            # Use regex for whole-word, case-insensitive replacement
            try:
                pattern = re.compile(r'\b' + re.escape(value) + r'\b', re.IGNORECASE)
                user_pattern = pattern.sub(placeholder, user_pattern)
            except re.error:
                # Fallback for complex strings that break regex
                user_pattern = user_pattern.replace(value, placeholder)
        
        # --- END DYNAMIC LOGIC ---

        return {
            "user_pattern": user_pattern,
            "plan_template": plan_template
        }
    


    # In backend/utils/ai_core/policy_engine.py
    # --- REPLACE THE ENTIRE 'is_query_vague_nlp' METHOD WITH THIS ---

    def is_query_vague_nlp(self, query: str) -> bool:
        """
        [UPGRADED] Uses SpaCy's dependency parser to check for "dangling nouns."
        
        This version now checks the token's entire 'subtree' (children,
        grandchildren, etc.) to find a saving modifier, making it robust
        against phrases like "adviser *for BSCS*".
        """
        if not self.nlp:
            return False # Safety check if SpaCy isn't loaded

        doc = self.nlp(query)

        # Nouns that MUST have a target to be unambiguous
        TARGET_REQUIRED_NOUNS = {
            "adviser",
            "schedule",
            "grade",
            "dean",
            "head",
            "president"
        }
        
        # Modifiers that "save" a noun from being vague
        # pobj: "adviser *of BSCS*" (prepositional object)
        # dobj: "get *the schedule*" (direct object)
        # compound: "*BSCS* schedule" (compound noun)
        # nsubj: "*Maria's* schedule" (nominal subject)
        # poss: "Maria's schedule" (possessive)
        # pcomp: (prepositional complement)
        # appos: (appositional modifier, e.g., "Mr. Smith, the adviser")
        SAVING_MODIFIERS = {"pobj", "dobj", "compound", "nsubj", "poss", "pcomp", "appos"}

        for token in doc:
            if token.lemma_ in TARGET_REQUIRED_NOUNS:
                # The noun is in our list. Now, check if it has any "saving" modifiers
                # anywhere in its subtree (children, grandchildren, etc.)
                
                has_modifier = False
                
                # --- THIS IS THE FIX ---
                # We check `token.subtree` instead of just `token.children`
                for descendant in token.subtree:
                    if descendant.dep_ in SAVING_MODIFIERS:
                        # Ensure the modifier is not the token itself
                        if descendant.i != token.i:
                            has_modifier = True
                            self.debug(f"NLP Vague Check: Noun '{token.text}' is saved by descendant '{descendant.text}' ({descendant.dep_}).")
                            break
                # --- END OF FIX ---
                
                # This is a dangling, ambiguous noun.
                if not has_modifier:
                    self.debug(f"NLP Vague Check: Found dangling noun '{token.text}'. Flagging as ambiguous.")
                    return True
        
        # No dangling nouns found
        self.debug("NLP Vague Check: No dangling nouns found. Query is not vague.")
        return False


    def get_intent(self, query: str) -> Optional[str]:
        """
        A fast, local, rule-based intent classifier.
        This is used by the ExampleManager for its Tier 1 retrieval.
        """
        q_lower = query.lower().strip()

        # --- Tier 1: Direct, high-confidence matches ---

        # get_person_profile
        if re.search(r'^(who is|what is|profile of|tell me about)\b', q_lower):
            return "get_person_profile"

        # get_person_schedule
        if re.search(r'\b(schedule|class|when is|classes)\b', q_lower):
            return "get_person_schedule"
        
        # get_student_grades
        if re.search(r'\b(grades|gwa|scores|class standing)\b', q_lower):
            return "get_student_grades"

        # query_curriculum
        if re.search(r'\b(curriculum|subjects|what subjects)\b', q_lower):
            return "query_curriculum"

        # find_people (group)
        if re.search(r'\b(list all|how many|find all|show all)\b', q_lower):
            if re.search(r'\b(students|faculty|teachers)\b', q_lower):
                return "find_people"

        # answer_conversational_query
        if q_lower in ['hi', 'hello', 'hey', 'thanks', 'thank you', 'ok', 'okay']:
            return "answer_conversational_query"

        # --- Tier 2: Fallback (if no high-confidence match) ---
        # If it *looks* like a person query, guess get_person_profile
        if len(q_lower.split()) <= 4:
            # This is a weak heuristic, but it's our best local guess
            return "get_person_profile" 

        # If we have no idea, return None and let the main LLM planner decide
        return None

    