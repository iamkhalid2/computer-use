# 🎙️ Voice Control Computer Assistant

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Gemini AI](https://img.shields.io/badge/AI-Gemini-brightgreen.svg)](https://deepmind.google/technologies/gemini/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A Python-based system that leverages Google's Gemini AI API for real-time computer control through natural language processing. The application implements a bidirectional WebSocket architecture for seamless AI communication, coupled with multi-threaded audio processing and computer vision for contextual awareness. Features include real-time PCM audio streaming, adaptive silence detection algorithms, and a modular command execution system for OS-level control operations.

Core technical implementations include concurrent audio buffer management using asyncio, WebSocket-based streaming for real-time AI interactions, and computer vision integration for contextual screen analysis. The system employs a state management system for handling various input/output modalities and provides cross-platform support through PyAutoGUI for system control operations.


## ✨ Features
- 🗣️ Advanced voice command recognition with natural language processing
- 🖥️ Real-time screen analysis for contextual understanding
- 🖱️ Precise computer control capabilities:
  - Mouse movement and click simulation
  - Keyboard input and hotkey combinations
  - Application launching and window management
- 🎯 Intelligent command interpretation using Gemini AI
- 🔄 Real-time audio processing with noise reduction
- 📊 Adaptive silence detection for better voice recognition
- 🤖 WebSocket-based real-time communication with Gemini AI
- 🔒 Secure API key management through environment variables

## 🛠️ Technical Stack
- Python 3.8+ 
- Google Gemini AI API
- WebSocket for real-time communication
- PyAudio for audio processing
- OpenCV and MSS for screen capture
- NumPy for audio analysis
- SpeechRecognition for voice-to-text

## 📋 Requirements
- 🐍 Python 3.8 or higher
- 🎤 Working microphone for voice input
- 🔑 Google Gemini API key
- 💻 Windows/Linux/MacOS compatible

## 🚀 Setup
1. Clone the repository
2. Create a `.env` file in the project root and add your Gemini API key:
   ```
   GOOGLE_API_KEY=your_api_key_here
   ```
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

## 🏃‍♂️ Running the Application
```bash
python voice_control.py
```

## 💡 Usage Examples
- "Open Spotify and play my favorite playlist"
- "Move the mouse to the top-right corner"
- "Minimize all windows"
- "Switch to Chrome and open a new tab"
- "Type out an email response"
- "Press Alt+Tab to switch windows"

## ⚠️ Notes
- Ensure your microphone is properly configured and set as the default input device
- The assistant works best in a quiet environment
- For optimal performance, speak clearly and naturally
- Some commands may require administrator privileges

## 🤝 Contributing
Contributions are welcome! Feel free to submit issues and pull requests.

## 📄 License
This project is licensed under the MIT License - see the LICENSE file for details.