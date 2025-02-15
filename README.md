# 🎙️ Voice Control Computer Assistant

A sophisticated Python-based AI assistant that uses Google's Gemini AI to provide natural language computer control through voice commands. This assistant can understand context from both voice and screen content to perform complex computer operations.

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