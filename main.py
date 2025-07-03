import os
from datetime import datetime, timezone
import json
from enum import Enum
from typing import Literal, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from postgrest.exceptions import APIError as PostgrestAPIError # Import specifico per errori API Supabase
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import vertexai
from vertexai.generative_models import GenerativeModel, Part, Image

load_dotenv()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")

app = FastAPI(title="Zenith Rewards Backend", description="Backend per la gestione di utenti, AI, pagamenti e gamification per Zenith Rewards.")

gemini_flash_model = None
gemini_pro_vision_model = None
vertexai_initialized = False

# Configura il logging
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        sa_key_path = "gcp_sa_key.json"
        with open(sa_key_path, "w") as f:
            f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_key_path

        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        gemini_flash_model = GenerativeModel("gemini-1.5-flash")
        gemini_pro_vision_model = GenerativeModel("gemini-pro-vision")
        vertexai_initialized = True
        logger.info("Vertex AI initialized successfully.")
    except Exception as e:
        logger.error(f"WARNING: Vertex AI configuration error: {e}. AI functionalities might be limited or unavailable.")
else:
    logger.warning("WARNING: Missing GCP credentials. Vertex AI is disabled.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe API key loaded.")
else:
    logger.warning("WARNING: STRIPE_SECRET_KEY not configured. Stripe functionalities are disabled.")

FRONTEND_URL = os.environ.get("NEXT_PUBLIC_FRONTEND_URL", "https://cashhh-52f38.web.app")

allowed_origins = [
    "http://localhost:3000",
    "https://cashhh-52f38.web.app",
    "https://cashhh-52738.web.app",
    FRONTEND_URL
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

POINTS_TO_EUR_RATE = 1000.0

class SubscriptionPlan(str, Enum):
    FREE = 'free'
    PREMIUM = 'premium'
    ASSISTANT = 'assistant'

class ContentType(str, Enum):
    IMAGE = 'IMAGE'
    POST = 'POST'
    VIDEO = 'VIDEO'

class ItemType(str, Enum):
    BOOST = 'BOOST'
    COSMETIC = 'COSMETIC'
    GENERATION_PACK = 'GENERATION_PACK'

class UserSyncRequest(BaseModel):
    user_id: str
    email: str | None = None
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class UserProfileUpdate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str
    address: str

class AIAdviceRequest(BaseModel):
    user_id: str
    prompt: str

class AIGenerationRequest(BaseModel):
    user_id: str
    prompt: str
    content_type: ContentType
    payment_method: Literal['points', 'stripe']
    contest_id: int | None = None

class VoteContentRequest(BaseModel):
    user_id: str

class CreateSubscriptionRequest(BaseModel):
    user_id: str
    plan_type: str
    success_url: str
    cancel_url: str

class ShopBuyRequest(BaseModel):
    user_id: str
    item_id: int
    payment_method: Literal['points', 'stripe']

def get_supabase_client() -> Client:
    try:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("Supabase credentials not configured.")
        # Utilizza ClientOptions per una gestione più robusta dei timeout o retry
        options = ClientOptions(postgrest_client_timeout=10, storage_client_timeout=10)
        return create_client(SUPABASE_URL, SUPABASE_KEY, options=options)
    except Exception as e:
        logger.critical(f"Failed to create Supabase client: {e}")
        raise HTTPException(status_code=500, detail=f"Supabase client initialization failed: {e}")

class UserManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    def _execute_supabase_query(self, query_builder, error_message: str) -> Any:
        try:
            result = query_builder.execute()
            if hasattr(result, 'data'):
                return result.data
            return result # For RPC calls that might return something else directly
        except PostgrestAPIError as e:
            logger.error(f"Supabase API Error in UserManager: {error_message} - {e.code}: {e.message} - {e.details}")
            raise HTTPException(status_code=400, detail=f"Database error: {e.message}. Hint: {e.details}")
        except Exception as e:
            logger.error(f"Unexpected error in UserManager: {error_message} - {e}")
            raise HTTPException(status_code=500, detail=f"Internal server error during {error_message}.")

    def sync_user(self, user_data: UserSyncRequest):
        now = datetime.now(timezone.utc)
        logger.info(f"Attempting to sync user: {user_data.user_id}")
        
        user_record = self._execute_supabase_query(
            self.supabase.table('users').select('user_id, last_login_at, login_streak, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date').eq('user_id', user_data.user_id).maybe_single(),
            "user sync fetch"
        )
        
        if not user_record:
            logger.info(f"Creating new user: {user_data.user_id}")
            new_user_record = {
                'user_id': user_data.user_id,
                'email': user_data.email,
                'display_name': user_data.displayName,
                'referrer_id': user_data.referrer_id,
                'avatar_url': user_data.avatar_url,
                'login_streak': 1,
                'last_login_at': now.isoformat(),
                'points_balance': 0,
                'pending_points_balance': 0,
                'subscription_plan': SubscriptionPlan.FREE.value,
                'daily_ai_generations_used': 0,
                'last_generation_reset_date': now.isoformat(),
                'daily_votes_used': 0,
                'last_vote_reset_date': now.isoformat()
            }
            self._execute_supabase_query(
                self.supabase.table('users').insert(new_user_record),
                "insert new user"
            )
            logger.info(f"New user {user_data.user_id} created successfully.")
        else:
            logger.info(f"Updating existing user: {user_data.user_id}")
            last_login_str = user_record.get('last_login_at')
            current_streak = user_record.get('login_streak', 0)
            
            new_streak = 1 # Default a 1 se la streak è interrotta o non c'era un login precedente
            if last_login_str:
                last_login_date = datetime.fromisoformat(last_login_str).date()
                today = now.date()
                days_diff = (today - last_login_date).days

                if days_diff == 1:
                    new_streak = current_streak + 1
                elif days_diff == 0: # Già loggato oggi, non resettare la streak
                    new_streak = current_streak
            
            update_data: Dict[str, Any] = {'last_login_at': now.isoformat(), 'login_streak': new_streak}
            
            last_gen_reset_date_str = user_record.get('last_generation_reset_date')
            last_gen_reset_date = datetime.fromisoformat(last_gen_reset_date_str).date() if last_gen_reset_date_str else now.date()
            if now.date() > last_gen_reset_date:
                update_data['daily_ai_generations_used'] = 0
                update_data['last_generation_reset_date'] = now.isoformat()
            
            last_vote_reset_date_str = user_record.get('last_vote_reset_date')
            last_vote_reset_date = datetime.fromisoformat(last_vote_reset_date_str).date() if last_vote_reset_date_str else now.date()
            if now.date() > last_vote_reset_date:
                update_data['daily_votes_used'] = 0
                update_data['last_vote_reset_date'] = now.isoformat()

            self._execute_supabase_query(
                self.supabase.table('users').update(update_data).eq('user_id', user_data.user_id),
                "update existing user"
            )
            logger.info(f"User {user_data.user_id} updated successfully.")
        return {"status": "success"}

    def update_profile(self, user_id: str, profile_data: UserProfileUpdate):
        logger.info(f"Updating profile for user: {user_id}")
        update_payload = profile_data.model_dump(exclude_unset=True)
        if not update_payload: 
            logger.warning(f"No data provided for profile update for user {user_id}")
            raise HTTPException(status_code=400, detail="No data provided to update.")
        
        self._execute_supabase_query(
            self.supabase.table('users').update(update_payload).eq('user_id', user_id),
            "update user profile"
        )
        logger.info(f"Profile for user {user_id} updated successfully.")
        return {"status": "success", "message": "Profile updated successfully."}

    def get_user_balance(self, user_id: str):
        logger.info(f"Fetching balance for user: {user_id}")
        response_data = self._execute_supabase_query(
            self.supabase.table('users').select('points_balance, pending_points_balance').eq('user_id', user_id).maybe_single(),
            "fetch user balance"
        )
        if not response_data:
            logger.warning(f"User {user_id} not found when fetching balance.")
            raise HTTPException(status_code=404, detail="User not found.")
        return response_data

    def get_user_profile(self, user_id: str):
        logger.info(f"Fetching profile for user: {user_id}")
        response_data = self._execute_supabase_query(
            self.supabase.table('users').select(
                'subscription_plan, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date, points_balance'
            ).eq('user_id', user_id).maybe_single(),
            "fetch user profile"
        )
        if not response_data:
            logger.warning(f"User {user_id} not found when fetching profile. Returning default data.")
            return {
                "subscription_plan": SubscriptionPlan.FREE.value,
                "daily_ai_generations_used": 0,
                "last_generation_reset_date": datetime.now(timezone.utc).isoformat(),
                "daily_votes_used": 0,
                "last_vote_reset_date": datetime.now(timezone.utc).isoformat(),
                "points_balance": 0
            }
        return response_data

    def get_streak_status(self, user_id: str):
        logger.info(f"Fetching streak status for user: {user_id}")
        response_data = self._execute_supabase_query(
            self.supabase.table('users').select('login_streak').eq('user_id', user_id).maybe_single(),
            "fetch streak status"
        )
        if not response_data: 
            logger.warning(f"User {user_id} not found when fetching streak. Returning 0.")
            return {"login_streak": 0}
        return response_data

    def claim_streak_reward(self, user_id: str):
        logger.info(f"Attempting to claim streak reward for user: {user_id}")
        result_data = self._execute_supabase_query(
            self.supabase.rpc('claim_streak_reward', {'p_user_id': user_id}),
            "claim streak RPC"
        )
        # result_data from RPC is usually a list of dicts or single dict
        if result_data and isinstance(result_data, list) and len(result_data) > 0:
            return result_data[0]
        elif result_data and isinstance(result_data, dict):
             return result_data
        logger.error(f"Unexpected RPC return for claim_streak_reward for user {user_id}: {result_data}")
        raise HTTPException(status_code=400, detail="Failed to claim streak reward. Check database function logs for details.")

    def get_referral_stats(self, user_id: str):
        logger.info(f"Fetching referral stats for user: {user_id}")
        referral_count_res = self._execute_supabase_query(
            self.supabase.table('users').select('count').eq('referrer_id', user_id).maybe_single(),
            "fetch referral count"
        )
        referral_count = referral_count_res['count'] if referral_count_res else 0

        referral_earnings = referral_count * 100
        logger.info(f"Referral stats for {user_id}: count={referral_count}, earnings={referral_earnings}")
        return {"referral_count": referral_count, "referral_earnings": referral_earnings}

class AIManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    AI_GENERATION_LIMITS = {
        SubscriptionPlan.FREE: 3,
        SubscriptionPlan.PREMIUM: 15,
        SubscriptionPlan.ASSISTANT: 100
    }
    DAILY_VOTE_LIMITS = {
        SubscriptionPlan.FREE: 5,
        SubscriptionPlan.PREMIUM: 20,
        SubscriptionPlan.ASSISTANT: 50
    }

    def get_ai_cost(self, user_plan: SubscriptionPlan):
        if user_plan == SubscriptionPlan.FREE:
            return {"points": 500, "eur": 0.50}
        elif user_plan == SubscriptionPlan.PREMIUM:
            return {"points": 200, "eur": 0.20}
        elif user_plan == SubscriptionPlan.ASSISTANT:
            return {"points": 100, "eur": 0.10}
        return {"points": 1000, "eur": 1.00}

    async def generate_advice(self, req: AIAdviceRequest):
        if not vertexai_initialized or not gemini_flash_model:
            logger.error("AI service (Gemini Flash) is not available.")
            raise HTTPException(status_code=503, detail="AI service (Gemini Flash) is not available or not initialized.")

        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        generations_used = user_profile.get('daily_ai_generations_used', 0)
        last_reset_str = user_profile.get('last_generation_reset_date', datetime.now(timezone.utc).isoformat())
        
        if datetime.fromisoformat(last_reset_str).date() < datetime.now(timezone.utc).date():
            generations_used = 0
            UserManager(self.supabase)._execute_supabase_query(
                self.supabase.table('users').update({
                    'daily_ai_generations_used': 0,
                    'last_generation_reset_date': datetime.now(timezone.utc).isoformat()
                }).eq('user_id', req.user_id),
                "reset daily AI generations"
            )

        if generations_used >= self.AI_GENERATION_LIMITS.get(user_plan, 0):
            logger.warning(f"User {req.user_id} exceeded AI generation limit for plan {user_plan.value}")
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite di generazioni AI giornaliere ({self.AI_GENERATION_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'. Effettua l'upgrade per più generazioni!")

        final_prompt = f"Data l'idea o l'obiettivo '{req.prompt}', fornisci 3 consigli brevi e di impatto per il successo."
        if user_plan == SubscriptionPlan.PREMIUM:
            final_prompt = f"Agisci come un esperto di strategie aziendali. Data l'idea o l'obiettivo '{req.prompt}', crea un piano d'azione dettagliato di 5-7 punti con esempi pratici e suggerimenti per marketing e social media."
        elif user_plan == SubscriptionPlan.ASSISTANT:
            final_prompt = f"Sei un mentore aziendale di livello mondiale e un esperto di marketing digitale, dropshipping, trading e social media. Data l'idea o l'obiettivo '{req.prompt}', crea una strategia passo-passo estremamente dettagliata e personalizzata, includendo tattiche specifiche per scalare sia i social della piattaforma Zenith Rewards che i social esterni, suggerimenti per il dropshipping, il trading e l'e-commerce, e un piano di viralità completo. La tua risposta deve essere completa, azionabile e coprire tutte le sfaccettature richieste."
        
        try:
            logger.info(f"Generating AI advice for user {req.user_id} with prompt: {req.prompt[:50]}...")
            response_ai = gemini_flash_model.generate_content(final_prompt)
            generated_text = response_ai.text.strip()

            UserManager(self.supabase)._execute_supabase_query(
                self.supabase.table('users').update({
                    'daily_ai_generations_used': generations_used + 1
                }).eq('user_id', req.user_id),
                "increment daily AI generations"
            )
            logger.info(f"AI advice generated and usage incremented for user {req.user_id}.")
            return {"advice": generated_text}
        except Exception as e:
            logger.error(f"Error during AI advice generation for user {req.user_id}: {e}")
            raise HTTPException(status_code=503, detail=f"Errore del servizio AI: {e}. Riprova più tardi.")

    async def generate_content(self, req: AIGenerationRequest):
        if not vertexai_initialized:
            logger.error("AI service is not available for content generation.")
            raise HTTPException(status_code=503, detail="AI service is not available.")

        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        
        cost = self.get_ai_cost(user_plan)
        
        if req.payment_method == 'points':
            if user_profile['points_balance'] < cost['points']:
                logger.warning(f"User {req.user_id} has insufficient points ({user_profile['points_balance']}) for AI content generation (needed {cost['points']}).")
                raise HTTPException(status_code=402, detail=f"Punti insufficienti. Hai bisogno di {cost['points']} ZC.")
        elif req.payment_method == 'stripe':
            pass

        generated_url = None
        generated_text = None
        ai_strategy_plan = f"Piano base per la viralità: Condividi la tua creazione sui social media di Zenith Rewards e incoraggia i tuoi amici a votare! Per strategie avanzate, considera l'upgrade al piano Premium o Assistant."

        try:
            logger.info(f"Generating AI content ({req.content_type.value}) for user {req.user_id} with prompt: {req.prompt[:50]}...")
            if req.content_type == ContentType.IMAGE:
                if not gemini_pro_vision_model:
                     raise HTTPException(status_code=503, detail="AI image generation model not available.")
                image_response = gemini_pro_vision_model.generate_content(f"Crea una breve descrizione testuale e un suggerimento visivo per un'immagine basata su: '{req.prompt}'.")
                generated_text = f"Immagine generata: {image_response.text.strip()}\n(Simulazione: L'API reale genererebbe un URL immagine.)"
                generated_url = "https://via.placeholder.com/400x300?text=AI+Image"

            elif req.content_type == ContentType.POST:
                response_ai = gemini_flash_model.generate_content(f"Crea un post coinvolgente e conciso per i social media basato su: '{req.prompt}'. Focus su un linguaggio accattivante e hashtag pertinenti.")
                generated_text = response_ai.text.strip()
            
            elif req.content_type == ContentType.VIDEO:
                response_ai = gemini_flash_model.generate_content(f"Genera una breve sceneggiatura o un'idea per un video di 15-30 secondi basata su: '{req.prompt}'.")
                generated_text = f"Sceneggiatura video generata: {response_ai.text.strip()}\n(Simulazione: L'API reale genererebbe un URL video.)"
                generated_url = "https://www.w3schools.com/html/mov_bbb.mp4"

            if user_plan == SubscriptionPlan.PREMIUM:
                strategy_response = gemini_flash_model.generate_content(f"Espandi il piano di viralità per '{req.prompt}' e '{req.content_type.value}' con 3-5 strategie di marketing digitale e consigli per l'engagement sui social. Evidenzia le parole chiave.")
                ai_strategy_plan = strategy_response.text.strip()
            elif user_plan == SubscriptionPlan.ASSISTANT:
                strategy_response = gemini_flash_model.generate_content(f"Agisci come consulente di marketing esperto. Crea un PIANO VIRALE AVANZATO dettagliato per il contenuto '{req.prompt}' ({req.content_type.value}), includendo analisi del target, canali di distribuzione (Zenith Rewards e social esterni), calendario di pubblicazione suggerito, idee per collaborazioni, ottimizzazione SEO/hashtag e misurazione dei risultati. Pensa come un growth hacker.")
                ai_strategy_plan = strategy_response.text.strip()

            insert_data = {
                'user_id': req.user_id,
                'contest_id': req.contest_id,
                'prompt': req.prompt,
                'content_type': req.content_type.value,
                'generated_url': generated_url,
                'generated_text': generated_text,
                'ai_strategy_plan': ai_strategy_plan,
                'is_published': False,
                'votes': 0
            }
            content_res = UserManager(self.supabase)._execute_supabase_query(
                self.supabase.table('ai_contents').insert(insert_data),
                "insert AI content"
            )
            ai_content_id = content_res[0]['id'] if content_res else None
            
            if not ai_content_id:
                raise Exception("Failed to retrieve ID of generated AI content.")

            if req.payment_method == 'points':
                UserManager(self.supabase)._execute_supabase_query(
                    self.supabase.rpc('deduct_points', {
                        'p_user_id': req.user_id,
                        'p_amount': cost['points'],
                        'p_reason': f'AI Generation - {req.content_type.value}'
                    }),
                    "deduct points for AI generation"
                )
            
            UserManager(self.supabase)._execute_supabase_query(
                self.supabase.table('users').update({
                    'daily_ai_generations_used': user_profile['daily_ai_generations_used'] + 1,
                    'last_content_generated_id': ai_content_id
                }).eq('user_id', req.user_id),
                "increment AI generations usage"
            )
            logger.info(f"AI content generated and usage incremented for user {req.user_id}. Content ID: {ai_content_id}")
            return {
                "id": ai_content_id,
                "prompt": req.prompt,
                "content_type": req.content_type.value,
                "generated_url": generated_url,
                "generated_text": generated_text,
                "ai_strategy_plan": ai_strategy_plan,
                "payment_required": False
            }

        except PostgrestAPIError as e:
            logger.error(f"Supabase API Error during AI content generation: {e.code}: {e.message} - {e.details}")
            if "Punti insufficienti" in e.message: # Custom message from RPC
                raise HTTPException(status_code=402, detail="Punti insufficienti per la generazione AI.")
            raise HTTPException(status_code=400, detail=f"Database error during AI generation: {e.message}. Hint: {e.details}")
        except Exception as e:
            logger.error(f"Unexpected error during AI content generation for user {req.user_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Errore durante la generazione AI: {e}. Riprova più tardi.")

    def publish_ai_content(self, ai_content_id: int):
        logger.info(f"Publishing AI content: {ai_content_id}")
        UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('ai_contents').update({'is_published': True}).eq('id', ai_content_id),
            "publish AI content"
        )
        logger.info(f"AI content {ai_content_id} published successfully.")
        return {"status": "success", "message": "Content published successfully."}

    async def get_feed(self):
        logger.info("Fetching AI content feed.")
        response = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('ai_contents').select(
                'id, user_id, contest_id, prompt, content_type, generated_url, generated_text, ai_strategy_plan, votes, created_at, users(display_name, avatar_url)'
            ).eq('is_published', True).order('votes', desc=True).order('created_at', desc=True).limit(50),
            "fetch AI content feed"
        )
        
        formatted_feed = []
        if response:
            for item in response:
                formatted_feed.append({
                    "id": item['id'],
                    "user_id": item['user_id'],
                    "contest_id": item['contest_id'],
                    "prompt": item['prompt'],
                    "content_type": item['content_type'],
                    "generated_url": item['generated_url'],
                    "generated_text": item['generated_text'],
                    "ai_strategy_plan": item['ai_strategy_plan'],
                    "votes": item['votes'],
                    "user": {
                        "display_name": item['users']['display_name'],
                        "avatar_url": item['users']['avatar_url']
                    }
                })
        logger.info(f"Fetched {len(formatted_feed)} items for AI content feed.")
        return formatted_feed

    async def vote_content(self, content_id: int, user_id: str):
        logger.info(f"User {user_id} attempting to vote for content {content_id}.")
        user_profile = UserManager(self.supabase).get_user_profile(user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        daily_votes_used = user_profile.get('daily_votes_used', 0)
        last_vote_reset_date = user_profile.get('last_vote_reset_date', datetime.now(timezone.utc).isoformat())

        if datetime.fromisoformat(last_vote_reset_date).date() < datetime.now(timezone.utc).date():
            daily_votes_used = 0
            UserManager(self.supabase)._execute_supabase_query(
                self.supabase.table('users').update({
                    'daily_votes_used': 0,
                    'last_vote_reset_date': datetime.now(timezone.utc).isoformat()
                }).eq('user_id', user_id),
                "reset daily votes"
            )

        if daily_votes_used >= self.DAILY_VOTE_LIMITS.get(user_plan, 0):
            logger.warning(f"User {user_id} exceeded daily vote limit for plan {user_plan.value}.")
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite giornaliero di voti ({self.DAILY_VOTE_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'.")

        existing_vote = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('votes').select('*').eq('user_id', user_id).eq('content_id', content_id).maybe_single(),
            "check existing vote"
        )
        if existing_vote:
            logger.warning(f"User {user_id} already voted for content {content_id}.")
            raise HTTPException(status_code=400, detail="Hai già votato questo contenuto.")

        content_owner_res = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('ai_contents').select('user_id').eq('id', content_id).maybe_single(),
            "get content owner"
        )
        if content_owner_res and content_owner_res['user_id'] == user_id:
            logger.warning(f"User {user_id} tried to vote for their own content {content_id}.")
            raise HTTPException(status_code=400, detail="Non puoi votare il tuo stesso contenuto.")

        UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('votes').insert({'user_id': user_id, 'content_id': content_id}),
            "insert new vote"
        )
        UserManager(self.supabase)._execute_supabase_query(
            self.supabase.rpc('increment_content_votes', {'p_content_id': content_id}),
            "increment content votes RPC"
        )

        UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('users').update({
                'daily_votes_used': daily_votes_used + 1
            }).eq('user_id', user_id),
            "increment daily votes usage"
        )
        logger.info(f"User {user_id} successfully voted for content {content_id}.")
        return {"status": "success", "message": "Voto registrato con successo!"}

class ContestManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    CONTEST_REWARD_POOLS = {
        SubscriptionPlan.FREE: 10.00,
        SubscriptionPlan.PREMIUM: 30.00,
        SubscriptionPlan.ASSISTANT: 60.00
    }

    def get_current_contest(self, user_plan: SubscriptionPlan):
        logger.info(f"Fetching current contest for plan: {user_plan.value}")
        now = datetime.now(timezone.utc)
        result = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('contests').select('*') \
                .lte('start_date', now.isoformat()) \
                .gte('end_date', now.isoformat()) \
                .contains('min_plan_access', [user_plan.value]) \
                .order('end_date', desc=False) \
                .limit(1).maybe_single(),
            f"fetch current contest for plan {user_plan.value}"
        )
        
        if result: # result is the data itself, not a response object
            result['reward_pool_euro'] = self.CONTEST_REWARD_POOLS.get(user_plan, 0.00)
            logger.info(f"Found active contest: {result['theme_prompt']}")
            return result
        logger.info(f"No active contest found for plan {user_plan.value}.")
        return None

    def get_leaderboard(self):
        logger.info("Fetching leaderboard.")
        response_data = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('users').select('display_name, avatar_url, points_balance').order('points_balance', desc=True).limit(100),
            "fetch leaderboard"
        )
        logger.info(f"Fetched {len(response_data) if response_data else 0} users for leaderboard.")
        return response_data if response_data else []

class ShopManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    def get_shop_items(self):
        logger.info("Fetching shop items.")
        response_data = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('shop_items').select('*').order('price_points', desc=False),
            "fetch shop items"
        )
        logger.info(f"Fetched {len(response_data) if response_data else 0} shop items.")
        return response_data if response_data else []

    async def buy_item(self, req: ShopBuyRequest):
        logger.info(f"User {req.user_id} attempting to buy item {req.item_id} with {req.payment_method}.")
        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        item = UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('shop_items').select('*').eq('id', req.item_id).maybe_single(),
            f"fetch shop item {req.item_id}"
        )
        
        if not item: 
            logger.warning(f"Item {req.item_id} not found for purchase by user {req.user_id}.")
            raise HTTPException(status_code=404, detail="Item not found.")

        if req.payment_method == 'points':
            if user_profile['points_balance'] < item['price_points']:
                logger.warning(f"User {req.user_id} has insufficient points ({user_profile['points_balance']}) to buy item {req.item_id} (needed {item['price_points']}).")
                raise HTTPException(status_code=402, detail="Punti insufficienti per l'acquisto.")
            
            UserManager(self.supabase)._execute_supabase_query(
                self.supabase.rpc('deduct_points', {
                    'p_user_id': req.user_id,
                    'p_amount': item['price_points'],
                    'p_reason': f'Shop Purchase: {item["name"]} (Points)'
                }),
                "deduct points for shop item"
            )

            await self._apply_item_effect(req.user_id, item, req.payment_method, item['price_points'], None)

            logger.info(f"User {req.user_id} successfully bought item {item['name']} with points.")
            return {"status": "success", "message": f"Acquisto di '{item['name']}' completato con successo con i punti!"}
        
        elif req.payment_method == 'stripe':
            if not STRIPE_SECRET_KEY: 
                logger.error("Stripe not configured for shop purchases.")
                raise HTTPException(status_code=500, detail="Stripe not configured.")
            if not item['price_eur']: 
                logger.warning(f"Item {req.item_id} does not have EUR price defined for Stripe purchase.")
                raise HTTPException(status_code=400, detail="Questo articolo non ha un prezzo in EUR definito.")

            try:
                payment_intent = stripe.PaymentIntent.create(
                    amount=int(item['price_eur'] * 100),
                    currency='eur',
                    metadata={'user_id': req.user_id, 'item_id': item['id'], 'item_name': item['name']},
                    automatic_payment_methods={'enabled': True}
                )
                logger.info(f"Stripe Payment Intent created for user {req.user_id}, item {item['name']}.")
                return {"payment_required": True, "client_secret": payment_intent.client_secret, "message": "Procedi al pagamento Stripe."}

            except stripe.error.StripeError as e:
                logger.error(f"Stripe error during Payment Intent creation: {e.user_message}")
                raise HTTPException(status_code=500, detail=f"Errore Stripe: {e.user_message}")
            except Exception as e:
                logger.error(f"Unexpected error creating Payment Intent: {e}")
                raise HTTPException(status_code=500, detail=f"Errore nella creazione del Payment Intent: {e}")

    async def _apply_item_effect(self, user_id: str, item: dict, payment_method: str, amount_points: float | None, amount_eur: float | None):
        logger.info(f"Applying effect for item {item['name']} to user {user_id}. Type: {item['item_type']}")
        if item['item_type'] == ItemType.BOOST.value:
            logger.info(f"Boost '{item['name']}' applied to user {user_id}. Effect: {item.get('effect')}")
        
        elif item['item_type'] == ItemType.COSMETIC.value:
            logger.info(f"Cosmetic '{item['name']}' applied to user {user_id}. Effect: {item.get('effect')}")
        
        elif item['item_type'] == ItemType.GENERATION_PACK.value:
            effect_data = json.loads(item.get('effect', '{}'))
            generations_to_add = effect_data.get('generations', 0)
            if generations_to_add > 0:
                user_res = UserManager(self.supabase)._execute_supabase_query(
                    self.supabase.table('users').select('daily_ai_generations_used').eq('user_id', user_id).maybe_single(),
                    "fetch user daily generations for item effect"
                )
                if user_res:
                    current_generations_used = user_res['daily_ai_generations_used']
                    UserManager(self.supabase)._execute_supabase_query(
                        self.supabase.table('users').update({
                            'daily_ai_generations_used': current_generations_used - generations_to_add # Allows more generations by reducing the count
                        }).eq('user_id', user_id),
                        "update user daily generations with item effect"
                    )
                    logger.info(f"Added {generations_to_add} AI generations to user {user_id}.")

        UserManager(self.supabase)._execute_supabase_query(
            self.supabase.table('user_purchases').insert({
                'user_id': user_id,
                'item_id': item['id'],
                'purchase_date': datetime.now(timezone.utc).isoformat(),
                'payment_method': payment_method,
                'amount_paid_points': amount_points,
                'amount_paid_eur': amount_eur,
                'status': 'completed'
            }),
            "log user purchase"
        )
        logger.info(f"Item effect for {item['name']} applied and purchase logged for user {user_id}.")

def get_user_manager(supabase: Client = Depends(get_supabase_client)): return UserManager(supabase)
def get_ai_manager(supabase: Client = Depends(get_supabase_client)): return AIManager(supabase)
def get_contest_manager(supabase: Client = Depends(get_supabase_client)): return ContestManager(supabase)
def get_shop_manager(supabase: Client = Depends(get_supabase_client)): return ShopManager(supabase)

@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend is operational. Access the API documentation at /docs."}

@app.post("/sync_user")
def sync_user_endpoint(user_data: UserSyncRequest, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.sync_user(user_data)
    except HTTPException as e:
        logger.error(f"HTTPException in sync_user_endpoint: {e.detail}")
        raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in sync_user_endpoint for user {user_data.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error during user sync: {str(e)}")

@app.post("/update_profile/{user_id}")
def update_profile_endpoint(user_id: str, profile_data: UserProfileUpdate, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.update_profile(user_id, profile_data)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in update_profile_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/request_payout")
def request_payout_endpoint(payout_data: PayoutRequest, user_manager: UserManager = Depends(get_user_manager)):
    logger.info(f"Payout request from user {payout_data.user_id} for {payout_data.points_amount} points.")
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        result = UserManager(supabase)._execute_supabase_query(
            supabase.rpc('request_payout_function', { 
                'p_user_id': payout_data.user_id, 
                'p_points_amount': payout_data.points_amount, 
                'p_value_in_eur': value_eur, 
                'p_method': payout_data.method, 
                'p_address': payout_data.address 
            }),
            "request payout RPC"
        )
        
        if result and isinstance(result, list) and len(result) > 0:
            logger.info(f"Payout request successful for user {payout_data.user_id}.")
            return {"status": "success", "message": result[0].get('message', "Your payout request has been sent and will be processed soon!")}
        
        logger.error(f"Unexpected RPC return for request_payout_function for user {payout_data.user_id}: {result}")
        raise HTTPException(status_code=400, detail="Failed to process payout request. Check database function logs.")

    except HTTPException as e:
        if 'Punti insufficienti' in e.detail:
            raise HTTPException(status_code=402, detail="Punti insufficienti per il prelievo.")
        logger.error(f"HTTPException in request_payout_endpoint: {e.detail}")
        raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in request_payout_endpoint for user {payout_data.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error processing payout request: {str(e)}")

@app.get("/users/{user_id}/profile")
def get_user_profile_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_user_profile(user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_user_profile_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/get_user_balance/{user_id}")
def get_user_balance_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_user_balance(user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_user_balance_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/streak/status/{user_id}")
def get_streak_status_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_streak_status(user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_streak_status_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/streak/claim/{user_id}")
def claim_streak_reward_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.claim_streak_reward(user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in claim_streak_reward_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/leaderboard")
def get_leaderboard_endpoint(contest_manager: ContestManager = Depends(get_contest_manager)):
    try:
        return contest_manager.get_leaderboard()
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_leaderboard_endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/referral_stats/{user_id}")
def get_referral_stats_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_referral_stats(user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_referral_stats_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/ai/generate-advice")
async def generate_advice_endpoint(req: AIAdviceRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.generate_advice(req)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in generate_advice_endpoint for user {req.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/ai/generate")
async def generate_content_endpoint(req: AIGenerationRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.generate_content(req)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in generate_content_endpoint for user {req.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/ai/content/{ai_content_id}/publish")
def publish_content_endpoint(ai_content_id: int, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return ai_manager.publish_ai_content(ai_content_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in publish_content_endpoint for content {ai_content_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/ai/content/feed")
async def get_content_feed_endpoint(ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.get_feed()
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_content_feed_endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/ai/content/{content_id}/vote")
async def vote_content_endpoint(content_id: int, req: VoteContentRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.vote_content(content_id, req.user_id)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in vote_content_endpoint for content {content_id} by user {req.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/contests/current/{user_id}")
def get_current_contest_endpoint(user_id: str, contest_manager: ContestManager = Depends(get_contest_manager), user_manager: UserManager = Depends(get_user_manager)):
    try:
        user_profile = user_manager.get_user_profile(user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        contest = contest_manager.get_current_contest(user_plan)
        if not contest:
            raise HTTPException(status_code=404, detail="Nessun contest attivo disponibile per il tuo piano al momento.")
        return contest
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_current_contest_endpoint for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/shop/items")
def get_shop_items_endpoint(shop_manager: ShopManager = Depends(get_shop_manager)):
    try:
        return shop_manager.get_shop_items()
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in get_shop_items_endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/shop/buy")
async def buy_shop_item_endpoint(req: ShopBuyRequest, shop_manager: ShopManager = Depends(get_shop_manager)):
    try:
        return await shop_manager.buy_item(req)
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in buy_shop_item_endpoint for user {req.user_id}, item {req.item_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/create-checkout-session")
def create_checkout_session_endpoint(req: CreateSubscriptionRequest, supabase: Client = Depends(get_supabase_client)):
    if not stripe.api_key: raise HTTPException(status_code=500, detail="Stripe not configured.")
    price_map = {
        'premium': STRIPE_PRICE_ID_PREMIUM,
        'assistant': STRIPE_PRICE_ID_ASSISTANT
    }
    price_id = price_map.get(req.plan_type)
    if not price_id: raise HTTPException(status_code=400, detail="Invalid plan type specified.")
    
    try:
        user_res = UserManager(supabase)._execute_supabase_query(
            supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).maybe_single(),
            "fetch user for Stripe checkout"
        )
        if not user_res: raise HTTPException(status_code=404, detail="User not found.")
        
        user_data = user_res
        customer_id = user_data.get('stripe_customer_id')
        
        if not customer_id:
            logger.info(f"Creating new Stripe customer for user {req.user_id}.")
            customer = stripe.Customer.create(
                email=user_data.get('email'),
                metadata={'user_id': req.user_id}
            )
            customer_id = customer.id
            UserManager(supabase)._execute_supabase_query(
                supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id),
                "update user with Stripe customer ID"
            )
        
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=req.success_url,
            cancel_url=req.cancel_url,
            metadata={
                'user_id': req.user_id,
                'plan_type': req.plan_type
            }
        )
        logger.info(f"Stripe Checkout Session created for user {req.user_id}.")
        return {"url": checkout_session.url}
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating checkout session: {e.user_message}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Errore Stripe: {e.user_message}")
    except HTTPException as e: raise e
    except Exception as e:
        logger.critical(f"Unhandled exception in create_checkout_session_endpoint for user {req.user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request, supabase: Client = Depends(get_supabase_client)):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')

    if not STRIPE_WEBHOOK_SECRET:
        logger.error("Stripe webhook secret not configured. Cannot process webhooks.")
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured.")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        logger.error(f"Invalid payload for Stripe webhook: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid signature for Stripe webhook: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")
    except Exception as e:
        logger.error(f"Unexpected error processing Stripe webhook event: {e}")
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    event_type = event['type']
    data_object = event['data']['object']
    logger.info(f"Received Stripe webhook event type: {event_type}")

    if event_type == 'customer.subscription.created' or event_type == 'customer.subscription.updated':
        subscription = data_object
        customer_id = subscription.get('customer')
        price_id = subscription['items']['data'][0]['price']['id']
        status = subscription.get('status')
        
        user_res = UserManager(supabase)._execute_supabase_query(
            supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single(),
            "fetch user for subscription webhook"
        )
        
        if user_res:
            user_id = user_res['user_id']
            new_plan = SubscriptionPlan.FREE.value
            
            if price_id == STRIPE_PRICE_ID_PREMIUM:
                new_plan = SubscriptionPlan.PREMIUM.value
            elif price_id == STRIPE_PRICE_ID_ASSISTANT:
                new_plan = SubscriptionPlan.ASSISTANT.value
            
            if status in ['active', 'trialing']:
                UserManager(supabase)._execute_supabase_query(
                    supabase.table('users').update({'subscription_plan': new_plan}).eq('user_id', user_id),
                    "update user subscription plan"
                )
                logger.info(f"User {user_id} subscription plan updated to {new_plan} (status: {status}).")
            else:
                UserManager(supabase)._execute_supabase_query(
                    supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_id),
                    "revert user subscription plan"
                )
                logger.info(f"User {user_id} subscription plan reverted to FREE (status: {status}).")
        else:
            logger.warning(f"User not found for Stripe customer ID: {customer_id} during subscription webhook.")

    elif event_type == 'customer.subscription.deleted':
        customer_id = data_object.get('customer')
        user_res = UserManager(supabase)._execute_supabase_query(
            supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single(),
            "fetch user for deleted subscription webhook"
        )
        if user_res:
            UserManager(supabase)._execute_supabase_query(
                supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_res['user_id']),
                "revert user plan on subscription delete"
            )
            logger.info(f"User {user_res['user_id']} subscription deleted, reverted to FREE plan.")
        else:
            logger.warning(f"User not found for Stripe customer ID: {customer_id} during deleted subscription webhook.")
            
    elif event_type == 'payment_intent.succeeded':
        payment_intent = data_object
        user_id = payment_intent['metadata'].get('user_id')
        item_id = payment_intent['metadata'].get('item_id')
        
        if user_id and item_id:
            item_res = UserManager(supabase)._execute_supabase_query(
                supabase.table('shop_items').select('*').eq('id', int(item_id)).maybe_single(),
                "fetch item for payment intent succeeded"
            )
            if item_res:
                item = item_res
                shop_manager = ShopManager(supabase)
                await shop_manager._apply_item_effect(user_id, item, 'stripe', None, item['price_eur'])
                logger.info(f"Payment intent succeeded for user {user_id}, item {item['name']}. Effect applied.")
            else:
                logger.warning(f"Item {item_id} not found for successful payment intent for user {user_id}.")
        else:
            logger.warning(f"Missing user_id or item_id in metadata for payment_intent.succeeded: {payment_intent['metadata']}")

    else:
        logger.info(f"Unhandled Stripe event type: {event_type}")

    return Response(status_code=200)