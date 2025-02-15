import json
import mss
import PIL.Image
import logging
import base64
import io
import numpy as np
import time
from websockets.asyncio.client import connect
from .config import URI
from .screen_parser import ScreenParser

logger = logging.getLogger('APIClient')

class APIClient:
    def __init__(self):
        self.ws = None
        self.last_screenshot = None
        self.screen_parser = ScreenParser()
        self.last_action = None
        self.action_history = []
        self.context = {
            "task_stack": [],
            "session_state": {},
            "validation_queue": []
        }

    def take_screenshot(self) -> dict:
        """Take a screenshot and analyze it using the enhanced screen parser."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            screenshot = sct.grab(monitor)
            img = PIL.Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            
            # Store screenshot and analyze it
            self.last_screenshot = img
            screen_analysis = self.screen_parser.analyze_screen(img)
            
            # Convert to base64
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=70)
            img_byte_arr = img_byte_arr.getvalue()
            buffer = base64.b64encode(img_byte_arr)
            
            # Combine image data with analysis
            return {
                "mime_type": "image/jpeg",
                "data": buffer.decode(),
                "screen_info": screen_analysis["screen_info"],
                "elements": screen_analysis["elements"],
                "semantic_structure": screen_analysis["semantic_structure"],
                "timestamp": time.time()
            }

    def record_action(self, action: dict):
        """Record an action and its context in the history."""
        self.last_action = action
        
        # Take screenshot before recording action
        screenshot_data = self.take_screenshot()
        
        # Create rich action record
        action_record = {
            "action": action["action"],
            "reasoning": action.get("reasoning", "No reasoning provided"),
            "timestamp": time.time(),
            "screen_state": {
                "elements": len(screenshot_data["elements"]),
                "semantic_structure": screenshot_data["semantic_structure"]
            },
            "context": {
                "current_task": self.context["task_stack"][-1] if self.context["task_stack"] else None,
                "session_state": self.context["session_state"]
            }
        }
        
        self.action_history.append(action_record)

    async def setup(self):
        """Perform initial API setup with enhanced system prompt."""
        try:
            system_prompt = """You are an advanced computer control assistant that helps users accomplish tasks by:

1. UNDERSTAND - Analyze the current screen state using computer vision and semantic understanding
2. PLAN - Break down complex tasks into manageable subgoals
3. ACT - Execute precise actions with clear reasoning
4. VALIDATE - Verify each step's success before proceeding

For each task:
- Maintain awareness of screen state and context
- Use semantic understanding to identify UI elements
- Execute actions with proper timing and validation
- Adapt to dynamic screen changes
- Handle errors and unexpected states gracefully

Available actions:
- type: Type text or launch applications
- click: Click UI elements by semantic ID
- scroll: Navigate content vertically
- wait: Allow for loading and transitions
- navigate: Go to URLs or between apps
- hover: Move mouse over elements
- complex_task: Execute multi-step sequences

Each action requires:
- Clear reasoning about the current context
- Validation criteria for success
- Consideration of screen state
- Proper error handling

Handle special cases:
- Login requirements: Stop and notify user
- Loading states: Use wait action
- Missing elements: Scroll or retry
- Complex workflows: Break into steps
"""

            function_declaration = {
                "name": "execute_computer_action",
                "description": """Execute actions to control the computer with semantic understanding and validation:
                    - type: Type text or launch applications
                    - click: Click UI elements by semantic ID
                    - scroll: Navigate content
                    - wait: Handle loading/transitions
                    - navigate: Browse to URLs
                    - hover: Mouse movement
                    - complex_task: Multi-step sequences""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["type", "click", "scroll_up", "scroll_down", "wait", "navigate", "hover", "complex_task"],
                            "description": "Type of action to perform"
                        },
                        "element_id": {
                            "type": "integer",
                            "description": "Semantic ID of UI element to interact with"
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type or URL to navigate to"
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Detailed explanation of action in current context"
                        },
                        "validation": {
                            "type": "string",
                            "description": "Criteria to validate action success"
                        }
                    },
                    "required": ["action", "reasoning"]
                }
            }

            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.0-flash-exp",
                    "tools": [{"function_declarations": [function_declaration]}],
                    "system": system_prompt,
                    "generation_config": {
                        "temperature": 0.7,
                        "response_modalities": ["TEXT"]
                    }
                }
            }

            logger.debug(f"Setup message: {json.dumps(setup_msg, indent=2)}")
            await self.ws.send(json.dumps(setup_msg))

            response = await self.ws.recv()
            response_data = json.loads(response)
            logger.debug(f"Setup response: {response}")
            
            if "error" in response_data:
                logger.error(f"Setup failed: {response_data['error']}")
                return False
                
            if "setupComplete" in response_data:
                logger.info("Setup completed successfully")
                return True

            logger.error(f"Invalid setup response: {response_data}")
            return False

        except Exception as e:
            logger.error(f"Setup failed: {str(e)}")
            return False

    async def push_task(self, task: str):
        """Push a new task onto the task stack."""
        self.context["task_stack"].append(task)
        
    async def pop_task(self):
        """Pop completed task from the stack."""
        if self.context["task_stack"]:
            return self.context["task_stack"].pop()
        return None

    def update_session_state(self, updates: dict):
        """Update session state with new information."""
        self.context["session_state"].update(updates)

    async def send_text(self, text: str):
        """Send text input with enhanced context."""
        logger.info(f"Sending text: {text}")
        
        # Take screenshot and analyze
        screenshot_data = self.take_screenshot()
        
        # Create message with rich context
        msg = {
            "client_content": {
                "turns": [{
                    "role": "user",
                    "parts": [
                        {"text": text},
                        {
                            "context": {
                                "screen_info": screenshot_data["screen_info"],
                                "semantic_structure": screenshot_data["semantic_structure"],
                                "current_task": self.context["task_stack"][-1] if self.context["task_stack"] else None,
                                "action_history": self.action_history[-5:],
                                "session_state": self.context["session_state"]
                            }
                        }
                    ]
                }],
                "turn_complete": True
            }
        }
        await self.ws.send(json.dumps(msg))

    async def send_screenshot(self):
        """Send screenshot and analysis."""
        screenshot_data = self.take_screenshot()
        msg = {
            "realtime_input": {
                "media_chunks": [{
                    "type": "screenshot",
                    "data": screenshot_data
                }]
            }
        }
        await self.ws.send(json.dumps(msg))

    async def connect(self):
        """Establish WebSocket connection."""
        try:
            self.ws = await connect(URI)
            return True
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            return False