# 🎙️ Voice Control Computer Assistant

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Python-based system that leverages Google's Gemini AI API for real-time computer control through voice or text input. The application implements a bidirectional WebSocket architecture for seamless AI communication, coupled with multi-threaded audio processing, computer vision, and OCR for contextual awareness.

## ✨ Features
- 🗣️ Dual input modes: voice commands or text input
- 🖥️ Real-time screen analysis with OCR and UI element detection
- 🖱️ Precise computer control capabilities:
  - Mouse movement and click simulation
  - Keyboard input and hotkey combinations
  - Application launching and window management
- 🎯 Intelligent command interpretation using Gemini AI
- 🔄 Real-time audio processing with noise reduction
- 📊 Adaptive silence detection for better voice recognition
- 🤖 WebSocket-based real-time communication with Gemini AI
- 🔍 OCR-powered text recognition on screen
- 🎯 UI element detection and classification

## 🛠️ Prerequisites
1. Python 3.8 or higher
2. Tesseract OCR ([Download and install from here](https://github.com/UB-Mannheim/tesseract/wiki))
3. Working microphone (for voice input)
4. Google Gemini API key

## 🚀 Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd computer_control
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
# On Windows
.\venv\Scripts\activate
# On Linux/MacOS
source venv/bin/activate
```

3. Install Python dependencies:
```bash
pip install -r requirements.txt
```

4. Install Tesseract OCR:
   - Windows: Download and install from [UB-Mannheim's repository](https://github.com/UB-Mannheim/tesseract/wiki)
   - Linux: `sudo apt-get install tesseract-ocr`
   - MacOS: `brew install tesseract`

5. Create a .env file in the project root:
```
GOOGLE_API_KEY=your_api_key_here
```

## 🏃‍♂️ Usage

1. Activate the virtual environment if not already activated:
```bash
# On Windows
.\venv\Scripts\activate
# On Linux/MacOS
source venv/bin/activate
```

2. Run the application:
```bash
python voice_control.py
```

3. Choose your preferred input mode when prompted:
   - `voice`: Use voice commands
   - `text`: Use text input

## 💡 Example Commands
- "Open Spotify and play my favorite playlist"
- "Check for the cheapest flights from LA to New York"
- "Open Chrome and search for the weather"
- "Find and click the WiFi icon"
- "Minimize all windows"
- "Type out an email response"

## ⚠️ Notes
- Ensure Tesseract OCR is properly installed and in PATH
- For voice mode, ensure your microphone is properly configured
- The assistant works best in a quiet environment for voice commands
- Some commands may require administrator privileges
- Screenshots are analyzed in real-time for UI element detection

## 🤝 Contributing
Contributions are welcome! Feel free to submit issues and pull requests.

## 📄 License
This project is licensed under the MIT License - see the LICENSE file for details.