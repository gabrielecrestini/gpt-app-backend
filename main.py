# main.py
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client, Client
import os
from dotenv import load_dotenv

# Carica le variabili d'ambiente da un file .env
# Crea un file .env e mettici dentro SUPABASE_URL e SUPABASE_KEY
load_dotenv()

# --- Configurazione Iniziale ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Controlla se le chiavi sono state impostate
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Errore: devi impostare SUPABASE_URL e SUPABASE_KEY nel tuo file .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Freecash Clone Backend")

# --- Modelli Dati (Pydantic) ---
# Modello per rappresentare un utente nel nostro database
class User(BaseModel):
    id: int
    user_id: str  # ID univoco da Firebase Auth
    email: str
    balance: float = Field(default=0.0)

# --- Endpoint API ---
@app.get("/")
def read_root():
    """Endpoint di benvenuto per testare se il server è attivo."""
    return {"message": "Benvenuto nel backend della tua app GPT!"}

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    """Recupera il saldo di un utente specifico."""
    try:
        response = supabase.table('users').select('balance').eq('user_id', user_id).single().execute()
        if response.data:
            return {"user_id": user_id, "balance": response.data['balance']}
        else:
            # Se l'utente non esiste, lo creiamo con saldo 0
            # Nota: la creazione vera e propria avverrà alla registrazione
            raise HTTPException(status_code=404, detail="Utente non trovato")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Endpoint Postback per Offerwall (AdGate, CPALead, etc.) ---
@app.get("/postback/adgate")
async def adgate_postback(request: Request):
    """
    Riceve la notifica (postback) da AdGate quando un utente completa un'offerta.
    Esempio di URL che AdGate chiamerà:
    https://tuo-dominio.onrender.com/postback/adgate?user_id={USER_ID}&amount={AMOUNT}&offer_id={OFFER_ID}
    """
    params = request.query_params
    user_id = params.get("user_id")
    amount_str = params.get("amount") # L'importo che l'utente ha guadagnato

    if not user_id or not amount_str:
        raise HTTPException(status_code=400, detail="Parametri 'user_id' e 'amount' mancanti")

    try:
        amount = float(amount_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Il parametro 'amount' deve essere un numero")

    print(f"✅ Postback ricevuto: Utente {user_id} ha guadagnato ${amount}")

    try:
        # 1. Recupera il saldo attuale dell'utente
        user_data = supabase.table('users').select('balance').eq('user_id', user_id).single().execute()

        if not user_data.data:
            raise HTTPException(status_code=404, detail=f"Utente {user_id} non trovato nel database.")

        current_balance = user_data.data['balance']
        new_balance = current_balance + amount

        # 2. Aggiorna il saldo dell'utente nel database
        updated_user = supabase.table('users').update({'balance': new_balance}).eq('user_id', user_id).execute()

        if not updated_user.data:
             raise HTTPException(status_code=500, detail="Errore durante l'aggiornamento del saldo.")

        print(f"💰 Saldo aggiornato per {user_id}: da ${current_balance} a ${new_balance}")

        return {"status": "success", "user_id": user_id, "new_balance": new_balance}

    except Exception as e:
        # Stampa l'errore per il debug
        print(f"❌ Errore durante l'elaborazione del postback: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Errore interno del server: {str(e)}")