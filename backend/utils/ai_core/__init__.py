# backend/utils/ai_core/__init__.py

"""
This file makes the 'ai_core' directory a Python package.

It also provides a convenient way to import all the major components
of the AI system from a single place.
"""

from .database import MongoCollectionAdapter
from .llm_service import LLMService
from .prompts import PROMPT_TEMPLATES
from .training import TrainingSystem
from .analyst import AIAnalyst

# This __all__ list defines the public API of the package.
# When a user does 'from ai_core import *', only these names will be imported.
__all__ = [
    "MongoCollectionAdapter",
    "LLMService",
    "PROMPT_TEMPLATES",
    "TrainingSystem",
    "AIAnalyst",
]