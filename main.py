# main.py - Versione Definitiva, Stabile e Completa
# Data: 1 Luglio 2025

# --- Import delle librerie ---
import os
import json
import base64
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# Librerie per l'Intelligenza Artificiale di Google
import vertexai
from vertexai.generative_models import GenerativeModel
from vertexai.preview.vision_models import ImageGenerationModel

# --- Configurazione Iniziale ---
load_dotenv()

# Caricamento delle variabili d'ambiente
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")

# --- Inizializzazione dei Servizi ---
app = FastAPI(title="Zenith Rewards Backend", description="API per la gestione dell'app Zenith Rewards.")

if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI inizializzato correttamente.")
    except Exception as e:
        print(f"ATTENZIONE: Errore nella configurazione di Vertex AI: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Costanti e Modelli Dati (Pydantic) ---
IMAGE_GENERATION_COST = 50
POINTS_TO_EUR_RATE = 1000.0

class UserSyncRequest(BaseModel):
    user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class ImageGenerationRequest(BaseModel):
    user_id: str; prompt: str; contest_id: int
class PayoutRequest(BaseModel):
    user_id: str; points_amount: int; method: str; address: str
class SubmissionRequest(BaseModel):
    contest_id: int; user_id: str; image_url: str; prompt: str
class PurchaseRequest(BaseModel):
    user_id: str; item_id: int
class UserProfileUpdate(BaseModel):
    displayName: str | None = None; avatar_url: str | None = None

# --- Funzioni Helper ---
def get_supabase_client() -> Client:
    """Crea e restituisce un client Supabase nuovo e pulito per ogni richiesta."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not all([url, key]): raise ValueError("Variabili Supabase non impostate.")
    return create_client(url, key)

def generate_daily_theme() -> str:
    """Usa Vertex AI (Gemini) per generare un tema artistico creativo e breve."""
    try:
        model = GenerativeModel("gemini-1.0-pro")
        prompt = "Genera un tema artistico breve, creativo, surreale e stimolante (massimo 10 parole) per una competizione di arte digitale. Fornisci solo il testo del tema, senza virgolette o prefissi."
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Errore generazione tema AI: {e}"); return "Una balena meccanica che nuota tra le nuvole."

# --- Endpoint dell'API ---

@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('last_login_at, login_streak').eq('user_id', user_data.user_id).execute()
        if not response: raise Exception("CRITICO: Risposta nulla dal database.")
        now = datetime.now(timezone.utc)
        if not response.data or len(response.data) == 0:
            new_user_record = {'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1, 'last_login_at': now.isoformat(), 'points_balance': 0}
            supabase.table('users').insert(new_user_record).execute()
        else:
            user = response.data[0]
            last_login_str, new_streak = user.get('last_login_at'), user.get('login_streak', 1)
            if last_login_str:
                days_diff = (now.date() - datetime.fromisoformat(last_login_str).date()).days
                if days_diff == 1: new_streak += 1
                elif days_diff > 1: new_streak = 1
            supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': new_streak}).eq('user_id', user_data.user_id).execute()
        return {"status": "success"}
    except Exception as e: print(f"Errore in sync_user: {e}"); raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    try:
        supabase = get_supabase_client()
        update_payload = {}
        if profile_data.displayName is not None: update_payload['display_name'] = profile_data.displayName
        if profile_data.avatar_url is not None: update_payload['avatar_url'] = profile_data.avatar_url
        if not update_payload: raise HTTPException(status_code=400, detail="Nessun dato fornito per l'aggiornamento.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profilo aggiornato con successo."}
    except Exception as e: print(f"Errore in update_profile: {e}"); raise HTTPException(status_code=500, detail="Errore durante l'aggiornamento del profilo.")

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.rpc('request_payout_function', { 'p_user_id': payout_data.user_id, 'p_points_amount': payout_data.points_amount, 'p_value_in_eur': value_eur, 'p_method': payout_data.method, 'p_address': payout_data.address }).execute()
        return {"status": "success", "message": "La tua richiesta di prelievo è stata inviata!"}
    except Exception as e:
        if 'Punti insufficienti' in str(e): raise HTTPException(status_code=402, detail="Punti insufficienti per effettuare questo prelievo.")
        print(f"Errore in request_payout: {e}"); raise HTTPException(status_code=500, detail="Errore durante la richiesta.")

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response or not response.data: raise HTTPException(status_code=404, detail=f"Utente {user_id} non trovato.")
        return {"points_balance": response.data.get('points_balance', 0)}
    except HTTPException as http_exc: raise http_exc
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('login_streak, last_streak_claim_at').eq('user_id', user_id).maybe_single().execute()
        if not response or not response.data: return {"days": 0, "canClaim": False}
        user, can_claim = response.data, True
        if user.get('last_streak_claim_at'):
            if datetime.fromisoformat(user.get('last_streak_claim_at')).date() == datetime.now(timezone.utc).date(): can_claim = False
        return {"days": user.get('login_streak', 0), "canClaim": can_claim}
    except Exception as e: print(f"Errore in get_streak_status: {e}"); raise HTTPException(status_code=500, detail=str(e))

@app.post("/streak/claim/{user_id}")
def claim_streak_bonus(user_id: str):
    try:
        status_response = get_streak_status(user_id=user_id)
        if not status_response.get("canClaim"): raise HTTPException(status_code=400, detail="Bonus giornaliero già riscosso.")
        reward = min(status_response.get("days", 0) * 10, 100)
        supabase = get_supabase_client()
        user_res = supabase.table('users').select('points_balance').eq('user_id', user_id).single().execute()
        new_balance = user_res.data.get('points_balance', 0) + reward
        supabase.table('users').update({'points_balance': new_balance, 'last_streak_claim_at': datetime.now(timezone.utc).isoformat()}).eq('user_id', user_id).execute()
        return {"status": "success", "message": f"Hai riscattato {reward} Zenith Coins!", "new_balance": new_balance}
    except HTTPException as http_exc: raise http_exc
    except Exception as e: print(f"Errore in claim_streak_bonus: {e}"); raise HTTPException(status_code=500, detail="Errore durante la riscossione.")

@app.get("/leaderboard")
def get_leaderboard():
    supabase = get_supabase_client()
    response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(10).execute()
    return [{"name": u.get('display_name', 'N/A'), "points_balance": u.get('points_balance', 0), "avatar": u.get('avatar_url', ''), "earnings": u.get('points_balance', 0) / POINTS_TO_EUR_RATE} for u in response.data]

@app.get("/shop/items")
def get_shop_items():
    supabase = get_supabase_client()
    response = supabase.table("shop_items").select("*").eq("is_active", True).order("price").execute()
    return response.data

@app.post("/shop/buy")
def buy_shop_item(req: PurchaseRequest):
    try:
        supabase = get_supabase_client()
        supabase.rpc('purchase_item', {'p_user_id': req.user_id, 'p_item_id': req.item_id}).execute()
        return {"status": "success", "message": "Acquisto completato!"}
    except Exception as e:
        if 'Fondi insufficienti' in str(e): raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")
        print(f"Errore acquisto: {e}"); raise HTTPException(status_code=500, detail="Errore durante l'acquisto.")

@app.get("/contests/current")
def get_current_contest():
    try:
        supabase = get_supabase_client()
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        response = supabase.table('ai_contests').select('*').gte('created_at', today_start.isoformat()).order('id', desc=True).limit(1).execute()
        if response.data:
            return response.data[0]
        new_theme = generate_daily_theme()
        new_contest_data = {"theme_prompt": new_theme, "start_date": today_start.isoformat(), "end_date": (today_start + timedelta(days=1)).isoformat(), "status": "active", "prize_pool": 10000}
        insert_response = supabase.table('ai_contests').insert(new_contest_data).execute()
        new_contest_query = supabase.table('ai_contests').select('*').eq('theme_prompt', new_theme).order('id', desc=True).limit(1).execute()
        if not new_contest_query.data:
            raise Exception("Fallimento nel recuperare il contest appena creato.")
        return new_contest_query.data[0]
    except Exception as e:
        print(f"ERRORE CRITICO in get_current_contest: {e}")
        raise HTTPException(status_code=500, detail="Impossibile caricare o creare il contest del giorno.")

@app.post("/contests/generate_image")
def generate_ai_image(req: ImageGenerationRequest):
    try:
        supabase = get_supabase_client()
        user_response = supabase.table('users').select('points_balance').eq('user_id', req.user_id).maybe_single().execute()
        if not user_response or not user_response.data: raise HTTPException(status_code=404, detail="Utente non trovato.")
        if user_response.data.get('points_balance', 0) < IMAGE_GENERATION_COST: raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")
        new_balance = user_response.data.get('points_balance', 0) - IMAGE_GENERATION_COST
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', req.user_id).execute()
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        images = model.generate_images(prompt=req.prompt, number_of_images=1, aspect_ratio="1:1")
        base64_image = base64.b64encode(images[0]._image_bytes).decode('utf-8')
        return {"image_url": f"data:image/png;base64,{base64_image}", "new_balance": new_balance}
    except HTTPException as http_exc: raise http_exc
    except Exception as e: print(f"Errore in generate_image: {e}"); raise HTTPException(status_code=500, detail="Errore interno.")

@app.get("/contests/{contest_id}/submissions")
def get_contest_submissions(contest_id: int):
    supabase = get_supabase_client()
    response = supabase.table("ai_submissions").select("*, user:users(display_name, avatar_url)").eq("contest_id", contest_id).order("votes", desc=True).execute()
    return response.data

@app.post("/submissions/{submission_id}/vote")
def vote_for_submission(submission_id: int):
    supabase = get_supabase_client()
    supabase.rpc('increment_votes', {'submission_id_in': submission_id}).execute()
    return {"status": "success"}
    
@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    supabase = get_supabase_client()
    response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
    return {"referral_count": response.count or 0, "referral_earnings": 0.00}

@app.get("/missions/{user_id}")
def get_missions(user_id: str): raise HTTPException(status_code=501, detail="Non implementato.")
@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest): raise HTTPException(status_code=501, detail="Non implementato.")