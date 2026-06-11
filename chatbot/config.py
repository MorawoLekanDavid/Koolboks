import os
import logging

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("koolbuy")

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://koolbuy:koolbuy_secure_password_2026@localhost:5432/koolbuy"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
CHAT_TTL_STR = os.environ.get("REDIS_CHAT_TTL", "3600")
MAX_HISTORY_STR = os.environ.get("MAX_HISTORY_MESSAGES", "20")
LEAD_TTL_STR = os.environ.get("REDIS_LEAD_TTL", "86400")
WHATSAPP_CONTACT = os.environ.get("WHATSAPP_CONTACT", "+2348116402869")
ZAPIER_WEBHOOK = os.environ.get("ZAPIER_WEBHOOK", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "KoolbotAdmin2026")
BOT_RESPONSE_DELAY = int(os.environ.get("BOT_RESPONSE_DELAY_SECONDS", "10"))

WABA_ID = os.environ.get("WABA_ID", "")
REENGAGEMENT_TEMPLATE = os.environ.get("REENGAGEMENT_TEMPLATE", "")
REENGAGEMENT_TEMPLATE_LANG = os.environ.get("REENGAGEMENT_TEMPLATE_LANG", "en")

FOLLOW_UP_ENABLED = os.environ.get("FOLLOW_UP_ENABLED", "true").lower() == "true"
FOLLOW_UP_HOURS = int(os.environ.get("FOLLOW_UP_HOURS", "24"))
FOLLOW_UP_RECHECK_DAYS = int(os.environ.get("FOLLOW_UP_RECHECK_DAYS", "7"))
FOLLOW_UP_MESSAGE = os.environ.get(
    "FOLLOW_UP_MESSAGE",
    "Hi! \U0001F44B We noticed you were checking out our products earlier and wanted to follow up.\n\n"
    "Are you still interested? We're here to help you find the right solution — just reply "
    "and we'll pick up right where we left off! \U0001F60A",
)

CHAT_TTL = int(CHAT_TTL_STR) if CHAT_TTL_STR and CHAT_TTL_STR.isdigit() else 3600
MAX_HISTORY = int(MAX_HISTORY_STR) if MAX_HISTORY_STR and MAX_HISTORY_STR.isdigit() else 20
LEAD_TTL = int(LEAD_TTL_STR) if LEAD_TTL_STR and LEAD_TTL_STR.isdigit() else 86400
IDLE_THRESHOLD = 5 * 60

RATE_LIMIT = int(os.environ.get("RATE_LIMIT_MESSAGES", "50"))

HANDOFF_AUTO_RESET_HOURS = int(os.environ.get("HANDOFF_AUTO_RESET_HOURS", "8"))

WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "koolbuy_whatsapp_2026")
WHATSAPP_API_TOKEN = os.environ.get("WHATSAPP_API_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_URL = "https://graph.facebook.com/v19.0"

# chatbot/ -> project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_FILE = os.path.join(BASE_DIR, "system_prompt.txt")
KB_FILE = os.path.join(BASE_DIR, "knowledge_base.txt")


def load_text_file(path: str, label: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        log.info(f"Loaded {label} from {path} ({len(content)} chars)")
        return content
    except FileNotFoundError:
        log.warning(f"{label} not found at {path}")
        return ""


SYSTEM_PROMPT_TEMPLATE = load_text_file(PROMPT_FILE, "system prompt")
KNOWLEDGE_BASE = load_text_file(KB_FILE, "knowledge base")
