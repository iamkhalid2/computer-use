import asyncio
import base64
import tempfile
import os
import wave
import numpy as np
import pyaudio
import speech_recognition as sr
import logging
from .config import FORMAT, CHANNELS, RATE, CHUNK_SIZE

logger = logging.getLogger('AudioManager')

class AudioManager:
    def __init__(self):
        self.audio_stream = None
        self.pya = None
        self.recognizer = sr.Recognizer()
        self.speaking = False
        self.recording = False
        self.audio_buffer = []
        self.silence_threshold = 0.01
        self.silence_duration = 0.5

    async def setup_audio(self):
        """Initialize audio capture."""
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
            return True
        except Exception as e:
            logger.error(f"Audio setup error: {e}")
            return False

    async def process_audio_chunk(self, data: bytes):
        """Convert audio chunk to text using speech recognition."""
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                with wave.open(temp_file.name, 'wb') as wf:
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)
                    wf.setframerate(RATE)
                    wf.writeframes(data)

            with sr.AudioFile(temp_file.name) as source:
                audio = self.recognizer.record(source)
                text = self.recognizer.recognize_google(audio)
                if text.strip():
                    logger.info(f"Recognized: {text}")
                    return text
                
        except sr.UnknownValueError:
            pass
        except Exception as e:
            logger.error(f"Speech recognition error: {e}")
        finally:
            os.unlink(temp_file.name)
        return None

    async def capture_audio(self, callback):
        """Continuously capture and process audio."""
        try:
            if not await self.setup_audio():
                return

            while True:
                if not self.speaking:
                    try:
                        data = await asyncio.to_thread(
                            self.audio_stream.read, CHUNK_SIZE, exception_on_overflow=False)
                        
                        audio_data = np.frombuffer(data, dtype=np.int16)
                        audio_level = np.abs(audio_data).mean()
                        
                        if audio_level > self.silence_threshold * 32767:
                            if not self.recording:
                                logger.info("Started recording")
                            self.audio_buffer.append(data)
                            self.recording = True
                        elif self.recording and len(self.audio_buffer) > 0:
                            if len(self.audio_buffer) > 5:
                                logger.info("Processing audio...")
                                full_audio = b''.join(self.audio_buffer)
                                text = await self.process_audio_chunk(full_audio)
                                if text:
                                    await callback(text)
                            self.audio_buffer = []
                            self.recording = False
                            
                    except OSError as e:
                        logger.error(f"Audio read error: {e}")
                        await asyncio.sleep(0.1)
                else:
                    await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Audio capture error: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
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