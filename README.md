# AI Calling Agent
Automatically calls leads the moment they submit a Google Form.
Built with FastAPI, Twilio, Deepgram (STT + TTS), and Groq LLM.

## How it works
1. Lead fills Google Form with phone number
2. Apps Script fires webhook to FastAPI server
3. Server triggers Twilio outbound call
4. Call connects via WebSocket media stream
5. Deepgram nova-2 transcribes speech in real-time
6. Groq llama-3.3-70b generates response
7. Deepgram Aura Asteria speaks the reply back

## Tech Stack
- FastAPI + uvicorn
- Twilio Programmable Voice + Media Streams
- Deepgram nova-2 (STT) + Aura Asteria (TTS)
- Groq llama-3.3-70b (LLM)
- Google Forms + Apps Script

## Setup
1. Clone the repo
2. Copy .env.example to .env and fill in your keys
3. pip install -r requirements.txt
4. Run ngrok: ngrok http 8000
5. Update SERVER_URL in .env with ngrok URL
6. uvicorn main:app --port 8000 --reload
7. Set up Google Form trigger using apps_script.js

## Get API Keys
- Twilio: https://console.twilio.com
- Groq: https://console.groq.com (free)
- Deepgram: https://console.deepgram.com (free $200 credits)
