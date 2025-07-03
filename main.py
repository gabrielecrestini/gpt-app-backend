import os
import time
from datetime import datetime, timezone, timedelta
import json

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import paypalrestsdk

# Carica le variabili d'ambiente da un file .env
load_dotenv()

# --- CONFIGURAZIONE VARIABILI D'AMBIENTE ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")

# --- INIZIALIZZAZIONE APPLICAZIONE E SERVIZI ---
app = FastAPI(
    title="Zenith Rewards Backend",
    description="API for managing the Zenith Rewards app.",
    version="1.1.0"
)

# Inizializzazione Vertex AI (Google Cloud AI)
vertexai = None
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        # Scrive le credenziali in un file temporaneo per l'autenticazione
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("INFO: Vertex AI initialized successfully.")
    except Exception as e:
        print(f"WARNING: Vertex AI initialization failed: {e}")
else:
    print("WARNING: Missing GCP environment variables. Vertex AI is disabled.")

# Inizializzazione Stripe
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY is not configured. Stripe payments will fail.")

# Inizializzazione PayPal
if all([PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    paypal_mode = os.environ.get("PAYPAL_MODE", "sandbox")
    try:
        paypalrestsdk.configure({ "mode": paypal_mode, "client_id": PAYPAL_CLIENT_ID, "client_secret": PAYPAL_CLIENT_SECRET })
    except Exception as e:
        print(f"WARNING: Paypal SDK configuration error: {e}")
else:
    print("WARNING: PayPal credentials are not configured. PayPal payments are disabled.")

# Configurazione CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"], # Aggiungi qui le URL del tuo frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- COSTANTI E MODELLI ---
POINTS_TO_EUR_RATE = 1000.0

class SubscriptionPlan(str, Enum):
    FREE = 'free'
    PREMIUM = 'premium'
    ASSISTANT = 'assistant'

# Mappa per convertire gli ID di prezzo di Stripe in nomi di piani interni
PLAN_MAP = {
    STRIPE_PRICE_ID_PREMIUM: SubscriptionPlan.PREMIUM,
    STRIPE_PRICE_ID_ASSISTANT: SubscriptionPlan.ASSISTANT
}

class UserSyncRequest(BaseModel):
    user_id: str
    email: str | None = None
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class AIGenerationRequest(BaseModel):
    user_id: str
    prompt: str

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str
    address: str

class UserProfileUpdate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None

class CreateSubscriptionRequest(BaseModel):
    user_id: str
    plan_type: str  # 'premium' o 'assistant'
    success_url: str
    cancel_url: str

# --- FUNZIONI HELPER ---

def get_supabase_client() -> Client:
    """Crea e restituisce un client Supabase."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_daily_limit(plan: str) -> int | None:
    """Restituisce il limite di generazioni AI in base al piano. None per illimitato."""
    if plan == SubscriptionPlan.FREE:
        return 1
    if plan == SubscriptionPlan.PREMIUM:
        return 15
    if plan == SubscriptionPlan.ASSISTANT:
        return None  # Illimitato
    return 1

# --- ENDPOINTS API ---

@app.get("/", summary="Root Status")
def read_root():
    """Endpoint di base per verificare che l'API sia operativa."""
    return {"message": "Zenith Rewards Backend API. All systems operational."}


@app.post("/sync_user", summary="Sync User on Login")
def sync_user(user_data: UserSyncRequest):
    """
    Sincronizza l'utente al login: lo crea se non esiste e aggiorna la sua striscia di login.
    Resetta il contatore di generazioni AI se è un nuovo giorno.
    """
    supabase = get_supabase_client()
    now = datetime.now(timezone.utc)
    today = now.date()

    try:
        user_response = supabase.table('users').select('*').eq('user_id', user_data.user_id).maybe_single().execute()
        user = user_response.data

        if not user:
            # Crea un nuovo utente se non esiste
            new_user_record = {
                'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName,
                'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1,
                'last_login_at': now.isoformat(), 'points_balance': 0, 'pending_points_balance': 0,
                'subscription_plan': SubscriptionPlan.FREE.value, 'daily_ai_generations_used': 0,
                'last_generation_reset_date': today.isoformat(), 'stripe_customer_id': None
            }
            supabase.table('users').insert(new_user_record).execute()
            return {"status": "success", "message": "User created."}
        
        # Utente esistente: aggiorna la striscia di login e resetta il contatore AI
        update_payload = {'last_login_at': now.isoformat()}
        
        # Logica per la striscia di login
        last_login_date = datetime.fromisoformat(user['last_login_at']).date()
        days_diff = (today - last_login_date).days
        if days_diff == 1:
            update_payload['login_streak'] = user.get('login_streak', 0) + 1
        elif days_diff > 1:
            update_payload['login_streak'] = 1  # Resetta la striscia

        # Logica per il reset delle generazioni AI giornaliere
        last_reset_date = datetime.fromisoformat(user['last_generation_reset_date']).date()
        if today > last_reset_date:
            update_payload['daily_ai_generations_used'] = 0
            update_payload['last_generation_reset_date'] = today.isoformat()

        supabase.table('users').update(update_payload).eq('user_id', user_data.user_id).execute()
        return {"status": "success", "message": "User synchronized."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during user sync: {str(e)}")


@app.post("/update_profile/{user_id}", summary="Update User Profile")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    """Aggiorna il nome visualizzato e l'avatar dell'utente."""
    supabase = get_supabase_client()
    update_payload = profile_data.model_dump(exclude_unset=True)
    if not update_payload:
        raise HTTPException(status_code=400, detail="No data provided for update.")
    try:
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profile updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/request_payout", summary="Request a Payout")
def request_payout(payout_data: PayoutRequest):
    """Permette a un utente di richiedere un payout dei suoi punti."""
    supabase = get_supabase_client()
    try:
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.rpc('request_payout_function', {
            'p_user_id': payout_data.user_id, 'p_points_amount': payout_data.points_amount,
            'p_value_in_eur': value_eur, 'p_method': payout_data.method, 'p_address': payout_data.address
        }).execute()
        return {"status": "success", "message": "Your payout request has been submitted and will be processed soon!"}
    except Exception as e:
        if 'Punti insufficienti' in str(e): # Questo controllo dipende dall'errore sollevato dalla funzione RPC
            raise HTTPException(status_code=402, detail="Insufficient withdrawable points.")
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")


@app.post("/ai/generate-advice", summary="Generate Advice with AI")
def generate_advice(req: AIGenerationRequest):
    """Genera consigli tramite AI, rispettando i limiti del piano dell'utente."""
    if not vertexai:
        raise HTTPException(status_code=503, detail="AI service is not configured or available.")

    supabase = get_supabase_client()
    try:
        user_res = supabase.table('users').select('subscription_plan, daily_ai_generations_used').eq('user_id', req.user_id).single().execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    
    user_plan = user_res.data.get('subscription_plan', 'free')
    generations_used = user_res.data.get('daily_ai_generations_used', 0)
    daily_limit = get_daily_limit(user_plan)

    # Controlla se l'utente ha superato il limite giornaliero
    if daily_limit is not None and generations_used >= daily_limit:
        raise HTTPException(status_code=429, detail=f"Daily generation limit of {daily_limit} reached. Upgrade your plan for more.")

    # Costruisce il prompt in base al piano
    if user_plan == SubscriptionPlan.ASSISTANT:
        final_prompt = f"Act as a world-class business and marketing mentor. Given the goal '{req.prompt}', create an extremely detailed and professional step-by-step strategy to achieve it. Include market analysis, content strategies, KPIs, and concrete next steps."
    elif user_plan == SubscriptionPlan.PREMIUM:
        final_prompt = f"Given the goal '{req.prompt}', create a detailed 5-7 point action plan. For each point, provide practical examples and tips."
    else: # FREE
        final_prompt = f"Given the goal '{req.prompt}', provide 3 brief and impactful tips."

    try:
        model = GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(final_prompt)
        
        # Incrementa il contatore delle generazioni usate nel DB
        if daily_limit is not None:
            supabase.table('users').update({'daily_ai_generations_used': generations_used + 1}).eq('user_id', req.user_id).execute()

        return {"advice": response.text}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI service error: {e}")


@app.post("/create-checkout-session", summary="Create Stripe Checkout Session")
def create_checkout_session(req: CreateSubscriptionRequest):
    """Crea una sessione di pagamento Stripe per un abbonamento."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe payments are not configured.")
    
    price_map = {'premium': STRIPE_PRICE_ID_PREMIUM, 'assistant': STRIPE_PRICE_ID_ASSISTANT}
    price_id = price_map.get(req.plan_type)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan type provided.")

    try:
        supabase = get_supabase_client()
        user_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).single().execute()
        user_data = user_res.data
        customer_id = user_data.get('stripe_customer_id')

        # Crea un nuovo cliente su Stripe se non esiste già
        if not customer_id:
            customer = stripe.Customer.create(
                email=user_data.get('email'),
                name=user_data.get('display_name'),
                metadata={'internal_user_id': req.user_id}
            )
            customer_id = customer.id
            supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id).execute()

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=req.success_url,
            cancel_url=req.cancel_url,
            metadata={'internal_user_id': req.user_id} # Assicura che l'ID sia sempre presente
        )
        return {"url": checkout_session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stripe-webhook", summary="Handle Stripe Webhooks")
async def stripe_webhook(request: Request):
    """
    Ascolta gli eventi di Stripe per creare, aggiornare o cancellare abbonamenti
    e aggiorna lo stato dell'utente nel database di conseguenza.
    """
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    supabase = get_supabase_client()

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    event_type = event['type']
    subscription = event['data']['object']
    
    if 'customer.subscription' in event_type:
        customer_id = subscription.get('customer')
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()

        if not user_res.data:
            print(f"ERROR: Webhook received for Stripe customer {customer_id} not found in our DB.")
            return Response(status_code=200) # Rispondi 200 a Stripe per non ricevere più la notifica

        user_id = user_res.data['user_id']
        new_plan = SubscriptionPlan.FREE # Default a free in caso di cancellazione

        if event_type in ['customer.subscription.created', 'customer.subscription.updated']:
            status = subscription.get('status')
            if status in ['active', 'trialing']:
                price_id = subscription['items']['data'][0]['price']['id']
                # Converte il price_id in un nome di piano interno
                new_plan = PLAN_MAP.get(price_id, SubscriptionPlan.FREE)
        
        # Aggiorna il DB con il nuovo piano
        try:
            supabase.table('users').update({'subscription_plan': new_plan.value}).eq('user_id', user_id).execute()
            print(f"INFO: Plan for user {user_id} updated to {new_plan.value} due to event {event_type}.")
        except Exception as e:
            print(f"ERROR: Failed to update user {user_id} plan in DB. Error: {e}")
            # Non sollevare un'eccezione HTTP qui, altrimenti Stripe continuerà a inviare la notifica
            
    return Response(status_code=200) # Rispondi sempre 200 a Stripe per confermare la ricezione