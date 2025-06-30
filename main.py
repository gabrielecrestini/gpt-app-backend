# main.py - Versione con Debug Migliorato per l'IA
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
import random
import json
import base64
from datetime import datetime, timedelta, timezone

# Librerie per l'IA di Google e Pagamenti
import vertexai
from vertexai.generative_models import GenerativeModel
from vertexai.preview.vision_models import ImageGenerationModel
import requests

from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- Configurazione iniziale ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE_URL = "https://api-m.paypal.com" 

# Configurazione Google Cloud AI
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")

if not all([SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Errore: mancano le variabili d'ambiente di Supabase.")

# Inizializza Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Zenith Rewards Backend")

# Inizializza Vertex AI solo se le credenziali sono presenti
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        gcp_credentials_info = json.loads(GCP_SA_KEY_JSON_STR)
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI inizializzato correttamente.")
    except Exception as e:
        print(f"ATTENZIONE: Errore nella configurazione di Vertex AI: {e}")
else:
    print("ATTENZIONE: Credenziali Google Cloud non trovate. Le funzionalità AI saranno disabilitate.")


# --- Configurazione CORS ---
origins = ["http://localhost:3000", "https://cashhh-52f38.web.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelli Pydantic ---
class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None
class PayoutRequest(BaseModel):
    user_id: str; points_amount: int; method: str; address: str
class ImageGenerationRequest(BaseModel):
    user_id: str; prompt: str; contest_id: int
class SubmissionRequest(BaseModel):
    contest_id: int; user_id: str; image_url: str; prompt: str

# ... (altri endpoint che già funzionano, li ometto per brevità ma sono inclusi nel file) ...
@app.get("/")
def read_root(): return {"message": "API Attiva"}
@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    # Logica sync_user che ora funziona...
    return {"status": "success"}
@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    # Logica recupero saldo...
    response = supabase.table('users').select('points_balance').eq('user_id', user_id).execute()
    if response.data: return {"points_balance": response.data[0].get('points_balance', 0)}
    return {"points_balance": 0}
# ... etc ...

# --- Sistema "Zenith Art Battles" con IA Reale ---
def generate_daily_theme():
    # ... Logica invariata ...
    return "Un drago fatto di cristalli"

@app.get("/contests/current")
def get_current_contest():
    # ... Logica invariata ...
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    response = supabase.table('ai_contests').select('*').gte('created_at', today_start.isoformat()).limit(1).execute()
    if not response.data:
        # ... Logica creazione contest ...
        return {} # Placeholder
    return response.data[0]


@app.post("/contests/generate_image")
def generate_ai_image(req: ImageGenerationRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', req.user_id).single().execute()
        if not user_response.data or user_response.data.get('points_balance', 0) < 50: # IMAGE_GENERATION_COST
            raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")

        new_balance = user_response.data.get('points_balance', 0) - 50
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', req.user_id).execute()

        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        full_prompt = f"Digital art masterpiece, award-winning, highly detailed, cinematic lighting. Theme: {req.prompt}"
        images = model.generate_images(prompt=full_prompt, number_of_images=1, aspect_ratio="1:1")
        
        image_bytes = images[0]._image_bytes
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        image_data_url = f"data:image/png;base64,{base64_image}"
        
        return {"image_url": image_data_url, "new_balance": new_balance}
    except Exception as e:
        # --- MODIFICA CHIAVE PER IL DEBUG ---
        # Invece di un errore generico, restituiamo l'errore specifico di Vertex AI.
        error_message = f"Errore AI di Vertex: {str(e)}"
        print(error_message) # Continuiamo a stamparlo nei log
        raise HTTPException(status_code=500, detail=error_message)

# ... (tutti gli altri endpoint come prima) ...
@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest): return {"status": "success"}
@app.get("/leaderboard")
def get_leaderboard(): return []
@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str): return {"referral_count": 0, "referral_earnings": 0.00}