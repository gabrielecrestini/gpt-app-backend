# main.py - Versione Finale con Endpoint di Debug per la Connessione
import os, json, base64, time, sys
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import vertexai
from vertexai.generative_models import GenerativeModel
from vertexai.preview.vision_models import ImageGenerationModel

load_dotenv()
SUPABASE_URL, SUPABASE_KEY = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR = os.environ.get("GCP_PROJECT_ID"), os.environ.get("GCP_REGION"), os.environ.get("GCP_SA_KEY_JSON_STR")
app = FastAPI(title="Zenith Rewards Backend")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    except Exception as e: print(f"ATTENZIONE: Errore config Vertex AI: {e}")
else: print("ATTENZIONE: Credenziali Google Cloud non trovate.")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

IMAGE_GENERATION_COST, POINTS_TO_EUR_RATE = 50, 1000.0
class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class ImageGenerationRequest(BaseModel): user_id: str; prompt: str; contest_id: int
class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class SubmissionRequest(BaseModel): contest_id: int; user_id: str; image_url: str; prompt: str

@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = supabase.table('users').select('last_login_at, login_streak').eq('user_id', user_data.user_id).maybe_single().execute()
            if response:
                now = datetime.now(timezone.utc)
                if not response.data:
                    new_user_record = {'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1, 'last_login_at': now.isoformat(), 'points_balance': 0}
                    supabase.table('users').insert(new_user_record).execute()
                else:
                    user, last_login_str, new_streak = response.data, response.data.get('last_login_at'), response.data.get('login_streak', 1)
                    if last_login_str:
                        days_diff = (now.date() - datetime.fromisoformat(last_login_str).date()).days
                        if days_diff == 1: new_streak += 1
                        elif days_diff > 1: new_streak = 1
                    supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': new_streak}).eq('user_id', user_data.user_id).execute()
                return {"status": "success"}
            print(f"Tentativo sync {attempt + 1} fallito: risposta nulla. Riprovo...")
        except Exception as e: print(f"Errore sync (tentativo {attempt + 1}): {e}")
        if attempt < max_retries - 1: time.sleep(1)
    raise HTTPException(status_code=500, detail="Impossibile sincronizzare l'utente dopo diversi tentativi.")

# ... (tutti gli altri endpoint come get_user_balance, ecc. sono qui)

@app.get("/test_connection")
def test_connection_endpoint():
    log_output = []
    log_output.append("--- Inizio Test di Connessione a Supabase ---")
    URL, KEY = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not all([URL, KEY]):
        log_output.append("ERRORE FATALE: Variabili SUPABASE_URL o SUPABASE_KEY non trovate!")
        raise HTTPException(status_code=500, detail={"log": log_output})
    log_output.append(f"[FASE 1] URL Trovato: {URL[:25]}...")
    log_output.append(f"[FASE 1] Key Trovata: {KEY[:5]}...")
    try:
        log_output.append("[FASE 2] Provo a creare un nuovo client Supabase per il test...")
        test_client: Client = create_client(URL, KEY)
        log_output.append("==> Client di test creato con successo.")
        log_output.append("[FASE 3] Provo a eseguire una query semplice...")
        response = test_client.table('users').select('user_id').limit(1).execute()
        log_output.append("==> Query eseguita.")
        log_output.append("[FASE 4] Analisi risposta...")
        if response:
            log_output.append("RISULTATO: Risposta ricevuta dal database!")
            log_output.append(f"   Contenuto di response.data: {str(response.data)}")
            log_output.append("CONCLUSIONE: La connessione e le credenziali sembrano FUNZIONARE.")
        else:
            log_output.append("RISULTATO: ERRORE CRITICO! La risposta è 'None'.")
            log_output.append("CONCLUSIONE: Problema fondamentale di connessione/credenziali o timeout.")
    except Exception as e:
        log_output.append(f"ERRORE CATTURATO DURANTE IL TEST: {str(e)}")
        log_output.append("CONCLUSIONE: Test fallito a causa di un'eccezione.")
    log_output.append("--- Fine Test ---")
    return {"log": log_output}