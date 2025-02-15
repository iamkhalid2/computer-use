import asyncio
import json
import sys
import logging
import sounddevice as sd
import soundfile as sf
import tempfile
import os
import wave
import base64
from modules.audio_manager import AudioManager
from modules.computer_actions import ComputerActions, ActionPlanner
from modules.api_client import APIClient
from modules.config import RECEIVE_SAMPLE_RATE

logger = logging.getLogger('VoiceControl')

class VoiceControl:
    def __init__(self, mode='voice'):
        self.running = True
        self.mode = mode
        self.audio_manager = AudioManager() if mode == 'voice' else None
        self.computer_actions = ComputerActions()
        self.api_client = APIClient()
        self.action_planner = ActionPlanner()
        self.send_screenshots = True

    async def handle_response(self, response: dict):
        """Handle API responses with support for complex tasks."""
        try:
            if "serverContent" in response:
                content = response["serverContent"]
                
                if "modelTurn" in content:
                    turn = content["modelTurn"]
                    for part in turn.get("parts", []):
                        if "text" in part:
                            print("Assistant:", part["text"])
                        elif "inlineData" in part and self.mode == 'voice':
                            audio_data = base64.b64decode(part["inlineData"]["data"])
                            await self.play_audio(audio_data)

                if "turnComplete" in content and self.send_screenshots:
                    try:
                        await self.api_client.send_screenshot()
                    except Exception as e:
                        logger.warning(f"Failed to send screenshot: {str(e)}")
                        self.send_screenshots = False

            elif "toolCall" in response:
                tool_call = response["toolCall"]
                for fc in tool_call.get("functionCalls", []):
                    if fc["name"] == "execute_computer_action":
                        args = fc["args"]
                        if isinstance(args, str):
                            args = json.loads(args)
                        
                        # Handle complex tasks
                        if args.get("action") == "complex_task":
                            await self.api_client.push_task(args.get("text", "Unknown task"))
                            screen_info = self.api_client.take_screenshot()
                            plan = self.action_planner.create_plan(args.get("text"), screen_info)
                            
                            for step in plan:
                                success = await self.computer_actions.execute_action(step)
                                if not success:
                                    logger.error(f"Failed at step: {step}")
                                    break
                                
                                # Update context and validate
                                screen_info = self.api_client.take_screenshot()
                                self.action_planner.update_context(screen_info)
                                
                                if not self.action_planner.validate_step(step.get("validation"), screen_info):
                                    logger.error(f"Validation failed for step: {step}")
                                    break
                                    
                                await asyncio.sleep(0.5)
                            
                            await self.api_client.pop_task()
                            success = True
                        else:
                            success = await self.computer_actions.execute_action(args)
                        
                        try:
                            msg = {
                                "tool_response": {
                                    "function_responses": [{
                                        "name": fc["name"],
                                        "id": fc["id"],
                                        "response": {
                                            "result": "ok" if success else "failed",
                                            "context": {
                                                "task_stack": self.api_client.context["task_stack"],
                                                "session_state": self.api_client.context["session_state"]
                                            }
                                        }
                                    }]
                                }
                            }
                            await self.api_client.ws.send(json.dumps(msg))
                        except Exception as e:
                            logger.error(f"Failed to send tool response: {str(e)}")

        except Exception as e:
            logger.error(f"Error handling response: {str(e)}")

    async def play_audio(self, audio_data: bytes):
        """Play audio response."""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                with wave.open(temp_file.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(RECEIVE_SAMPLE_RATE)
                    wf.writeframes(audio_data)

            data, fs = sf.read(temp_file.name, dtype='float32')
            sd.play(data, fs)
            sd.wait()
            os.unlink(temp_file.name)
            
        except Exception as e:
            logger.error(f"Error playing audio: {str(e)}")

    async def text_input_loop(self):
        """Handle text input mode."""
        print("\nComputer Control Assistant")
        print("Available commands:")
        print("- Normal commands: The assistant will help you control your computer")
        print("- 'exit': Quit the program")
        
        while self.running:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\nEnter command (or 'exit' to quit): ").strip()
                )
                
                if user_input.lower() == 'exit':
                    self.running = False
                    break
                    
                if user_input:
                    try:
                        await self.api_client.send_text(user_input)
                    except Exception as e:
                        print(f"\nError: {str(e)}")
                        print("Attempting to reconnect...")
                        if await self.reconnect():
                            print("Successfully reconnected. Please try your command again.")
                        else:
                            print("Failed to reconnect. Please restart the application.")
                    
            except Exception as e:
                logger.error(f"Error processing text input: {str(e)}")
                break

    async def reconnect(self, max_attempts=3):
        """Attempt to reconnect to the API service."""
        for attempt in range(max_attempts):
            try:
                if await self.api_client.connect() and await self.api_client.setup():
                    return True
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Reconnection attempt {attempt + 1} failed: {str(e)}")
        return False

    async def run(self):
        """Main run loop with enhanced error handling and state management."""
        try:
            logger.info("Starting main loop")
            
            if not await self.api_client.connect():
                logger.error("Failed to connect")
                return

            if not await self.api_client.setup():
                logger.error("Failed to setup connection")
                return

            async with asyncio.TaskGroup() as tg:
                # Create appropriate input task based on mode
                if self.mode == 'voice':
                    input_task = tg.create_task(
                        self.audio_manager.capture_audio(self.api_client.send_text)
                    )
                    logger.info("Audio capture started")
                else:
                    input_task = tg.create_task(self.text_input_loop())
                    logger.info("Text input mode started")

                while True:
                    try:
                        if not self.running:
                            logger.info("Received stop signal")
                            break

                        msg = await self.api_client.ws.recv()
                        try:
                            response = json.loads(msg)
                            await self.handle_response(response)
                        except json.JSONDecodeError:
                            logger.error("Failed to decode response")
                        except Exception as e:
                            logger.error(f"Error handling response: {str(e)}")
                            if "invalid frame payload data" in str(e):
                                logger.warning("Disabled screenshot sending due to compatibility issues")
                                self.send_screenshots = False
                                continue

                    except asyncio.CancelledError:
                        logger.info("Task cancelled")
                        break
                    except Exception as e:
                        if "invalid frame payload data" in str(e):
                            logger.warning("WebSocket error with image data, continuing without screenshots")
                            self.send_screenshots = False
                            continue
                        elif "connection" in str(e).lower():
                            if self.mode == 'text':
                                print("\nConnection lost. Attempting to reconnect...")
                            if await self.reconnect():
                                if self.mode == 'text':
                                    print("Reconnected successfully. Please try your command again.")
                                continue
                            else:
                                if self.mode == 'text':
                                    print("Failed to reconnect. Please restart the application.")
                        logger.error(f"WebSocket error: {str(e)}")
                        break

        except Exception as e:
            logger.error(f"Fatal error: {str(e)}")
        finally:
            self.running = False
            if self.audio_manager:
                self.audio_manager.cleanup()
            if self.api_client.ws:
                try:
                    await self.api_client.ws.close()
                except:
                    pass
            logger.info("Cleanup complete")

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    mode = input("Choose input mode (voice/text): ").lower()
    if mode not in ['voice', 'text']:
        print("Invalid mode. Defaulting to text mode.")
        mode = 'text'

    voice_control = VoiceControl(mode=mode)
    asyncio.run(voice_control.run())
