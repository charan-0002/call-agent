import os, json, asyncio, base64, httpx
from datetime import datetime
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from groq import Groq
from deepgram import (
    DeepgramClient,
    LiveTranscriptionEvents,
    LiveOptions,
    DeepgramClientOptions,
)
from deepgram.clients.listen import LiveTranscriptionEvents
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# ── Clients ──────────────────────────────────────────────────────────────────
twilio_client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
groq_client   = Groq(api_key=os.environ["GROQ_KEY"])
deepgram      = DeepgramClient(os.environ["DEEPGRAM_KEY"])

TWILIO_NUMBER = os.environ["TWILIO_NUMBER"]
SERVER_URL    = os.environ["SERVER_URL"]   # e.g. https://abc.ngrok.io

# ── System prompt — edit this to match your use case ─────────────────────────
SYSTEM_PROMPT = """You are a friendly hostess for an upscale adults-only entertainment lounge.

Your job is to warmly greet callers, explain available companionship, conversation, private VIP experiences, and relaxation packages without implying or offering sexual services.

Keep every reply under 2 short sentences. Be conversational, confident, and charming.

If someone asks about pricing, explain the available packages in a professional manner. If they are not interested, politely thank them for their time and wish them a wonderful day. Never describe explicit sexual acts or offer illegal services."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. Google Form webhook — receives phone number, fires the call
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/form-submit")
async def form_submit(request: Request):
    data = await request.json()

    # Try common field names — update to match your exact Google Form field
    phone = (
        data.get("Phone Number")
        or data.get("phone")
        or data.get("Mobile Number")
        or data.get("Contact Number")
    )

    if not phone:
        return {"status": "error", "msg": "No phone field found in form data"}

    # Normalize Indian numbers to E.164 format
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+91" + phone[1:]
    elif not phone.startswith("+"):
        phone = "+91" + phone

    make_call(phone)
    return {"status": "ok", "calling": phone}


def make_call(to_number: str):
    twilio_client.calls.create(
        to=to_number,
        from_=TWILIO_NUMBER,
        url=f"{SERVER_URL}/call-twiml",
    )
    print(f"[CALL] Dialing {to_number}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Twilio fetches TwiML — tells Twilio to open a media stream WebSocket
# ─────────────────────────────────────────────────────────────────────────────
@app.api_route("/call-twiml", methods=["GET", "POST"])
async def call_twiml():
    resp = VoiceResponse()
    connect = Connect()
    ws_url = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f"{ws_url}/media-stream")
    resp.append(connect)
    return Response(content=str(resp), media_type="application/xml")


# ─────────────────────────────────────────────────────────────────────────────
# 3. WebSocket — real-time audio bridge
#    Twilio ↔ Deepgram STT → Groq LLM → Deepgram TTS → Twilio
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    print("[WS] Call connected")

    conversation     = [{"role": "system", "content": SYSTEM_PROMPT}]
    transcript_parts = []
    stream_sid       = None
    is_processing    = False  # prevent overlapping LLM calls

    # ── Deepgram live STT connection ─────────────────────────────────────────
    dg_conn = deepgram.listen.asynclive.v("1")

    async def on_message(self, result, **kwargs):
        nonlocal is_processing
        sentence = result.channel.alternatives[0].transcript
        if result.is_final and sentence.strip() and not is_processing:
            transcript_parts.append(sentence)
            print(f"[STT] {sentence}")

    async def on_error(self, error, **kwargs):
        print(f"[STT ERROR] {error}")

    dg_conn.on(LiveTranscriptionEvents.Transcript, on_message)
    dg_conn.on(LiveTranscriptionEvents.Error, on_error)

    await dg_conn.start(LiveOptions(
        model="nova-2",
        language="en-IN",          # handles Indian accents well
        encoding="mulaw",
        sample_rate=8000,
        channels=1,
        interim_results=False,
        endpointing=400,           # ms of silence = end of utterance
    ))

    # ── Main message loop ────────────────────────────────────────────────────
    async def process_conversation():
        nonlocal is_processing, stream_sid
        while True:
            await asyncio.sleep(0.5)
            if transcript_parts and not is_processing:
                is_processing = True
                user_text = " ".join(transcript_parts).strip()
                transcript_parts.clear()

                print(f"[USER] {user_text}")
                conversation.append({"role": "user", "content": user_text})

                # Groq LLM reply
                reply = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=conversation,
                    max_tokens=100,
                    temperature=0.7,
                ).choices[0].message.content.strip()

                conversation.append({"role": "assistant", "content": reply})
                print(f"[AGENT] {reply}")

                print("\n" + "="*50)
                print("FULL CONVERSATION SO FAR:")
                for msg in conversation:
                    if msg["role"] == "user":
                        print(f"  YOU  : {msg['content']}")
                    elif msg["role"] == "assistant":
                        print(f"  AGENT: {msg['content']}")
                print("="*50 + "\n")

                # Deepgram TTS → send to Twilio
                audio = await deepgram_tts(reply)
                if audio and stream_sid:
                    await send_audio_to_twilio(ws, audio, stream_sid)

                is_processing = False

    asyncio.create_task(process_conversation())

    # ── Receive Twilio media frames ───────────────────────────────────────────
    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                stream_sid = msg["start"]["streamSid"]
                print(f"[TWILIO] Stream started: {stream_sid}")
                # Now send opening since we have stream_sid
                opening = "Hi! I'm calling regarding your recent enquiry. Are you interested in learning more about our service?"
                audio = await deepgram_tts(opening)
                if audio:
                    await send_audio_to_twilio(ws, audio, stream_sid)

            elif event == "media":
                payload = base64.b64decode(msg["media"]["payload"])
                await dg_conn.send(payload)

            elif event == "stop":
                print("[TWILIO] Call ended")
                break

    except Exception as e:
        print(f"[ERROR] {e}")
    finally:
        await dg_conn.finish()
        print("[WS] Disconnected")

        # Save the conversation transcript to a unique file
        if stream_sid and len(conversation) > 1:
            os.makedirs("transcripts", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"transcripts/call_{timestamp}_{stream_sid}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write("FULL CONVERSATION TRANSCRIPT\n")
                f.write("="*50 + "\n")
                for msg in conversation:
                    if msg["role"] == "user":
                        f.write(f"YOU  : {msg['content']}\n")
                    elif msg["role"] == "assistant":
                        f.write(f"AGENT: {msg['content']}\n")
                f.write("="*50 + "\n")
            print(f"[*] Transcript permanently saved to {filename}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Deepgram TTS — Aura Asteria (most human-sounding)
# ─────────────────────────────────────────────────────────────────────────────
async def deepgram_tts(text: str) -> bytes | None:
    url = "https://api.deepgram.com/v1/speak"
    params = {
        "model": "aura-asteria-en",   # most natural voice
        "encoding": "mulaw",          # Twilio requires mulaw
        "sample_rate": 8000,
        "container": "none",
    }
    headers = {
        "Authorization": f"Token {os.environ['DEEPGRAM_KEY']}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, params=params, headers=headers,
                                  json={"text": text})
            if r.status_code == 200:
                return r.content
            print(f"[TTS ERROR] {r.status_code}: {r.text}")
            return None
    except Exception as e:
        print(f"[TTS EXCEPTION] {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Send audio bytes back to Twilio over the WebSocket
# ─────────────────────────────────────────────────────────────────────────────
async def send_audio_to_twilio(ws: WebSocket, audio: bytes, stream_sid: str):
    # Twilio expects base64-encoded mulaw audio in a media event
    chunk_size = 160  # 20ms chunks at 8kHz mulaw
    for i in range(0, len(audio), chunk_size):
        chunk = audio[i:i + chunk_size]
        payload = base64.b64encode(chunk).decode("utf-8")
        await ws.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload}
        }))
    # Send mark event so Twilio knows audio chunk is done
    await ws.send_text(json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": "audio_done"}
    }))
