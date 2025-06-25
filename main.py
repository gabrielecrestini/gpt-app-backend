# main.py - Versione 3 (con User Sync e CORS)
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client, Client
import os
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# Caricamento e configurazione (invariato)
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Errore: devi impostare SUPABASE_URL e SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Freecash Clone Backend")

# Configurazione CORS (invariato)
origins = [
    "http://localhost:3000",
    "https://cashhh-52f38.web.app", # Aggiunto il tuo URL di produzione
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelli Dati ---
class UserSyncRequest(BaseModel):
    user_id: str
    email: str

# --- Endpoint ---
@app.get("/")
def read_root():
    return {"message": "Benvenuto nel backend! API v3 con sync utente attiva."}

# NUOVO ENDPOINT PER SINCRONIZZARE L'UTENTE
@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    """
    Questo endpoint riceve i dati dell'utente dal frontend dopo il login.
    Controlla se l'utente esiste nel nostro database. Se non esiste, lo crea.
    Questa operazione si chiama 'UPSERT' (update or insert).
    """
    try:
        # Usiamo 'upsert' per inserire o aggiornare il record.
        # 'on_conflict' dice a Supabase di non fare nulla se un utente con lo stesso 'user_id' esiste già.
        data, count = supabase.table('users').upsert(
            {
                'user_id': user_data.user_id, 
                'email': user_data.email,
                # 'balance' non è specificato qui, quindi userà il valore di default '0' del database.
            },
            on_conflict='user_id'
        ).execute()
        return {"status": "success", "message": "User synchronized successfully"}
    except Exception as e:
        print(f"Errore durante la sincronizzazione: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        # Ora l'utente dovrebbe esistere sempre grazie a sync_user
        response = supabase.table('users').select('balance').eq('user_id', user_id).single().execute()
        if response.data:
            return {"user_id": user_id, "balance": response.data['balance']}
        else:
            # Questa condizione ora dovrebbe essere molto rara
            raise HTTPException(status_code=404, detail="Utente non trovato, la sincronizzazione potrebbe essere fallita.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# L'endpoint postback rimane invariato
@app.get("/postback/adgate")
async def adgate_postback(request: Request):
    # ... (codice del postback invariato) ...
    pass
