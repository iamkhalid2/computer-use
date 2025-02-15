import asyncio
import json
import sys
import logging
import sounddevice as sd
import soundfile as sf
import tempfile
import os
import wave
from modules.audio_manager import AudioManager
from modules.computer_actions import ComputerActions
from modules.api_client import APIClient
from modules.config import RECEIVE_SAMPLE_RATE

# Ensure async support for older Python versions
if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('VoiceControl')

class VoiceControl:
    def __init__(self):
        self.running = True
        self.audio_manager = AudioManager()
        self.computer_actions = ComputerActions()
        self.api_client = APIClient()

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
                    await self.api_client.send_screenshot()

            elif "toolCall" in response:
                tool_call = response["toolCall"]
                for fc in tool_call.get("functionCalls", []):
                    if fc["name"] == "execute_computer_action":
                        args = fc["args"]
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        
                        success = await self.computer_actions.execute_action(args)
                        
                        msg = {
                            "tool_response": {
                                "function_responses": [{
                                    "name": fc["name"],
                                    "id": fc["id"],
                                    "response": {"result": "ok" if success else "failed"}
                                }]
                            }
                        }
                        await self.api_client.ws.send(json.dumps(msg))

        except Exception as e:
            logger.error(f"Error handling response: {str(e)}")

    async def play_audio(self, audio_data: bytes):
        """Play audio from model response."""
        try:
            self.audio_manager.speaking = True
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                with wave.open(temp_file.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(RECEIVE_SAMPLE_RATE)
                    wf.writeframes(audio_data)
                
                data, fs = sf.read(temp_file.name)
                sd.play(data, fs)
                sd.wait()
        finally:
            self.audio_manager.speaking = False
            os.unlink(temp_file.name)

    async def run(self):
        """Main run loop."""
        try:
            logger.info("Starting main loop")
            
            if not await self.api_client.connect():
                logger.error("Failed to connect")
                return

            if not await self.api_client.setup():
                logger.error("Failed to setup connection")
                return

            async with asyncio.TaskGroup() as tg:
                audio_task = tg.create_task(
                    self.audio_manager.capture_audio(self.api_client.send_text)
                )
                logger.info("Audio capture started")

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
            self.audio_manager.cleanup()
            if self.api_client.ws:
                try:
                    await self.api_client.ws.close()
                except:
                    pass
            logger.info("Cleanup complete")

def main():
    try:
        logger.setLevel(logging.DEBUG)
        controller = VoiceControl()
        asyncio.run(controller.run())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")

if __name__ == "__main__":
    main()
