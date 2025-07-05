import os
from datetime import datetime, timezone, timedelta
import json
from enum import Enum
from typing import Literal, Dict, Any, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import vertexai
from vertexai.generative_models import GenerativeModel, Part, Image

# Import per PostgreSQL diretto
import psycopg2
from psycopg2 import Error as Psycopg2Error
from psycopg2.extras import DictCursor # Per ottenere risultati come dizionari

# --- Initial Configuration ---
load_dotenv()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
# DATABASE_URL sarà fornito da Railway/Neon
DATABASE_URL = os.environ.get("DATABASE_URL")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")

# --- Service Initialization ---
app = FastAPI(title="Zenith Rewards Backend", description="Backend per la gestione di utenti, AI, pagamenti e gamification per Zenith Rewards.")

gemini_flash_model = None
gemini_pro_vision_model = None
vertexai_initialized = False

# Configura il logging
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        sa_key_path = "/tmp/gcp_sa_key.json" # Use /tmp for Render ephemeral storage
        with open(sa_key_path, "w") as f:
            f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_key_path

        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        gemini_flash_model = GenerativeModel("gemini-1.5-flash")
        gemini_pro_vision_model = GenerativeModel("gemini-pro-vision")
        vertexai_initialized = True
        logger.info("Vertex AI initialized successfully.")
    except Exception as e:
        logger.error(f"WARNING: Vertex AI configuration error: {e}. AI functionalities might be limited or unavailable.", exc_info=True)
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

# --- Database Connection (PostgreSQL with psycopg2) ---
def get_pg_connection():
    """Provides a psycopg2 connection to PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Psycopg2Error as e:
        logger.critical(f"Failed to connect to PostgreSQL database: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database connection failed: {e}")
    except Exception as e:
        logger.critical(f"Unexpected error during DB connection: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

def _execute_pg_query(sql_query: str, params: Optional[Tuple] = None, fetch_one: bool = False, fetch_all: bool = False, error_context: str = "database operation"):
    """
    Executes a PostgreSQL query and handles transactions.
    Returns fetched data or None. Raises HTTPException on error.
    """
    conn = None
    cursor = None
    try:
        conn = get_pg_connection()
        cursor = conn.cursor(cursor_factory=DictCursor) # Use DictCursor for dictionary results
        logger.debug(f"Executing SQL: {sql_query} with params: {params}")

        if params:
            cursor.execute(sql_query, params)
        else:
            cursor.execute(sql_query)

        conn.commit() # Commit transaction for DML operations (INSERT, UPDATE, DELETE)

        if fetch_one:
            return cursor.fetchone()
        if fetch_all:
            return cursor.fetchall()
        return None # For INSERT/UPDATE/DELETE that don't need results

    except Psycopg2Error as e:
        if conn:
            conn.rollback() # Rollback on error
        logger.error(f"PostgreSQL Error ({error_context}): Code={e.pgcode}, Message={e.pgerror.strip()}", exc_info=True)
        # Rilancia un errore HTTP generico o più specifico se il codice errore lo permette
        raise HTTPException(status_code=400, detail=f"Database error ({error_context}): {e.pgerror.strip()}")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Unexpected error during PostgreSQL operation ({error_context}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error during {error_context}.")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# --- Managers (Adapted for psycopg2) ---

class UserManager:
    def __init__(self): pass

    def sync_user(self, user_data: UserSyncRequest):
        now = datetime.now(timezone.utc)
        logger.info(f"Attempting to sync user: {user_data.user_id}")
        
        user_record = _execute_pg_query(
            "SELECT user_id, last_login_at, login_streak, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date FROM users WHERE user_id = %s",
            (user_data.user_id,), fetch_one=True, error_context="user sync fetch"
        )
        
        if not user_record:
            logger.info(f"Creating new user: {user_data.user_id}")
            _execute_pg_query(
                """
                INSERT INTO users (user_id, email, display_name, referrer_id, avatar_url, login_streak, last_login_at, points_balance, pending_points_balance, subscription_plan, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_data.user_id, user_data.email, user_data.displayName, user_data.referrer_id, user_data.avatar_url, 1, now, 0, 0, SubscriptionPlan.FREE.value, 0, now, 0, now, now),
                error_context="insert new user"
            )
            logger.info(f"New user {user_data.user_id} created successfully.")
        else:
            logger.info(f"Updating existing user: {user_data.user_id}")
            # Ensure datetime objects are passed, psycopg2 handles conversion
            last_login_dt = user_record['last_login_at'] if user_record['last_login_at'] else now
            current_streak = user_record['login_streak'] if user_record['login_streak'] is not None else 0
            
            new_streak = 1
            if last_login_dt:
                last_login_date_only = last_login_dt.date()
                today = now.date()
                days_diff = (today - last_login_date_only).days

                if days_diff == 1:
                    new_streak = current_streak + 1
                elif days_diff == 0:
                    new_streak = current_streak
            
            update_data_params = [now, new_streak] # Order matters for SQL query
            
            daily_ai_generations_used_val = user_record['daily_ai_generations_used']
            last_generation_reset_date_val = user_record['last_generation_reset_date']

            if now.date() > (last_generation_reset_date_val.date() if last_generation_reset_date_val else now.date()):
                daily_ai_generations_used_val = 0
                last_generation_reset_date_val = now
            
            update_data_params.extend([daily_ai_generations_used_val, last_generation_reset_date_val])

            daily_votes_used_val = user_record['daily_votes_used']
            last_vote_reset_date_val = user_record['last_vote_reset_date']
            if now.date() > (last_vote_reset_date_val.date() if last_vote_reset_date_val else now.date()):
                daily_votes_used_val = 0
                last_vote_reset_date_val = now

            update_data_params.extend([daily_votes_used_val, last_vote_reset_date_val])
            update_data_params.append(user_data.user_id) # Final parameter for WHERE clause

            _execute_pg_query(
                "UPDATE users SET last_login_at = %s, login_streak = %s, daily_ai_generations_used = %s, last_generation_reset_date = %s, daily_votes_used = %s, last_vote_reset_date = %s WHERE user_id = %s",
                tuple(update_data_params), error_context="update existing user"
            )
            logger.info(f"User {user_data.user_id} updated successfully.")
        return {"status": "success"}

    def update_profile(self, user_id: str, profile_data: UserProfileUpdate):
        logger.info(f"Updating profile for user: {user_id}")
        update_payload_items = []
        update_values = []
        if profile_data.display_name is not None:
            update_payload_items.append("display_name = %s")
            update_values.append(profile_data.display_name)
        if profile_data.avatar_url is not None:
            update_payload_items.append("avatar_url = %s")
            update_values.append(profile_data.avatar_url)
        
        if not update_payload_items: 
            logger.warning(f"No data provided for profile update for user {user_id}")
            raise HTTPException(status_code=400, detail="No data provided to update.")
        
        sql_query = f"UPDATE users SET {', '.join(update_payload_items)} WHERE user_id = %s"
        update_values.append(user_id)

        _execute_pg_query(sql_query, tuple(update_values), error_context="update user profile")
        logger.info(f"Profile for user {user_id} updated successfully.")
        return {"status": "success", "message": "Profile updated successfully."}

    def get_user_balance(self, user_id: str):
        logger.info(f"Fetching balance for user: {user_id}")
        user_record = _execute_pg_query(
            "SELECT points_balance, pending_points_balance FROM users WHERE user_id = %s",
            (user_id,), fetch_one=True, error_context="fetch user balance"
        )
        if not user_record:
            logger.warning(f"User {user_id} not found when fetching balance.")
            raise HTTPException(status_code=404, detail="User not found.")
        return user_record

    def get_user_profile(self, user_id: str):
        logger.info(f"Fetching profile for user: {user_id}")
        user_record = _execute_pg_query(
            "SELECT subscription_plan, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date, points_balance, stripe_customer_id FROM users WHERE user_id = %s",
            (user_id,), fetch_one=True, error_context="fetch user profile"
        )
        if not user_record:
            logger.warning(f"User {user_id} not found when fetching profile. Returning default data.")
            return {
                "subscription_plan": SubscriptionPlan.FREE.value,
                "daily_ai_generations_used": 0,
                "last_generation_reset_date": datetime.now(timezone.utc).isoformat(),
                "daily_votes_used": 0,
                "last_vote_reset_date": datetime.now(timezone.utc).isoformat(),
                "points_balance": 0,
                "stripe_customer_id": None # Add stripe_customer_id to default
            }
        # Convert datetime objects to ISO format strings for consistency
        user_record['last_generation_reset_date'] = user_record['last_generation_reset_date'].isoformat() if user_record['last_generation_reset_date'] else None
        user_record['last_vote_reset_date'] = user_record['last_vote_reset_date'].isoformat() if user_record['last_vote_reset_date'] else None
        # Ensure subscription_plan and other nullable fields are handled
        user_record['subscription_plan'] = user_record['subscription_plan'] if user_record['subscription_plan'] else SubscriptionPlan.FREE.value
        user_record['stripe_customer_id'] = user_record['stripe_customer_id']
        return user_record

    def get_streak_status(self, user_id: str):
        logger.info(f"Fetching streak status for user: {user_id}")
        user_record = _execute_pg_query(
            "SELECT login_streak FROM users WHERE user_id = %s",
            (user_id,), fetch_one=True, error_context="fetch streak status"
        )
        if not user_record:
            logger.warning(f"User {user_id} not found when fetching streak. Returning 0.")
            return {"login_streak": 0}
        
        login_streak = user_record['login_streak'] if user_record['login_streak'] is not None else 0
        return {"login_streak": login_streak}

    def claim_streak_reward(self, user_id: str):
        logger.info(f"Attempting to claim streak reward for user: {user_id}")
        # Call the SQL function directly
        result_data = _execute_pg_query(
            "SELECT claim_streak_reward(%s) AS result",
            (user_id,), fetch_one=True, error_context="claim streak RPC"
        )
        if result_data and result_data['result']: # The function returns jsonb
            return result_data['result']
        logger.error(f"Unexpected RPC return for claim_streak_reward for user {user_id}: {result_data}")
        raise HTTPException(status_code=400, detail="Failed to claim streak reward. Check database function logs for details.")

    def get_referral_stats(self, user_id: str):
        logger.info(f"Fetching referral stats for user: {user_id}")
        referral_count_res = _execute_pg_query(
            "SELECT COUNT(*) FROM users WHERE referrer_id = %s",
            (user_id,), fetch_one=True, error_context="fetch referral count"
        )
        referral_count = referral_count_res['count'] if referral_count_res and 'count' in referral_count_res else 0

        referral_earnings = referral_count * 100
        logger.info(f"Referral stats for {user_id}: count={referral_count}, earnings={referral_earnings}")
        return {"referral_count": referral_count, "referral_earnings": referral_earnings}

class AIManager:
    def __init__(self): pass

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

        user_manager = UserManager() # Create manager instance for profile access
        user_profile = user_manager.get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        generations_used = user_profile.get('daily_ai_generations_used', 0)
        last_reset_dt = datetime.fromisoformat(user_profile.get('last_generation_reset_date', datetime.now(timezone.utc).isoformat()))
        
        if datetime.now(timezone.utc).date() > last_reset_dt.date():
            generations_used = 0
            _execute_pg_query(
                "UPDATE users SET daily_ai_generations_used = %s, last_generation_reset_date = %s WHERE user_id = %s",
                (0, datetime.now(timezone.utc), req.user_id), error_context="reset daily AI generations"
            )

        if generations_used >= self.AI_GENERATION_LIMITS.get(user_plan, 0):
            logger.warning(f"User {req.user_id} exceeded AI generation limit for plan {user_plan.value}")
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite di generazioni AI giornaliere ({self.AI_GENERATION_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'. Effettua l'upgrade per più generazioni!")

        final_prompt = f"Given the goal '{req.prompt}', provide 3 brief, impactful tips."
        if user_plan == SubscriptionPlan.PREMIUM:
            final_prompt = f"Act as a business strategy expert. Given the goal '{req.prompt}', create a detailed 5-7 point action plan with practical examples and suggestions for marketing and social media."
        elif user_plan == SubscriptionPlan.ASSISTANT:
            final_prompt = f"You are a world-class business mentor and an expert in digital marketing, dropshipping, trading, and social media. Given the goal '{req.prompt}', create an extremely detailed and personalized step-by-step strategy, including specific tactics to scale both Zenith Rewards platform's social features and external social media, dropshipping, trading, and e-commerce tips, and a comprehensive virality plan. Your response must be complete, actionable, and cover all requested facets."
        
        try:
            logger.info(f"Generating AI advice for user {req.user_id} with prompt: {req.prompt[:50]}...")
            response_ai = gemini_flash_model.generate_content(final_prompt)
            generated_text = response_ai.text.strip()

            _execute_pg_query(
                "UPDATE users SET daily_ai_generations_used = %s WHERE user_id = %s",
                (generations_used + 1, req.user_id), error_context="increment daily AI generations"
            )
            logger.info(f"AI advice generated and usage incremented for user {req.user_id}.")
            return {"advice": generated_text}
        except Exception as e:
            logger.error(f"Error during AI advice generation for user {req.user_id}: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=f"AI service error: {e}. Please try again later.")

    async def generate_content(self, req: AIGenerationRequest):
        if not vertexai_initialized:
            logger.error("AI service is not available for content generation.")
            raise HTTPException(status_code=503, detail="AI service is not available.")

        user_manager = UserManager()
        user_profile = user_manager.get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        
        cost = self.get_ai_cost(user_plan)
        
        if req.payment_method == 'points':
            if user_profile['points_balance'] < cost['points']:
                logger.warning(f"User {req.user_id} has insufficient points ({user_profile['points_balance']}) for AI content generation (needed {cost['points']}).")
                raise HTTPException(status_code=402, detail="Punti insufficienti. Hai bisogno di {cost['points']} ZC.")
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
                image_response = gemini_pro_vision_model.generate_content(f"Create a short text description and visual suggestion for an image based on: '{req.prompt}'.")
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
                strategy_response = gemini_flash_model.generate_content(f"Expand the virality plan for '{req.prompt}' and '{req.content_type.value}' with 3-5 digital marketing strategies and social engagement tips. Highlight keywords.")
                ai_strategy_plan = strategy_response.text.strip()
            elif user_plan == SubscriptionPlan.ASSISTANT:
                strategy_response = gemini_flash_model.generate_content(f"Act as an expert marketing consultant. Create a DETAILED ADVANCED VIRAL PLAN for the content '{req.prompt}' ({req.content_type.value}), including target analysis, distribution channels (Zenith Rewards and external social media), suggested publication calendar, collaboration ideas, SEO/hashtag optimization, and results measurement. Think like a growth hacker.")
                ai_strategy_plan = strategy_response.text.strip()

            content_id_row = _execute_pg_query(
                """
                INSERT INTO ai_contents (user_id, contest_id, prompt, content_type, generated_url, generated_text, ai_strategy_plan, is_published, votes, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (req.user_id, req.contest_id, req.prompt, req.content_type.value, generated_url, generated_text, ai_strategy_plan, False, 0, datetime.now(timezone.utc)),
                fetch_one=True, error_context="insert AI content"
            )
            ai_content_id = content_id_row['id'] if content_id_row else None
            
            if not ai_content_id:
                logger.error("Failed to retrieve ID of generated AI content after insertion.")
                raise Exception("Failed to retrieve ID of generated AI content.")

            if req.payment_method == 'points':
                _execute_pg_query(
                    "SELECT deduct_points(%s, %s, %s) AS result", # Call SQL function
                    (req.user_id, cost['points'], f'AI Generation - {req.content_type.value}'),
                    error_context="deduct points for AI generation"
                )
            
            _execute_pg_query(
                "UPDATE users SET daily_ai_generations_used = daily_ai_generations_used + 1, last_content_generated_id = %s WHERE user_id = %s",
                (ai_content_id, req.user_id), error_context="increment AI generations usage"
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

        except Exception as e:
            logger.error(f"Error during AI content generation for user {req.user_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Errore durante la generazione AI: {e}. Riprova più tardi.")

    def publish_ai_content(self, ai_content_id: int):
        logger.info(f"Publishing AI content: {ai_content_id}")
        _execute_pg_query(
            "UPDATE ai_contents SET is_published = TRUE WHERE id = %s",
            (ai_content_id,), error_context="publish AI content"
        )
        logger.info(f"AI content {ai_content_id} published successfully.")
        return {"status": "success", "message": "Content published successfully."}

    async def get_feed(self):
        logger.info("Fetching AI content feed.")
        response = _execute_pg_query(
            """
            SELECT
                ac.id, ac.user_id, ac.contest_id, ac.prompt, ac.content_type, ac.generated_url, ac.generated_text, ac.ai_strategy_plan, ac.votes, ac.created_at,
                u.display_name, u.avatar_url
            FROM ai_contents ac
            JOIN users u ON ac.user_id = u.user_id
            WHERE ac.is_published = TRUE
            ORDER BY ac.votes DESC, ac.created_at DESC
            LIMIT 50
            """,
            fetch_all=True, error_context="fetch AI content feed"
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
                        "display_name": item['display_name'],
                        "avatar_url": item['avatar_url']
                    }
                })
        logger.info(f"Fetched {len(formatted_feed)} items for AI content feed.")
        return formatted_feed

    async def vote_content(self, content_id: int, user_id: str):
        logger.info(f"User {user_id} attempting to vote for content {content_id}.")
        user_manager = UserManager()
        user_profile = user_manager.get_user_profile(user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        daily_votes_used = user_profile.get('daily_votes_used', 0)
        last_vote_reset_dt = datetime.fromisoformat(user_profile.get('last_vote_reset_date', datetime.now(timezone.utc).isoformat()))

        if datetime.now(timezone.utc).date() > last_vote_reset_dt.date():
            daily_votes_used = 0
            _execute_pg_query(
                "UPDATE users SET daily_votes_used = %s, last_vote_reset_date = %s WHERE user_id = %s",
                (0, datetime.now(timezone.utc), user_id), error_context="reset daily votes"
            )

        if daily_votes_used >= self.DAILY_VOTE_LIMITS.get(user_plan, 0):
            logger.warning(f"User {user_id} exceeded daily vote limit for plan {user_plan.value}.")
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite giornaliero di voti ({self.DAILY_VOTE_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'.")

        existing_vote = _execute_pg_query(
            "SELECT id FROM votes WHERE user_id = %s AND content_id = %s",
            (user_id, content_id), fetch_one=True, error_context="check existing vote"
        )
        if existing_vote:
            logger.warning(f"User {user_id} already voted for content {content_id}.")
            raise HTTPException(status_code=400, detail="Hai già votato questo contenuto.")

        content_owner_res = _execute_pg_query(
            "SELECT user_id FROM ai_contents WHERE id = %s",
            (content_id,), fetch_one=True, error_context="get content owner"
        )
        if content_owner_res and content_owner_res['user_id'] == user_id:
            logger.warning(f"User {user_id} tried to vote for their own content {content_id}.")
            raise HTTPException(status_code=400, detail="Non puoi votare il tuo stesso contenuto.")

        _execute_pg_query(
            "INSERT INTO votes (user_id, content_id, voted_at) VALUES (%s, %s, %s)",
            (user_id, content_id, datetime.now(timezone.utc)), error_context="insert new vote"
        )
        _execute_pg_query(
            "SELECT increment_content_votes(%s) AS result",
            (content_id,), error_context="increment content votes RPC"
        )

        _execute_pg_query(
            "UPDATE users SET daily_votes_used = %s WHERE user_id = %s",
            (daily_votes_used + 1, user_id), error_context="increment daily votes usage"
        )
        logger.info(f"User {user_id} successfully voted for content {content_id}.")
        return {"status": "success", "message": "Voto registrato con successo!"}

class ContestManager:
    def __init__(self): pass

    CONTEST_REWARD_POOLS = {
        SubscriptionPlan.FREE: 10.00,
        SubscriptionPlan.PREMIUM: 30.00,
        SubscriptionPlan.ASSISTANT: 60.00
    }

    def get_current_contest(self, user_plan: SubscriptionPlan):
        logger.info(f"Fetching current contest for plan: {user_plan.value}")
        now = datetime.now(timezone.utc)
        result = _execute_pg_query(
            """
            SELECT id, theme_prompt, start_date, end_date, reward_pool_euro, min_plan_access, created_at
            FROM contests
            WHERE start_date <= %s AND end_date >= %s AND %s = ANY(min_plan_access)
            ORDER BY end_date ASC
            LIMIT 1
            """,
            (now, now, user_plan.value), fetch_one=True, error_context=f"fetch current contest for plan {user_plan.value}"
        )
        
        if result:
            result['reward_pool_euro'] = self.CONTEST_REWARD_POOLS.get(user_plan, 0.00)
            # Convert datetime objects to ISO format strings for JSON serialization
            result['start_date'] = result['start_date'].isoformat() if result['start_date'] else None
            result['end_date'] = result['end_date'].isoformat() if result['end_date'] else None
            result['created_at'] = result['created_at'].isoformat() if result['created_at'] else None
            logger.info(f"Found active contest: {result['theme_prompt']} for plan {user_plan.value}")
            return result
        logger.info(f"No active contest found for plan {user_plan.value}.")
        return None

    def get_leaderboard(self):
        logger.info("Fetching leaderboard.")
        response_data = _execute_pg_query(
            "SELECT display_name, avatar_url, points_balance FROM users ORDER BY points_balance DESC LIMIT 100",
            fetch_all=True, error_context="fetch leaderboard"
        )
        logger.info(f"Fetched {len(response_data) if response_data else 0} users for leaderboard.")
        return response_data if response_data else []

class ShopManager:
    def __init__(self): pass

    def get_shop_items(self):
        logger.info("Fetching shop items.")
        response_data = _execute_pg_query(
            "SELECT id, name, description, price_points, price_eur, item_type, effect, image_url, is_active, created_at FROM shop_items ORDER BY price_points ASC",
            fetch_all=True, error_context="fetch shop items"
        )
        logger.info(f"Fetched {len(response_data) if response_data else 0} shop items.")
        
        # Convert datetime objects and ensure JSON is properly loaded/formatted
        if response_data:
            for item in response_data:
                if 'effect' in item and item['effect'] is not None and not isinstance(item['effect'], dict):
                    # psycopg2 DictCursor should handle JSONB directly, but add safeguard
                    item['effect'] = json.loads(item['effect'])
                if 'created_at' in item and item['created_at'] is not None:
                    item['created_at'] = item['created_at'].isoformat()
        
        return response_data if response_data else []

    async def buy_item(self, req: ShopBuyRequest):
        logger.info(f"User {req.user_id} attempting to buy item {req.item_id} with {req.payment_method}.")
        user_manager = UserManager()
        user_profile = user_manager.get_user_profile(req.user_id)
        
        item = _execute_pg_query(
            "SELECT id, name, description, price_points, price_eur, item_type, effect, image_url, is_active FROM shop_items WHERE id = %s",
            (req.item_id,), fetch_one=True, error_context=f"fetch shop item {req.item_id}"
        )
        
        if not item: 
            logger.warning(f"Item {req.item_id} not found for purchase by user {req.user_id}.")
            raise HTTPException(status_code=404, detail="Item not found.")

        if req.payment_method == 'points':
            if user_profile['points_balance'] < item['price_points']:
                logger.warning(f"User {req.user_id} has insufficient points ({user_profile['points_balance']}) to buy item {req.item_id} (needed {item['price_points']}).")
                raise HTTPException(status_code=402, detail="Punti insufficienti per l'acquisto.")
            
            _execute_pg_query(
                "SELECT deduct_points(%s, %s, %s) AS result", # Call SQL function
                (req.user_id, item['price_points'], f'Shop Purchase: {item["name"]} (Points)'),
                fetch_one=True, error_context="deduct points for shop item"
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
                logger.error(f"Stripe error during Payment Intent creation: {e.user_message}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Errore Stripe: {e.user_message}")
            except Exception as e:
                logger.error(f"Unexpected error creating Payment Intent: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Errore nella creazione del Payment Intent: {e}")

    async def _apply_item_effect(self, user_id: str, item: dict, payment_method: str, amount_points: float | None, amount_eur: float | None):
        logger.info(f"Applying effect for item {item['name']} to user {user_id}. Type: {item['item_type']}")
        
        if item['item_type'] == ItemType.BOOST.value:
            effect_data = item.get('effect', {})
            multiplier = effect_data.get('multiplier', 1.0)
            duration_hours = effect_data.get('duration_hours', 0)
            logger.info(f"Boost '{item['name']}' ({multiplier}x for {duration_hours}h) applied to user {user_id}. Actual effect implementation needed!")
        
        elif item['item_type'] == ItemType.COSMETIC.value:
            logger.info(f"Cosmetic '{item['name']}' applied to user {user_id}. Effect: {item.get('effect')}")
        
        elif item['item_type'] == ItemType.GENERATION_PACK.value:
            effect_data = item.get('effect', {})
            generations_to_add = effect_data.get('generations', 0)
            if generations_to_add > 0:
                user_res = _execute_pg_query(
                    "SELECT daily_ai_generations_used FROM users WHERE user_id = %s",
                    (user_id,), fetch_one=True, error_context="fetch user daily generations for item effect"
                )
                if user_res:
                    current_generations_used = user_res['daily_ai_generations_used']
                    _execute_pg_query(
                        "UPDATE users SET daily_ai_generations_used = %s WHERE user_id = %s",
                        (current_generations_used - generations_to_add, user_id), error_context="update user daily generations with item effect"
                    )
                    logger.info(f"Added {generations_to_add} AI generations to user {user_id}.")

        _execute_pg_query(
            """
            INSERT INTO user_purchases (user_id, item_id, purchase_date, payment_method, amount_paid_points, amount_paid_eur, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, item['id'], datetime.now(timezone.utc), payment_method, amount_points, amount_eur, 'completed', datetime.now(timezone.utc)),
            error_context="log user purchase"
        )
        logger.info(f"Item effect for {item['name']} applied and purchase logged for user {user_id}.")

def get_user_manager(): return UserManager()
def get_ai_manager(): return AIManager()
def get_contest_manager(): return ContestManager()
def get_shop_manager(): return ShopManager()

@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend is operational. Access the API documentation at /docs."}

@app.post("/sync_user")
def sync_user_endpoint(user_data: UserSyncRequest, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.sync_user(user_data)
    except HTTPException as e:
        logger.error(f"HTTPException in sync_user_endpoint: {e.detail}", exc_info=True)
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
        # Call the SQL function directly using _execute_pg_query
        result = _execute_pg_query(
            "SELECT request_payout_function(%s, %s, %s, %s, %s) AS result",
            (payout_data.user_id, payout_data.points_amount, payout_data.points_amount / POINTS_TO_EUR_RATE, payout_data.method, payout_data.address),
            fetch_one=True, error_context="request payout RPC"
        )
        
        if result and result['result']:
            logger.info(f"Payout request successful for user {payout_data.user_id}.")
            return {"status": "success", "message": result['result'].get('message', "Your payout request has been sent and will be processed soon!")}
        
        logger.error(f"Unexpected RPC return for request_payout_function for user {payout_data.user_id}: {result}")
        raise HTTPException(status_code=400, detail="Failed to process payout request. Check database function logs.")

    except HTTPException as e:
        if 'Punti insufficienti' in e.detail:
            raise HTTPException(status_code=402, detail="Punti insufficienti per il prelievo.")
        logger.error(f"HTTPException in request_payout_endpoint: {e.detail}", exc_info=True)
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
def get_current_contest_endpoint(user_id: str, contest_manager: ContestManager = Depends(get_contest_manager)):
    try:
        user_manager = UserManager() # Instance created here
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
def create_checkout_session_endpoint(req: CreateSubscriptionRequest):
    if not stripe.api_key: raise HTTPException(status_code=500, detail="Stripe not configured.")
    price_map = {
        'premium': STRIPE_PRICE_ID_PREMIUM,
        'assistant': STRIPE_PRICE_ID_ASSISTANT
    }
    price_id = price_map.get(req.plan_type)
    if not price_id: raise HTTPException(status_code=400, detail="Invalid plan type specified.")
    
    try:
        user_manager = UserManager()
        user_profile_data = user_manager.get_user_profile(req.user_id) # This call ensures DB access works
        user_email = user_profile_data.get('email') # Assuming email is retrieved by get_user_profile
        user_stripe_customer_id = user_profile_data.get('stripe_customer_id')

        customer_id = user_stripe_customer_id
        
        if not customer_id:
            logger.info(f"Creating new Stripe customer for user {req.user_id}.")
            customer = stripe.Customer.create(
                email=user_email,
                metadata={'user_id': req.user_id}
            )
            customer_id = customer.id
            _execute_pg_query(
                "UPDATE users SET stripe_customer_id = %s WHERE user_id = %s",
                (customer_id, req.user_id), error_context="update user with Stripe customer ID"
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
async def stripe_webhook(request: Request):
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
        logger.error(f"Unexpected error processing Stripe webhook event: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    event_type = event['type']
    data_object = event['data']['object']
    logger.info(f"Received Stripe webhook event type: {event_type}")

    # Use direct _execute_pg_query or UserManager/ShopManager methods that wrap it
    user_manager = UserManager() 
    shop_manager = ShopManager()

    if event_type == 'customer.subscription.created' or event_type == 'customer.subscription.updated':
        subscription = data_object
        customer_id = subscription.get('customer')
        price_id = subscription['items']['data'][0]['price']['id']
        status = subscription.get('status')
        
        user_res = _execute_pg_query(
            "SELECT user_id FROM users WHERE stripe_customer_id = %s",
            (customer_id,), fetch_one=True, error_context="fetch user for subscription webhook"
        )
        
        if user_res:
            user_id = user_res['user_id']
            new_plan = SubscriptionPlan.FREE.value
            
            if price_id == STRIPE_PRICE_ID_PREMIUM:
                new_plan = SubscriptionPlan.PREMIUM.value
            elif price_id == STRIPE_PRICE_ID_ASSISTANT:
                new_plan = SubscriptionPlan.ASSISTANT.value
            
            if status in ['active', 'trialing']:
                _execute_pg_query(
                    "UPDATE users SET subscription_plan = %s WHERE user_id = %s",
                    (new_plan, user_id), error_context="update user subscription plan"
                )
                logger.info(f"User {user_id} subscription plan updated to {new_plan} (status: {status}).")
            else:
                _execute_pg_query(
                    "UPDATE users SET subscription_plan = %s WHERE user_id = %s",
                    (SubscriptionPlan.FREE.value, user_id), error_context="revert user subscription plan"
                )
                logger.info(f"User {user_id} subscription plan reverted to FREE (status: {status}).")
        else:
            logger.warning(f"User not found for Stripe customer ID: {customer_id} during subscription webhook.")

    elif event_type == 'customer.subscription.deleted':
        customer_id = data_object.get('customer')
        user_res = _execute_pg_query(
            "SELECT user_id FROM users WHERE stripe_customer_id = %s",
            (customer_id,), fetch_one=True, error_context="fetch user for deleted subscription webhook"
        )
        if user_res:
            _execute_pg_query(
                "UPDATE users SET subscription_plan = %s WHERE user_id = %s",
                (SubscriptionPlan.FREE.value, user_res['user_id']), error_context="revert user plan on subscription delete"
            )
            logger.info(f"User {user_res['user_id']} subscription deleted, reverted to FREE plan.")
        else:
            logger.warning(f"User not found for Stripe customer ID: {customer_id} during deleted subscription webhook.")
            
    elif event_type == 'payment_intent.succeeded':
        payment_intent = data_object
        user_id = payment_intent['metadata'].get('user_id')
        item_id = payment_intent['metadata'].get('item_id')
        
        if user_id and item_id:
            item = _execute_pg_query(
                "SELECT id, name, description, price_points, price_eur, item_type, effect, image_url, is_active FROM shop_items WHERE id = %s",
                (int(item_id),), fetch_one=True, error_context="fetch item for payment intent succeeded"
            )
            if item:
                await shop_manager._apply_item_effect(user_id, item, 'stripe', None, item.get('price_eur'))
                logger.info(f"Payment intent succeeded for user {user_id}, item {item['name']}. Effect applied.")
            else:
                logger.warning(f"Item {item_id} not found for successful payment intent for user {user_id}.")
        else:
            logger.warning(f"Missing user_id or item_id in metadata for payment_intent.succeeded: {payment_intent['metadata']}")

    else:
        logger.info(f"Unhandled Stripe event type: {event_type}")

    return Response(status_code=200)
# Questo è un commento per forzare un nuovo deployment