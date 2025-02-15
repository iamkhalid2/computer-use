import json
import mss
import PIL.Image
import logging
import base64
from websockets.asyncio.client import connect
from .config import URI

logger = logging.getLogger('APIClient')

class APIClient:
    def __init__(self):
        self.ws = None

    def take_screenshot(self) -> dict:
        """Take a screenshot and return it in the format expected by Gemini."""
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            screenshot = sct.grab(monitor)
            img = PIL.Image.frombytes("RGB", screenshot.size, screenshot.rgb)
            
            # Convert to base64
            with PIL.Image.new("RGB", screenshot.size) as background:
                background.paste(img)
                buffer = base64.b64encode(background.tobytes())
                
            return {
                "mime_type": "image/png",
                "data": buffer.decode()
            }

    async def setup(self):
        """Perform initial API setup."""
        try:
            function_declaration = {
                "name": "execute_computer_action",
                "description": """Execute actions to control the computer. Available actions:
                    - type: Type text or launch applications (spotify, explorer, edge, chrome)
                    - press: Press keyboard keys including combinations (win, minimize, maximize, alt+tab)
                    - click: Click at x,y coordinates 
                    - moveTo: Move mouse to x,y coordinates""",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string", 
                            "enum": ["click", "type", "press", "moveTo"],
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