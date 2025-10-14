# backend/utils/ai_core/llm_service.py

"""
This module contains the LLMService class, a client for interacting with
Large Language Model APIs (e.g., Mistral, Ollama).
"""

import json
import time
import requests
from typing import List, Optional

class LLMService:
    """
    A client for interacting with Large Language Model APIs (e.g., Mistral, Ollama).
    It handles request preparation, execution, and retries.
    """
    def __init__(self, config: dict):
        """
        Initializes the LLM service with configuration settings.

        Args:
            config: A dictionary containing API keys, URLs, and model names.
        """
        self.api_mode = config.get('api_mode', 'online')
        self.debug_mode = config.get('debug_mode', False)
        self.mistral_api_key = config.get('mistral_api_key')
        self.mistral_api_url = config.get('mistral_api_url', 'https://api.mistral.ai/v1/chat/completions')
        self.ollama_api_url = config.get('ollama_api_url', 'http://localhost:11434/api/chat')
        self.planner_model = config.get('planner_model')
        self.synth_model   = config.get('synth_model')

    def _prepare_request(self, messages: list, json_mode: bool, phase: str = "planner"):
        """
        Constructs the appropriate API request (URL, headers, payload) based on the
        configured API mode (online/offline) and whether JSON output is required.
        """
        headers, payload, api_url = {}, {}, ""
        model_override = self.planner_model if phase == "planner" else self.synth_model

        if self.api_mode == 'online':
            api_url = self.mistral_api_url
            headers = {"Authorization": f"Bearer {self.mistral_api_key}", "Content-Type": "application/json"}
            payload = {"model": model_override or "mistral-small-latest", "messages": messages}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
        else: # Handles 'offline' mode
            api_url = self.ollama_api_url
            headers = {"Content-Type": "application/json"}
            payload = {"model": model_override or "mistral:instruct", "messages": messages, "stream": False}
            if json_mode:
                payload["format"] = "json"
                # Add a forceful instruction for Ollama to ensure JSON output
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += (
                        "\n\nIMPORTANT: Your response MUST be a single, valid JSON object and nothing else. "
                        "Do not include any text, explanations, or markdown formatting before or after the JSON."
                    )
        return api_url, headers, payload

    def execute(self, *, system_prompt: str, user_prompt: str, json_mode: bool = False,
                history: Optional[List[dict]] = None, retries: int = 2, phase: str = "planner") -> str:
        """
        Executes a request to the configured LLM API with retry logic.

        Args:
            system_prompt: The system-level instructions for the AI.
            user_prompt: The user's query or request.
            json_mode: If True, requests a JSON object as the response.
            history: A list of previous conversation turns.
            retries: The number of times to retry the request on failure.
            phase: The current phase ('planner' or 'synth') to select the correct model.

        Returns:
            The content of the LLM's response as a string.
        """
        # Assemble messages in the correct order: system, history, then user.
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})

        api_url, headers, payload = self._prepare_request(messages, json_mode, phase=phase)
        if not api_url:
            return "Configuration Error: API URL is not set."

        if self.debug_mode:
            print(f"LLMService -> {self.api_mode.upper()} | phase={phase} | json={json_mode}")

        last_err = None
        for attempt in range(retries + 1):
            try:
                payload["messages"] = messages 
                resp = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=360)
                resp.raise_for_status()
                rj = resp.json()
                if 'choices' in rj and rj['choices']:
                    return rj['choices'][0]['message']['content'].strip()
                if 'message' in rj and 'content' in rj['message']:
                    return rj['message']['content'].strip()
                raise ValueError("No content in LLM response")
            except Exception as e:
                last_err = e
                if self.debug_mode:
                    print(f"LLM attempt {attempt+1}/{retries+1} failed: {e}")
                if attempt < retries:
                    time.sleep(1)
                    
        return f"Error: Could not connect to the AI service. Details: {last_err}"