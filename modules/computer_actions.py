import json
import pyautogui
import logging
import asyncio
from typing import Optional, Dict, Any, List
from .api_client import APIClient

logger = logging.getLogger('ComputerActions')

class ActionPlanner:
    """Plans and executes multi-step actions with validation."""
    
    def __init__(self):
        self.current_plan = []
        self.current_step = 0
        self.last_result = None
        self.context = {}

    def create_plan(self, task: str, screen_info: Dict) -> List[Dict]:
        """Create a sequence of actions to accomplish a task."""
        plan = []
        
        if "book" in task.lower() and "flight" in task.lower():
            plan = [
                {
                    "action": "navigate",
                    "text": "https://www.google.com",
                    "reasoning": "Start by going to Google to search for flight booking options",
                    "validation": "wait_for_element_text:Google"
                },
                {
                    "action": "type",
                    "text": "book flights",
                    "reasoning": "Search for flight booking websites",
                    "validation": "wait_for_element_text:Search results"
                },
                {
                    "action": "wait",
                    "duration": 1.0,
                    "reasoning": "Wait for search results to load",
                    "validation": None
                }
            ]
        elif "check" in task.lower() and ("email" in task.lower() or "gmail" in task.lower()):
            plan = [
                {
                    "action": "navigate",
                    "text": "https://gmail.com",
                    "reasoning": "Navigate to Gmail",
                    "validation": "wait_for_element_text:Sign in"
                }
            ]
        
        return plan

    def update_context(self, screen_info: Dict):
        """Update context based on current screen state."""
        self.context["current_screen"] = screen_info
        self.context["last_action"] = self.current_plan[self.current_step - 1] if self.current_step > 0 else None
        
    def validate_step(self, validation: Optional[str], screen_info: Dict) -> bool:
        """Validate if the current step was successful."""
        if not validation:
            return True
            
        if validation.startswith("wait_for_element_text:"):
            expected_text = validation.split(":", 1)[1]
            elements = screen_info.get("elements", [])
            return any(expected_text.lower() in e.text.lower() for e in elements if hasattr(e, 'text'))
            
        return False

class ComputerActions:
    def __init__(self):
        self.api_client = APIClient()
        self.planner = ActionPlanner()
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.5

    async def execute_action(self, action_json: str) -> bool:
        """Execute a computer control action with planning and validation."""
        try:
            action = json.loads(action_json) if isinstance(action_json, str) else action_json
            
            # Record the action
            self.api_client.record_action(action)
            
            # Extract action details
            action_type = action.get("action")
            text = action.get("text")
            element_id = action.get("element_id")
            reasoning = action.get("reasoning", "No reasoning provided")
            
            logger.info(f"Executing action: {action_type} - {reasoning}")

            # Create plan for complex tasks
            if action_type == "complex_task":
                screen_info = self.api_client.take_screenshot()
                plan = self.planner.create_plan(text, screen_info)
                
                for step in plan:
                    success = await self.execute_action(step)
                    if not success:
                        logger.error(f"Failed at step: {step}")
                        return False
                    
                    # Update context and validate
                    screen_info = self.api_client.take_screenshot()
                    self.planner.update_context(screen_info)
                    
                    if not self.planner.validate_step(step.get("validation"), screen_info):
                        logger.error(f"Validation failed for step: {step}")
                        return False
                        
                    await asyncio.sleep(0.5)  # Brief pause between steps
                    
                return True

            # Handle single actions
            if action_type == "click" and element_id is not None:
                return await self._handle_click(element_id)
                
            elif action_type == "type":
                if text:
                    if text.lower() in ["spotify", "explorer", "edge", "chrome"]:
                        return await self._launch_application(text)
                    else:
                        pyautogui.write(text)
                        return True
                        
            elif action_type in ["scroll_up", "scroll_down"]:
                amount = 100 if action_type == "scroll_down" else -100
                pyautogui.scroll(amount)
                await self._wait(0.5)
                return True
                
            elif action_type == "wait":
                duration = action.get("duration", 1.0)
                await self._wait(duration)
                return True
                
            elif action_type == "navigate":
                if text:
                    return await self._navigate_to_url(text)
                    
            elif action_type == "hover":
                if element_id is not None:
                    return await self._handle_hover(element_id)

            logger.error(f"Invalid action or missing parameters: {action}")
            return False

        except Exception as e:
            logger.error(f"Error executing action: {str(e)}")
            return False

    async def _handle_click(self, element_id: int) -> bool:
        """Handle clicking on a UI element by ID."""
        try:
            screenshot_data = self.api_client.take_screenshot()
            elements = screenshot_data.get("elements", [])
            
            target_element = next((elem for elem in elements if elem.id == element_id), None)
            
            if target_element and target_element.clickable:
                x1, y1, x2, y2 = target_element.bbox
                center_x = (x1 + x2) // 2
                center_y = (y1 + y2) // 2
                
                # Move mouse smoothly and click
                pyautogui.moveTo(center_x, center_y, duration=0.5)
                pyautogui.click()
                return True
                
            logger.error(f"Element {element_id} not found or not clickable")
            return False
            
        except Exception as e:
            logger.error(f"Error handling click: {str(e)}")
            return False

    async def _handle_hover(self, element_id: int) -> bool:
        """Handle hovering over a UI element."""
        try:
            screenshot_data = self.api_client.take_screenshot()
            elements = screenshot_data.get("elements", [])
            
            target_element = next((elem for elem in elements if elem.id == element_id), None)
            
            if target_element:
                x1, y1, x2, y2 = target_element.bbox
                center_x = (x1 + x2) // 2
                center_y = (y1 + y2) // 2
                
                # Move mouse smoothly without clicking
                pyautogui.moveTo(center_x, center_y, duration=0.5)
                return True
                
            logger.error(f"Element {element_id} not found")
            return False
            
        except Exception as e:
            logger.error(f"Error handling hover: {str(e)}")
            return False

    async def _launch_application(self, app_name: str) -> bool:
        """Launch a common application."""
        try:
            pyautogui.press('win')
            await self._wait(0.5)
            pyautogui.write(app_name)
            await self._wait(0.5)
            pyautogui.press('enter')
            await self._wait(1.0)  # Wait for app to start
            return True
            
        except Exception as e:
            logger.error(f"Error launching application: {str(e)}")
            return False

    async def _navigate_to_url(self, url: str) -> bool:
        """Navigate to a URL using default browser."""
        try:
            # Launch browser if needed
            await self._launch_application('edge')
            await self._wait(1.0)
            
            # Type URL and navigate
            pyautogui.hotkey('ctrl', 'l')  # Focus address bar
            await self._wait(0.5)
            pyautogui.write(url)
            pyautogui.press('enter')
            await self._wait(1.0)
            return True
            
        except Exception as e:
            logger.error(f"Error navigating to URL: {str(e)}")
            return False

    async def _wait(self, seconds: float):
        """Wait for the specified number of seconds."""
        await asyncio.sleep(seconds)