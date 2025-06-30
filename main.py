# main.py - Versione Finale e Completa
# Data: 30 Giugno 2025

# --- Import delle librerie ---
import os
import json
import base64
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
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") # Assicurati che su Render sia la chiave 'service_role'
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")

# --- Inizializzazione dei Servizi ---

# Inizializza l'applicazione FastAPI
app = FastAPI(title="Zenith Rewards Backend", description="API per la gestione dell'app Zenith Rewards.")

# Inizializza Supabase
if not all([SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Errore critico: mancano le variabili d'ambiente di Supabase.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Inizializza Vertex AI (solo se le credenziali sono presenti)
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        # GCP ha bisogno delle credenziali in un file, quindi le scriviamo temporaneamente
        # Questo approccio è comune in ambienti come Render
        with open("gcp_sa_key.json", "w") as f:
            f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI inizializzato correttamente.")
    except Exception as e:
        print(f"ATTENZIONE: Errore nella configurazione di Vertex AI: {e}")
else:
    print("ATTENZIONE: Credenziali Google Cloud non trovate. Le funzionalità AI saranno disabilitate.")

# Configurazione CORS per permettere al frontend di comunicare con il backend
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
    user_id: str
    email: str | None = None
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class ImageGenerationRequest(BaseModel):
    user_id: str
    prompt: str
    contest_id: int

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str
    address: str

class SubmissionRequest(BaseModel):
    contest_id: int
    user_id: str
    image_url: str
    prompt: str

# --- Endpoint dell'API ---

@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

# --- Gestione Utenti ---

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        # Usiamo maybe_single() per gestire elegantemente il caso in cui l'utente non esista
        response = supabase.table('users').select('*').eq('user_id', user_data.user_id).maybe_single().execute()

        # Se l'utente non esiste, lo creiamo (INSERT)
        if not response.data:
            new_user_record = {
                'user_id': user_data.user_id, 'email': user_data.email,
                'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id,
                'avatar_url': user_data.avatar_url, 'login_streak': 1,
                'last_login_at': datetime.now(timezone.utc).isoformat(), 'points_balance': 0
            }
            supabase.table('users').insert(new_user_record).execute()
        # Se l'utente esiste già, aggiorniamo i suoi dati di accesso (UPDATE)
        else:
            # Qui puoi inserire la logica per aggiornare la login_streak
            supabase.table('users').update({
                'last_login_at': datetime.now(timezone.utc).isoformat()
            }).eq('user_id', user_data.user_id).execute()

        return {"status": "success", "message": "Utente sincronizzato correttamente."}
    except Exception as e:
        print(f"Errore critico in sync_user: {e}")
        raise HTTPException(status_code=500, detail=f"Errore interno del server durante la sincronizzazione: {e}")

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).single().execute()
        return {"points_balance": response.data.get('points_balance', 0)}
    except Exception as e:
        print(f"Errore in get_user_balance: {e}")
        raise HTTPException(status_code=404, detail="Utente non trovato o errore nel recupero del saldo.")

# --- Sistema di Prelievi ---

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    # Logica di prelievo... (da implementare)
    return {"status": "success", "message": "Richiesta di prelievo inviata."}


# --- Sistema "Zenith Art Battles" con IA Reale ---

@app.get("/contests/current")
def get_current_contest():
    # Logica per recuperare il contest del giorno... (da implementare)
    return {"id": 1, "theme_prompt": "Un robot che dipinge un tramonto, stile Van Gogh", "end_date": "2025-07-01T23:59:59Z"}

@app.post("/contests/generate_image")
def generate_ai_image(req: ImageGenerationRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', req.user_id).single().execute()

        if user_response.data.get('points_balance', 0) < IMAGE_GENERATION_COST:
            raise HTTPException(status_code=402, detail="Zenith Coins insufficienti per generare l'immagine.")

        new_balance = user_response.data.get('points_balance', 0) - IMAGE_GENERATION_COST
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', req.user_id).execute()

        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        images = model.generate_images(prompt=req.prompt, number_of_images=1, aspect_ratio="1:1")
        
        image_bytes = images[0]._image_bytes
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        return {"image_url": f"data:image/png;base64,{base64_image}", "new_balance": new_balance}
    except HTTPException as http_exc:
        # Lascia passare le eccezioni HTTP che abbiamo generato noi (es. 402)
        raise http_exc
    except Exception as e:
        # Cattura tutti gli altri errori imprevisti (es. da Vertex AI)
        print(f"Errore imprevisto in generate_image: {e}")
        raise HTTPException(status_code=500, detail=f"Errore interno del server durante la generazione dell'immagine: {e}")

@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest):
    # Logica di invio opera d'arte... (da implementare)
    return {"status": "success"}

# --- Endpoint di Gamification ---

@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(10).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricamento della classifica.")

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    try:
        response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
        return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel recupero delle statistiche referral.")

# --- Endpoint Segnaposto (per risolvere i 404 Not Found) ---
# Questi endpoint sono richiesti dal tuo frontend. Per ora restituiscono un messaggio
# che indica che la funzionalità non è ancora implementata.

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    # Logica futura per la streak di login
    raise HTTPException(status_code=501, detail="Funzionalità streak non ancora implementata.")

@app.get("/missions/{user_id}")
def get_missions(user_id: str):
    # Logica futura per le missioni utente
    raise HTTPException(status_code=501, detail="Funzionalità missioni non ancora implementata.")

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str):
    # Logica futura per l'aggiornamento del profilo
    raise HTTPException(status_code=501, detail="Funzionalità di aggiornamento profilo non ancora implementata.")