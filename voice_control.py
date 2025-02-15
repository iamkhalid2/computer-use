import asyncio
import base64
import contextlib
import io
import json
import os
import sys
import time
import wave
import tempfile
from typing import Optional
from dotenv import load_dotenv
import speech_recognition as sr
import numpy as np
from scipy.io import wavfile
import sounddevice as sd
import soundfile as sf

import cv2
import pyaudio
import pyautogui
import PIL.Image
import mss
from websockets.asyncio.client import connect
import logging

# Ensure async support for older Python versions
if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

# Load environment variables
load_dotenv()

# Audio constants
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK_SIZE = 512
RECEIVE_SAMPLE_RATE = 16000

# API settings
HOST = "generativelanguage.googleapis.com"
MODEL = "models/gemini-2.0-flash-exp"

# Get API key from environment variable
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    raise ValueError("GOOGLE_API_KEY not found in environment variables. Please check your .env file.")

URI = f"wss://{HOST}/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={API_KEY}"

# Add logger configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ComputerControl')

class ComputerControl:
    def __init__(self):
        self.ws = None
        self.audio_stream = None
        self.screenshot = None
        self.running = True
        self.pya = None
        self.recognizer = sr.Recognizer()
        self.speaking = False
        self.recording = True
        self.audio_buffer = []
        self.silence_threshold = 0.01  # Lower threshold to be more sensitive
        self.silence_duration = 0.5    # Shorter silence duration before processing
        pyautogui.FAILSAFE = True
        logger.info("Initializing ComputerControl")
        
    def take_screenshot(self) -> dict:
        """Take a screenshot and return it in the format expected by Gemini."""
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            screenshot = sct.grab(monitor)
            
            # Convert to PIL Image
            img = PIL.Image.frombytes('RGB', screenshot.size, screenshot.rgb)
            
            # Resize if too large (Gemini has limits)
            img.thumbnail([1024, 1024])
            
            # Convert to bytes
            image_bytes = io.BytesIO()
            img.save(image_bytes, format='JPEG')
            image_bytes.seek(0)
            
            return {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(image_bytes.getvalue()).decode()
            }

    async def execute_action(self, action: dict):
        """Execute a computer control action."""
        try:
            # Ensure action is a dictionary even if it comes as a string
            if isinstance(action, str):
                action = json.loads(action)
                
            action_type = action.get("action")  # Using action instead of type
            logger.debug(f"Executing action: {action_type} with params: {action}")
            
            if action_type == "click":
                x = int(action.get("x", 0))
                y = int(action.get("y", 0))
                pyautogui.click(x, y)
            elif action_type == "type":
                text = str(action.get("text", ""))
                # For Windows commands that need Win key
                if text.lower() in ["spotify", "explorer", "edge", "chrome"]:
                    pyautogui.hotkey('winleft', 'r')
                    pyautogui.sleep(0.5)  # Wait for Run dialog
                    if text.lower() == "spotify":
                        pyautogui.typewrite("spotify")
                    elif text.lower() == "explorer":
                        pyautogui.typewrite("explorer")
                    elif text.lower() == "edge":
                        pyautogui.typewrite("msedge")
                    elif text.lower() == "chrome":
                        pyautogui.typewrite("chrome")
                    pyautogui.press('enter')
                else:
                    pyautogui.typewrite(text)
            elif action_type == "press":
                key = str(action.get("key", ""))
                # Handle special key combinations
                if key == "win":
                    pyautogui.press('winleft')
                elif key == "minimize":
                    pyautogui.hotkey('winleft', 'down')
                elif key == "maximize":
                    pyautogui.hotkey('winleft', 'up')
                elif "+" in key:  # Handle key combinations like "alt+tab"
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

    async def setup(self):
        """Perform initial API setup."""
        try:
            logger.info("Setting up API connection")
            
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
                    "model": MODEL,
                    "tools": [{"function_declarations": [function_declaration]}],
                    "generation_config": {
                        "temperature": 0.7,
                        "response_modalities": ["TEXT"]  # Start with text only first
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

    async def send_audio_chunk(self, data: bytes):
        """Send an audio chunk to the API."""
        msg = {
            "realtime_input": {
                "media_chunks": [{
                    "data": base64.b64encode(data).decode(),
                    "mime_type": "audio/pcm",
                }]
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

    async def process_audio_chunk(self, data: bytes):
        """Convert audio chunk to text using speech recognition."""
        try:
            # Convert bytes to wav file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                with wave.open(temp_file.name, 'wb') as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(RATE)
                    wf.writeframes(data)

            # Use speech recognition
            with sr.AudioFile(temp_file.name) as source:
                audio = self.recognizer.record(source)
                text = self.recognizer.recognize_google(audio)
                if text.strip():
                    logger.info(f"Recognized: {text}")
                    return text
                
        except sr.UnknownValueError:
            pass  # Speech not understood
        except Exception as e:
            logger.error(f"Speech recognition error: {e}")
        finally:
            os.unlink(temp_file.name)
        return None

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

    async def listen_audio(self):
        """Capture audio and detect speech."""
        try:
            self.pya = pyaudio.PyAudio()
            mic_info = self.pya.get_default_input_device_info()
            logger.info(f"Using microphone: {mic_info['name']}")
            
            self.audio_stream = self.pya.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                input_device_index=mic_info["index"],
                frames_per_buffer=CHUNK_SIZE
            )

            while self.running:
                if not self.speaking:  # Don't record while model is speaking
                    try:
                        data = await asyncio.to_thread(
                            self.audio_stream.read, CHUNK_SIZE, exception_on_overflow=False)
                        
                        # Check audio level for silence detection
                        audio_data = np.frombuffer(data, dtype=np.int16)
                        audio_level = np.abs(audio_data).mean()
                        
                        if audio_level > self.silence_threshold * 32767:
                            if not self.recording:
                                logger.info("Started recording")
                            self.audio_buffer.append(data)
                            self.recording = True
                        elif self.recording and len(self.audio_buffer) > 0:
                            # Only process if we have enough audio data
                            if len(self.audio_buffer) > 5:  # At least 5 chunks
                                logger.info("Processing audio...")
                                full_audio = b''.join(self.audio_buffer)
                                text = await self.process_audio_chunk(full_audio)
                                if text:
                                    await self.send_text(text)
                            self.audio_buffer = []
                            self.recording = False
                            
                    except OSError as e:
                        if not self.running:
                            break
                        logger.error(f"Audio read error: {e}")
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Unexpected audio error: {e}")
                        break
                else:
                    await asyncio.sleep(0.1)  # Wait while speaking

        except Exception as e:
            logger.error(f"Audio setup error: {e}")
            logger.exception(e)  # Print full stack trace
        finally:
            self.cleanup_audio()

    async def play_audio(self, audio_data: bytes):
        """Play audio from model response."""
        try:
            self.speaking = True
            # Convert PCM to wav
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                with wave.open(temp_file.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(RECEIVE_SAMPLE_RATE)
                    wf.writeframes(audio_data)
                
                # Play audio
                data, fs = sf.read(temp_file.name)
                sd.play(data, fs)
                sd.wait()
        finally:
            self.speaking = False
            os.unlink(temp_file.name)

    async def handle_response(self, response: dict):
        """Handle API responses."""
        try:
            if "serverContent" in response:
                content = response["serverContent"]
                
                if "modelTurn" in content:
                    turn = content["modelTurn"]
                    for part in turn.get("parts", []):
                        if "text" in part:
                            print("Gemini:", part["text"])
                        elif "inlineData" in part:
                            # Play audio response
                            audio_data = base64.b64decode(part["inlineData"]["data"])
                            await self.play_audio(audio_data)

                if "turnComplete" in content:
                    await self.send_screenshot()

            elif "toolCall" in response:
                tool_call = response["toolCall"]
                for fc in tool_call.get("functionCalls", []):
                    if fc["name"] == "execute_computer_action":
                        args = fc["args"]
                        # Ensure args is properly encoded as JSON string if needed
                        if isinstance(args, dict):
                            args = json.dumps(args)
                            
                        success = await self.execute_action(args)
                        
                        # Send response back to API
                        msg = {
                            "tool_response": {
                                "function_responses": [{
                                    "name": fc["name"],
                                    "id": fc["id"],
                                    "response": {"result": "ok" if success else "failed"}
                                }]
                            }
                        }
                        await self.ws.send(json.dumps(msg))

        except Exception as e:
            logger.error(f"Error handling response: {str(e)}")

    def cleanup_audio(self):
        """Clean up audio resources."""
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except:
                pass
            self.audio_stream = None
            
        if self.pya:
            try:
                self.pya.terminate()
            except:
                pass
            self.pya = None

    async def run(self):
        """Main run loop."""
        try:
            logger.info("Starting main loop")
            async with connect(URI) as ws:
                self.ws = ws
                if not await self.setup():
                    logger.error("Failed to setup connection")
                    return

                async with asyncio.TaskGroup() as tg:
                    audio_task = tg.create_task(self.listen_audio())
                    logger.info("Audio capture started")

                    while True:
                        try:
                            if not self.running:
                                logger.info("Received stop signal")
                                break

                            msg = await ws.recv()
                            try:
                                response = json.loads(msg)
                                await self.handle_response(response)
                            except json.JSONDecodeError:
                                logger.error("Failed to decode response")
                            except Exception as e:
                                logger.error(f"Error handling response: {str(e)}")

                        except asyncio.CancelledError:
                            logger.info("Task cancelled")
                            break
                        except Exception as e:
                            logger.error(f"WebSocket error: {str(e)}")
                            break

        except Exception as e:
            logger.error(f"Fatal error: {str(e)}")
        finally:
            self.running = False
            self.cleanup_audio()
            if self.ws:
                try:
                    await self.ws.close()
                except:
                    pass
            logger.info("Cleanup complete")

if __name__ == "__main__":
    try:
        # Enable debug logging
        logger.setLevel(logging.DEBUG)
        controller = ComputerControl()
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
