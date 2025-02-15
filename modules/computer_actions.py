import json
import pyautogui
import logging

logger = logging.getLogger('ComputerActions')

class ComputerActions:
    def __init__(self):
        pyautogui.FAILSAFE = True

    async def execute_action(self, action: dict):
        """Execute a computer control action."""
        try:
            if isinstance(action, str):
                action = json.loads(action)
                
            action_type = action.get("action")
            logger.debug(f"Executing action: {action_type} with params: {action}")
            
            if action_type == "click":
                x = int(action.get("x", 0))
                y = int(action.get("y", 0))
                pyautogui.click(x, y)
            elif action_type == "type":
                text = str(action.get("text", ""))
                if text.lower() in ["spotify", "explorer", "edge", "chrome"]:
                    pyautogui.hotkey('winleft', 'r')
                    pyautogui.sleep(0.5)
                    pyautogui.typewrite(text.lower())
                    pyautogui.press('enter')
                else:
                    pyautogui.typewrite(text)
            elif action_type == "press":
                key = str(action.get("key", ""))
                if key == "win":
                    pyautogui.press('winleft')
                elif key == "minimize":
                    pyautogui.hotkey('winleft', 'down')
                elif key == "maximize":
                    pyautogui.hotkey('winleft', 'up')
                elif "+" in key:
                    keys = key.split("+")
                    pyautogui.hotkey(*keys)
                else:
                    pyautogui.press(key)
            elif action_type == "moveTo":
                x = int(action.get("x", 0))
                y = int(action.get("y", 0)) 
                pyautogui.moveTo(x, y, duration=0.5)

            logger.info(f"Successfully executed action: {action_type}")
            return True

        except Exception as e:
            logger.error(f"Failed to execute action: {str(e)}")
            return False