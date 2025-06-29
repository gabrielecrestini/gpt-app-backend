# main.py - Versione Finale Definitiva (con IA Reale)
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
PAYPAL_API_BASE_URL = "https://api-m.paypal.com"  # O "https://api-m.sandbox.paypal.com" per test

# Configurazione Google Cloud AI
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")

if not all([SUPABASE_URL, SUPABASE_KEY, GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    raise ValueError("Errore: mancano le variabili d'ambiente di Supabase o Google Cloud.")

try:
    gcp_credentials_info = json.loads(GCP_SA_KEY_JSON_STR)
    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
except Exception as e:
    print(f"ATTENZIONE: Errore nella configurazione delle credenziali Google Cloud: {e}")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Zenith Rewards Backend")

# --- Configurazione CORS ---
origins = ["http://localhost:3000", "https://cashhh-52f38.web.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Costanti e Modelli ---
POINTS_TO_EUR_RATE = 1000.0
IMAGE_GENERATION_COST = 50

class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None

class ProfileUpdateRequest(BaseModel):
    display_name: str; avatar_url: str

class PayoutRequest(BaseModel):
    user_id: str; points_amount: int; method: str; address: str

class ImageGenerationRequest(BaseModel):
    user_id: str; prompt: str; contest_id: int

class SubmissionRequest(BaseModel):
    contest_id: int; user_id: str; image_url: str; prompt: str

# --- Endpoint di Base ---
@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

# --- Gestione Utenti ---
@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        user_res = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).execute()
        now = datetime.now(timezone.utc)
        
        if not user_res.data: # Nuovo utente
            user_record = { 
                'user_id': user_data.user_id, 'email': user_data.email, 
                'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 
                'avatar_url': user_data.avatar_url, 'login_streak': 1,
                'last_login_at': now.isoformat(), 'points_balance': 0
            }
            supabase.table('users').insert(user_record).execute()
        else: # Utente esistente, aggiorna streak
            user = user_res.data[0]
            last_login_str = user.get('last_login_at')
            streak = user.get('login_streak', 0)
            if last_login_str:
                last_login = datetime.fromisoformat(last_login_str)
                if (now.date() - last_login.date()).days == 1:
                    streak += 1
                elif (now.date() - last_login.date()).days > 1:
                    streak = 1
            else:
                streak = 1
            
            supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': streak}).eq('user_id', user_data.user_id).execute()
            
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).execute()
        if response.data:
            return {"points_balance": response.data[0].get('points_balance', 0)}
        return {"points_balance": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel recupero del saldo.")

# --- Sistema di Prelievi con PayPal API ---
def process_paypal_payout(payout_id: int, user_email: str, value_eur: float):
    try:
        auth_response = requests.post(f"{PAYPAL_API_BASE_URL}/v1/oauth2/token", auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET), headers={"Accept": "application/json", "Accept-Language": "en_US"}, data={"grant_type": "client_credentials"})
        auth_response.raise_for_status()
        access_token = auth_response.json()["access_token"]
        payout_data = {
            "sender_batch_header": {"sender_batch_id": f"Zenith_{payout_id}_{int(datetime.now().timestamp())}", "email_subject": "Hai ricevuto un pagamento da Zenith Rewards!", "email_message": f"Grazie per aver usato la nostra piattaforma! Ecco il tuo premio di {value_eur:.2f} EUR."},
            "items": [{"recipient_type": "EMAIL", "amount": {"value": f"{value_eur:.2f}", "currency": "EUR"}, "receiver": user_email}]
        }
        payout_response = requests.post(f"{PAYPAL_API_BASE_URL}/v1/payments/payouts", headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}, json=payout_data)
        payout_response.raise_for_status()
        supabase.table('payout_requests').update({'status': 'completed'}).eq('id', payout_id).execute()
        return True, payout_response.json()
    except Exception as e:
        supabase.table('payout_requests').update({'status': 'failed'}).eq('id', payout_id).execute()
        return False, str(e)

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', payout_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('points_balance', 0) < payout_data.points_amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti.")
        
        new_balance = user_response.data.get('points_balance', 0) - payout_data.points_amount
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', payout_data.user_id).execute()
        
        value_in_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        insert_res = supabase.table('payout_requests').insert({
            'user_id': payout_data.user_id, 'points_amount': payout_data.points_amount,
            'value_in_eur': value_in_eur, 'payout_method': payout_data.method,
            'wallet_address': payout_data.address, 'status': 'processing'
        }).execute()

        if payout_data.method == 'paypal' and PAYPAL_CLIENT_ID:
            payout_id = insert_res.data[0]['id']
            process_paypal_payout(payout_id, payout_data.address, value_in_eur)
            
        return {"status": "success", "message": "Richiesta di prelievo inviata."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'elaborazione della richiesta.")

# --- Sistema "Zenith Art Battles" con IA Reale ---
def generate_daily_theme():
    try:
        model = GenerativeModel("gemini-1.0-pro")
        prompt = "Genera un tema artistico breve, creativo e stimolante per una competizione di arte digitale. Fornisci solo il testo del tema, senza virgolette o prefissi."
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return "Un drago fatto di cristalli"

@app.get("/contests/current")
def get_current_contest():
    try:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        response = supabase.table('ai_contests').select('*').gte('created_at', today_start.isoformat()).limit(1).execute()
        if not response.data:
            new_theme = generate_daily_theme()
            end_date = today_start + timedelta(days=1)
            insert_res = supabase.table('ai_contests').insert({
                "theme_prompt": new_theme, "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(), "status": "active", "prize_pool": 10000
            }).execute()
            return insert_res.data[0]
        return response.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail="Impossibile recuperare il contest.")

@app.post("/contests/generate_image")
async def generate_ai_image(req: ImageGenerationRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', req.user_id).single().execute()
        if not user_response.data or user_response.data.get('points_balance', 0) < IMAGE_GENERATION_COST:
            raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")

        new_balance = user_response.data.get('points_balance', 0) - IMAGE_GENERATION_COST
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', req.user_id).execute()

        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        full_prompt = f"Digital art masterpiece, award-winning, highly detailed, cinematic lighting. Theme: {req.prompt}"
        images = model.generate_images(prompt=full_prompt, number_of_images=1, aspect_ratio="1:1")
        
        image_bytes = images[0]._image_bytes
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        image_data_url = f"data:image/png;base64,{base64_image}"
        
        return {"image_url": image_data_url, "new_balance": new_balance}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore durante la generazione dell'immagine.")

@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest):
    try:
        supabase.table('ai_submissions').insert({
            "contest_id": req.contest_id, "user_id": req.user_id,
            "image_url": req.image_url, "prompt": req.prompt, "votes": 0
        }).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'invio dell'opera.")

# --- Altri Endpoint (Gamification, Postback, etc.) ---
@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(5).execute()
        leaderboard_data = [{"name": u.get('display_name', 'N/A'), "earnings": u.get('points_balance', 0)/POINTS_TO_EUR_RATE, "avatar": u.get('avatar_url', '')} for u in response.data]
        return leaderboard_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    try:
        response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
        return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
