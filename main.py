import os, json, asyncio, base64, httpx, difflib
from datetime import datetime
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from groq import Groq
from deepgram import (
    DeepgramClient,
    LiveTranscriptionEvents,
    LiveOptions,
)
from deepgram.clients.listen import LiveTranscriptionEvents
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from database import get_db, init_db
from menu import MENU

load_dotenv()

app = FastAPI()

class CallRequest(BaseModel):
    table_number: int

# Hardcoded for Twilio Trial
CUSTOMER_PHONE = "+917483691261"

def get_best_match(name, choices):
    matches = difflib.get_close_matches(name, choices, n=1, cutoff=0.5)
    return matches[0] if matches else None

# Enable CORS for the React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup_event():
    init_db()

# ── Clients ──────────────────────────────────────────────────────────────────
twilio_client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
groq_client   = Groq(api_key=os.environ["GROQ_KEY"])
deepgram      = DeepgramClient(os.environ["DEEPGRAM_KEY"])

TWILIO_NUMBER = os.environ["TWILIO_NUMBER"]
SERVER_URL    = os.environ["SERVER_URL"]   # e.g. https://abc.ngrok.io

# ── System prompt — edit this to match your use case ─────────────────────────
SYSTEM_PROMPT = """# ROLE AND PERSONA
You are a friendly and professional waiter at "The Grand Azure," an upscale restaurant.
Your job is to greet the guest, take their order, and recommend special dishes if asked.
Tone: Warm, polite, and helpful.

# CRITICAL CONVERSATION RULES
1. ONE QUESTION LIMIT: End every response with exactly ONE question to hand the conversation back to the user.
2. CONCISENESS: Keep your responses under 2 sentences so they are easily spoken over the phone.
3. AI CONCEALMENT: Never reveal you are an AI. You are a human waiter.
4. TERMINATION: If the user wishes to finish their order or end the conversation, output exactly: [HANGUP]

# MENU KNOWLEDGE
- Special Dishes: Truffle Butter Filet Mignon, Pan-Seared Scallops with Lemon Risotto, and our famous Molten Chocolate Lava Cake.
- Drinks: We have a full bar, freshly squeezed juices, and artisanal coffees.

# CONVERSATIONAL FLOW
1. Greet the guest warmly and ask if they are ready to order or if they'd like to hear the specials.
2. Answer any questions about the menu or take their order.
3. Once they order, confirm the items and ask if they need anything else (like drinks or dessert).
4. When they say they are done, thank them, tell them their food will be right out, and output [HANGUP]."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. API Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/call")
async def call_table(request: CallRequest):
    table_number = request.table_number
    
    # Mark table as occupied
    conn = get_db()
    conn.execute("UPDATE tables SET status = 'Occupied' WHERE id = ?", (table_number,))
    conn.commit()
    conn.close()

    twilio_client.calls.create(
        to=CUSTOMER_PHONE,
        from_=TWILIO_NUMBER,
        url=f"{SERVER_URL}/call-twiml/{table_number}",
    )
    print(f"[CALL] Dialing {CUSTOMER_PHONE} for Table {table_number}")
    return {"status": "ok", "table": table_number}


@app.api_route("/call-twiml/{table_number}", methods=["GET", "POST"])
async def call_twiml(table_number: int):
    resp = VoiceResponse()
    connect = Connect()
    ws_url = SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
    connect.stream(url=f"{ws_url}/media-stream/{table_number}")
    resp.append(connect)
    return Response(content=str(resp), media_type="application/xml")


@app.get("/api/tables")
def get_tables():
    conn = get_db()
    tables = conn.execute("SELECT * FROM tables").fetchall()
    conn.close()
    return [dict(t) for t in tables]


@app.get("/api/tables/{table_id}")
def get_table_details(table_id: int):
    conn = get_db()
    orders = conn.execute("SELECT * FROM orders WHERE table_id = ?", (table_id,)).fetchall()
    transcript = conn.execute("SELECT text FROM transcripts WHERE table_id = ? ORDER BY id DESC LIMIT 1", (table_id,)).fetchone()
    conn.close()
    return {
        "orders": [dict(o) for o in orders],
        "transcript": transcript["text"] if transcript else None
    }


@app.post("/api/tables/{table_id}/checkout")
def checkout_table(table_id: int):
    conn = get_db()
    conn.execute("DELETE FROM orders WHERE table_id = ?", (table_id,))
    conn.execute("DELETE FROM transcripts WHERE table_id = ?", (table_id,))
    conn.execute("UPDATE tables SET status = 'Available' WHERE id = ?", (table_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. WebSocket — real-time audio bridge
#    Twilio ↔ Deepgram STT → Groq LLM → Deepgram TTS → Twilio
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/media-stream/{table_number}")
async def media_stream(ws: WebSocket, table_number: int):
    await ws.accept()
    print(f"[WS] Call connected for Table {table_number}")

    dynamic_prompt = SYSTEM_PROMPT + f"\n\nIMPORTANT: You are currently serving Table {table_number}."
    conversation     = [{"role": "system", "content": dynamic_prompt}]
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
                    max_tokens=250,
                    temperature=0.7,
                ).choices[0].message.content.strip()

                should_hangup = "[HANGUP]" in reply
                if should_hangup:
                    reply = reply.replace("[HANGUP]", "").strip()

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
                    
                    if should_hangup:
                        print("[*] Agent requested hangup. Waiting for audio to finish playing...")
                        await asyncio.sleep((len(audio) / 8000.0) + 1.0)
                        await ws.close()
                        break

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
                opening = f"Welcome to The Grand Azure! I'm your AI waiter for Table {table_number}. Ready to order?"
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

        # Save the conversation transcript and extract orders
        if stream_sid and len(conversation) > 1:
            transcript_text = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in conversation if m['role'] != 'system'])
            
            # Extract orders using Groq
            extraction_prompt = f"""You are a JSON extractor. Analyze this restaurant conversation.
Extract a strict JSON array of items the customer ordered. Do not include items they didn't explicitly order.
Example format: [{{"item": "Masala Dosa", "qty": 2}}]
If nothing was ordered, return [].
Do NOT wrap in markdown blocks, return ONLY raw JSON.

Transcript:
{transcript_text}"""
            
            try:
                extract_res = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": extraction_prompt}],
                    temperature=0.0
                ).choices[0].message.content.strip()
                
                # Strip markdown code blocks if the LLM still includes them
                if extract_res.startswith("```"):
                    extract_res = extract_res.split("```")[1].replace("json\n", "").strip()
                
                orders = json.loads(extract_res)
                
                conn = get_db()
                c = conn.cursor()
                c.execute("INSERT INTO transcripts (table_id, text) VALUES (?, ?)", (table_number, transcript_text))
                
                menu_keys = list(MENU.keys())
                for o in orders:
                    item = o.get("item")
                    qty = o.get("qty", 1)
                    if item:
                        match = get_best_match(item, menu_keys)
                        if match:
                            price = MENU[match]
                            c.execute("INSERT INTO orders (table_id, item_name, quantity, price) VALUES (?, ?, ?, ?)",
                                      (table_number, match, qty, price))
                conn.commit()
                conn.close()
                print(f"[*] Extraction complete for Table {table_number}")
            except Exception as e:
                print(f"[EXTRACTION ERROR] {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Deepgram TTS — Aura Asteria (most human-sounding)
# ─────────────────────────────────────────────────────────────────────────────
async def deepgram_tts(text: str) -> bytes | None:
    url = "https://api.deepgram.com/v1/speak"
    params = {
        "model": "aura-stella-en", # Stella speaks slightly slower and clearer
        "encoding": "mulaw",       # Twilio requires mulaw (8kHz limit)
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
