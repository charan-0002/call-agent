# AI Calling Agent
Google Form → FastAPI → Twilio → Deepgram STT → Groq LLM → Deepgram TTS

## Stack
- FastAPI + uvicorn
- Twilio (outbound call + media streams)
- Deepgram nova-2 (STT) + Aura Asteria (TTS)
- Groq llama-3.3-70b (LLM brain)

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure .env
```bash
cp .env.example .env
# Fill in all keys
```

### 3. Get your keys
- Twilio: https://console.twilio.com → Account SID + Auth Token + buy a number
- Groq: https://console.groq.com → API Keys
- Deepgram: https://console.deepgram.com → API Keys (free $200 credits)

### 4. Run ngrok (for dev)
```bash
ngrok http 8000
# Copy the https URL → paste as SERVER_URL in .env
```

### 5. Start the server
```bash
uvicorn main:app --port 8000 --reload
```

### 6. Google Form setup
1. Open your Google Form
2. Extensions → Apps Script
3. Paste contents of apps_script.js
4. Replace WEBHOOK_URL with your ngrok URL + /form-submit
5. Save → Triggers → Add trigger:
   - Function: onFormSubmit
   - Event source: From form
   - Event type: On form submit

### 7. Test
Submit your form with a verified phone number → you'll get a call in seconds!

## For Indian numbers (Jio/Airtel etc.)
- Twilio trial accounts can ONLY call verified numbers
  Go to Twilio Console → Phone Numbers → Verified Caller IDs → add your number
- Numbers are auto-formatted to +91XXXXXXXXXX in main.py
- If your form field name is different, update the `data.get(...)` lines in /form-submit

## Customize the agent
Edit SYSTEM_PROMPT in main.py to match your exact use case.
Keep instructions short — the LLM replies in 2 sentences max.

## Voices available (change in deepgram_tts function)
- aura-asteria-en   → female, warm, most natural ✅ (current)
- aura-orion-en     → male, clear
- aura-zeus-en      → male, deep
- aura-luna-en      → female, soft
