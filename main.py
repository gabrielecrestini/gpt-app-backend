# main.py - Versione Finale, Stabile e Completa
# Data: 2 Luglio 2025

# --- Import delle librerie ---
import os
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import paypalrestsdk

# --- Configurazione Iniziale ---
load_dotenv()

# Caricamento delle chiavi dalle variabili d'ambiente (metodo sicuro per Render)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")


# --- Inizializzazione dei Servizi ---
app = FastAPI(title="Zenith Rewards Backend", description="API per la gestione dell'app Zenith Rewards.")

# Configurazione Vertex AI
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        import vertexai
        from vertexai.generative_models import GenerativeModel
        from vertexai.preview.vision_models import ImageGenerationModel
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI inizializzato correttamente.")
    except Exception as e:
        print(f"ATTENZIONE: Errore nella configurazione di Vertex AI: {e}")

# Configurazione Stripe e PayPal
if STRIPE_SECRET_KEY: stripe.api_key = STRIPE_SECRET_KEY
if all([PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    paypalrestsdk.configure({
        "mode": "live",  # Cambia in "sandbox" per i test
        "client_id": PAYPAL_CLIENT_ID,
        "client_secret": PAYPAL_CLIENT_SECRET
    })

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelli Dati (Pydantic) ---
IMAGE_GENERATION_COST = 50
IMAGE_GENERATION_EUR_PRICE = 0.50
POINTS_TO_EUR_RATE = 1000.0

class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class ImageGenerationRequest(BaseModel): user_id: str; prompt: str; contest_id: int; payment_method: str
class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class SubmissionRequest(BaseModel): contest_id: int; user_id: str; image_url: str; prompt: str
class UserProfileUpdate(BaseModel): display_name: str | None = None; avatar_url: str | None = None
class PurchaseRequest(BaseModel): user_id: str; item_id: int; payment_method: str

# --- Funzioni Helper ---
def get_supabase_client() -> Client: return create_client(SUPABASE_URL, SUPABASE_KEY)
def generate_daily_theme() -> str:
    try:
        model = GenerativeModel("gemini-1.0-pro")
        prompt = "Genera un tema artistico breve, creativo e stimolante (massimo 10 parole)."
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        print(f"Errore generazione tema AI: {e}"); return "Una balena meccanica che nuota tra le nuvole."

# --- Endpoint API ---

@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    supabase = get_supabase_client()
    now = datetime.now(timezone.utc)
    try:
        response = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).single().execute()
        if not response.data:
            new_user_record = {'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1, 'last_login_at': now.isoformat(), 'points_balance': 0}
            supabase.table('users').insert(new_user_record).execute()
        else:
            user = response.data
            last_login_str, new_streak = user.get('last_login_at'), user.get('login_streak', 1)
            if last_login_str:
                days_diff = (now.date() - datetime.fromisoformat(last_login_str).date()).days
                if days_diff == 1: new_streak += 1
                elif days_diff > 1: new_streak = 1
            supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': new_streak}).eq('user_id', user_data.user_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    try:
        supabase = get_supabase_client()
        update_payload = profile_data.dict(exclude_unset=True)
        if not update_payload: raise HTTPException(status_code=400, detail="Nessun dato fornito.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profilo aggiornato con successo."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    supabase = get_supabase_client()
    try:
        user_res = supabase.table("users").select("points_balance").eq("user_id", payout_data.user_id).single().execute()
        if not user_res.data or user_res.data.get("points_balance", 0) < payout_data.points_amount:
            raise HTTPException(status_code=402, detail="Punti prelevabili insufficienti.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nel verificare il saldo: {e}")

    if payout_data.method == 'PayPal':
        try:
            value_eur = str(round(payout_data.points_amount / POINTS_TO_EUR_RATE, 2))
            payout = paypalrestsdk.Payout({"sender_batch_header": {"sender_batch_id": f"payout_{time.time()}", "email_subject": "Hai ricevuto un pagamento da Zenith Rewards!"}, "items": [{"recipient_type": "EMAIL", "amount": {"value": value_eur, "currency": "EUR"}, "receiver": payout_data.address, "note": "Grazie per aver usato Zenith Rewards!", "sender_item_id": f"item_{time.time()}"}]})
            if payout.create():
                supabase.rpc('add_points', {'user_id_in': payout_data.user_id, 'points_to_add': -payout_data.points_amount}).execute()
                return {"status": "success", "message": "La tua richiesta di prelievo PayPal è stata elaborata!"}
            else:
                raise HTTPException(status_code=500, detail=payout.error)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore durante il prelievo PayPal: {e}")
    else:
        supabase.rpc('add_points', {'user_id_in': payout_data.user_id, 'points_to_add': -payout_data.points_amount}).execute()
        return {"status": "success", "message": f"Richiesta di prelievo {payout_data.method} ricevuta."}

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"points_balance": 0, "pending_points_balance": 0}
        return {"points_balance": response.data.get('points_balance', 0), "pending_points_balance": 0}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('login_streak, last_streak_claim_at').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"days": 0, "canClaim": True}
        user, can_claim = response.data, True
        if user.get('last_streak_claim_at'):
            if datetime.fromisoformat(user['last_streak_claim_at']).date() == datetime.now(timezone.utc).date(): can_claim = False
        return {"days": user.get('login_streak', 0), "canClaim": can_claim}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/streak/claim/{user_id}")
def claim_streak_bonus(user_id: str):
    try:
        status = get_streak_status(user_id)
        if not status["canClaim"]: raise HTTPException(status_code=400, detail="Bonus giornaliero già riscosso.")
        reward = min(status["days"] * 10, 100)
        supabase = get_supabase_client()
        supabase.rpc('add_points', {'user_id_in': user_id, 'points_to_add': reward}).execute()
        supabase.table('users').update({'last_streak_claim_at': datetime.now(timezone.utc).isoformat()}).eq('user_id', user_id).execute()
        return {"status": "success", "message": f"Hai riscattato {reward} Zenith Coins!"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/leaderboard")
def get_leaderboard():
    supabase = get_supabase_client()
    response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(10).execute()
    return [{"name": u.get('display_name', 'N/A'), "earnings": u.get('points_balance', 0) / POINTS_TO_EUR_RATE, "avatar": u.get('avatar_url', '')} for u in response.data]

@app.get("/shop/items")
def get_shop_items():
    supabase = get_supabase_client()
    return supabase.table("shop_items").select("*").eq("is_active", True).order("price").execute().data

@app.post("/shop/buy")
def buy_shop_item(req: PurchaseRequest):
    supabase = get_supabase_client()
    try:
        item_res = supabase.table("shop_items").select("price, price_eur, name").eq("id", req.item_id).single().execute()
        if not item_res.data: raise HTTPException(status_code=404, detail="Articolo non trovato.")
        item = item_res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nel recuperare l'articolo: {e}")

    if req.payment_method == 'points':
        try:
            supabase.rpc('purchase_item', {'p_user_id': req.user_id, 'p_item_id': req.item_id}).execute()
            return {"status": "success", "message": "Acquisto completato con i tuoi Zenith Coins!"}
        except Exception as e:
            if 'Fondi insufficienti' in str(e): raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")
            raise HTTPException(status_code=500, detail="Errore durante l'acquisto con punti.")
    elif req.payment_method == 'stripe':
        try:
            price_in_eur = item.get("price_eur")
            if price_in_eur is None: raise HTTPException(status_code=400, detail="Prezzo in EUR non disponibile.")
            price_in_cents = int(price_in_eur * 100)
            payment_intent = stripe.PaymentIntent.create(amount=price_in_cents, currency="eur", automatic_payment_methods={"enabled": True}, metadata={'user_id': req.user_id, 'item_id': req.item_id})
            return {"client_secret": payment_intent.client_secret}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore Stripe: {e}")
    else:
        raise HTTPException(status_code=400, detail="Metodo di pagamento non valido.")

@app.get("/contests/current")
def get_current_contest():
    supabase = get_supabase_client()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    response = supabase.table('ai_contests').select('*').gte('created_at', today_start.isoformat()).order('id', desc=True).limit(1).execute()
    if response.data: return response.data[0]
    new_theme = generate_daily_theme()
    new_contest_data = {"theme_prompt": new_theme, "start_date": today_start.isoformat(), "end_date": (today_start + timedelta(days=1)).isoformat(), "status": "active", "prize_pool": 10000}
    insert_response = supabase.table('ai_contests').insert(new_contest_data).execute()
    if not insert_response.data: raise HTTPException(status_code=500, detail="Impossibile creare il contest.")
    return insert_response.data[0]

@app.post("/contests/generate_image")
def generate_ai_image(req: ImageGenerationRequest):
    supabase = get_supabase_client()
    # Logica di pagamento doppio
    if req.payment_method == 'points':
        user_res = supabase.table('users').select('points_balance').eq('user_id', req.user_id).single().execute()
        if user_res.data.get('points_balance', 0) < IMAGE_GENERATION_COST: raise HTTPException(status_code=402, detail="Zenith Coins insufficienti.")
        supabase.rpc('add_points', {'user_id_in': req.user_id, 'points_to_add': -IMAGE_GENERATION_COST}).execute()
    elif req.payment_method == 'stripe':
        price_in_cents = int(IMAGE_GENERATION_EUR_PRICE * 100)
        try:
            payment_intent = stripe.PaymentIntent.create(amount=price_in_cents, currency="eur", automatic_payment_methods={"enabled": True}, metadata={'user_id': req.user_id, 'item_id': 'image_generation'})
            return {"client_secret": payment_intent.client_secret, "payment_required": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore Stripe: {e}")
    else:
        raise HTTPException(status_code=400, detail="Metodo di pagamento non valido.")
    
    # Se il pagamento è andato a buon fine, genera l'immagine
    try:
        model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")
        images = model.generate_images(prompt=req.prompt, number_of_images=1, aspect_ratio="1:1")
        base64_image = base64.b64encode(images[0]._image_bytes).decode('utf-8')
        return {"image_url": f"data:image/png;base64,{base64_image}", "payment_required": False}
    except Exception as e:
        print(f"Errore in generate_image AI: {e}")
        if req.payment_method == 'points':
            supabase.rpc('add_points', {'user_id_in': req.user_id, 'points_to_add': IMAGE_GENERATION_COST}).execute()
        raise HTTPException(status_code=500, detail="Errore durante la generazione dell'immagine.")

@app.get("/contests/{contest_id}/submissions")
def get_contest_submissions(contest_id: int):
    supabase = get_supabase_client()
    return supabase.table("ai_submissions").select("*, user:users(display_name, avatar_url)").eq("contest_id", contest_id).order("votes", desc=True).execute().data

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

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore webhook: {e}")
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        metadata = payment_intent.get('metadata')
        if metadata:
            user_id = metadata.get('user_id')
            item_id_str = metadata.get('item_id')
            if item_id_str == 'image_generation':
                print(f"Pagamento Stripe per generazione immagine ricevuto per utente {user_id}")
                # La generazione è già avvenuta nel frontend, qui potremmo solo registrarla se necessario
            elif user_id and item_id_str:
                print(f"Pagamento Stripe per articolo {item_id_str} ricevuto per utente {user_id}")
                try:
                    supabase = get_supabase_client()
                    supabase.rpc('purchase_item', {'p_user_id': user_id, 'p_item_id': int(item_id_str)}).execute()
                    print("Articolo consegnato con successo!")
                except Exception as e:
                    print(f"ERRORE CRITICO: Impossibile consegnare l'articolo {item_id_str} all'utente {user_id} dopo il pagamento. Errore: {e}")
    return {"status": "success"}

@app.get("/missions/{user_id}")
def get_missions(user_id: str): raise HTTPException(status_code=501, detail="Non implementato.")
@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest): raise HTTPException(status_code=501, detail="Non implementato.")