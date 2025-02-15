import os
import pyaudio
from dotenv import load_dotenv

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