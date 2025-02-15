import json
import mss
import PIL.Image
import logging
import base64
import io
import cv2
import numpy as np
from websockets.asyncio.client import connect
from .config import URI

logger = logging.getLogger('APIClient')

class APIClient:
    def __init__(self):
        self.ws = None
        self.last_screenshot = None
        self.last_screenshot_cv = None

    def take_screenshot(self) -> dict:
        """Take a screenshot and return it in the format expected by Gemini."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            screenshot = sct.grab(monitor)
            img = PIL.Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            
            # Store the screenshot for element detection
            self.last_screenshot = img
            # Convert PIL image to CV2 format for detection
            self.last_screenshot_cv = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            
            # Convert to base64 using JPEG format
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=70)
            img_byte_arr = img_byte_arr.getvalue()
            buffer = base64.b64encode(img_byte_arr)
                
            return {
                "mime_type": "image/jpeg",
                "data": buffer.decode()
            }

    def find_ui_element(self, element_name: str) -> tuple[int, int]:
        """Find UI element coordinates using template matching."""
        if self.last_screenshot_cv is None:
            self.take_screenshot()

        # Define paths to template images
        template_paths = {
            "wifi": "ui_templates/wifi_icon.png",
            "battery": "ui_templates/battery_icon.png",
            "volume": "ui_templates/volume_icon.png",
            "start": "ui_templates/start_button.png",
            # Add more UI elements as needed
        }

        # Try to find the element by template matching
        if element_name.lower() in template_paths:
            try:
                template = cv2.imread(template_paths[element_name.lower()])
                if template is not None:
                    result = cv2.matchTemplate(self.last_screenshot_cv, template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    
                    if max_val > 0.8:  # Confidence threshold
                        return (max_loc[0] + template.shape[1]//2, max_loc[1] + template.shape[0]//2)
            except Exception as e:
                logger.error(f"Error finding UI element: {str(e)}")

        # If template matching fails, try OCR for text-based elements
        try:
            import pytesseract
            gray = cv2.cvtColor(self.last_screenshot_cv, cv2.COLOR_BGR2GRAY)
            text_data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)
            
            for i, text in enumerate(text_data['text']):
                if element_name.lower() in text.lower():
                    x = text_data['left'][i]
                    y = text_data['top'][i]
                    w = text_data['width'][i]
                    h = text_data['height'][i]
                    return (x + w//2, y + h//2)
        except Exception as e:
            logger.error(f"Error performing OCR: {str(e)}")

        return None

    async def setup(self):
        """Perform initial API setup."""
        try:
            function_declaration = {
                "name": "execute_computer_action",
                "description": """Execute actions to control the computer. Available actions:
                    - type: Type text or launch applications (spotify, explorer, edge, chrome)
                    - press: Press keyboard keys including combinations (win, minimize, maximize, alt+tab)
                    - click: Click at x,y coordinates 
                    - moveTo: Move mouse to x,y coordinates
                    - moveToElement: Move mouse to a named UI element""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string", 
                            "enum": ["click", "type", "press", "moveTo", "moveToElement"],
                            "description": "Type of action"
                        },
                        "x": {
                            "type": "integer",
                            "description": "X coordinate for click/moveTo"
                        },
                        "y": {
                            "type": "integer", 
                            "description": "Y coordinate for click/moveTo"
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type or app to launch (spotify, explorer, edge, chrome)"
                        },
                        "key": {
                            "type": "string",
                            "description": "Key to press (win, minimize, maximize, alt+tab or regular keys)"
                        },
                        "element": {
                            "type": "string",
                            "description": "Name of UI element to interact with (wifi, battery, volume, start, etc.)"
                        }
                    },
                    "required": ["action"]
                }
            }

            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.0-flash-exp",
                    "tools": [{"function_declarations": [function_declaration]}],
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

    async def send_text(self, text: str):
        """Send text input to the API."""
        logger.info(f"Sending text: {text}")
        msg = {
            "client_content": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": text}]
                }],
                "turn_complete": True
            }
        }
        await self.ws.send(json.dumps(msg))

    async def send_screenshot(self):
        """Send a screenshot to the API."""
        screenshot = self.take_screenshot()
        msg = {
            "realtime_input": {
                "media_chunks": [screenshot]
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