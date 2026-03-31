"""
Optional WhatsApp Integration Webhook Handler
Place this in a separate app or add to main app.py when ready to integrate

Usage:
1. Create WhatsApp Business Account at business.facebook.com
2. Set webhook URL to: https://your-domain.com/whatsapp-webhook
3. Subscribe to messages event
4. Verify webhook token matches WEBHOOK_TOKEN env var
"""

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import httpx
import os
import logging
import json
from datetime import datetime

log = logging.getLogger('whatsapp-webhook')

WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "your_webhook_token_here")
API_BASE_URL = os.environ.get("API_URL", "http://localhost:8000")


class WhatsAppMessage(BaseModel):
    from_: str  # Phone number
    type: str   # text, image, document, etc.
    text: str = None
    timestamp: str = None


async def send_whatsapp_message(phone: str, message: str, access_token: str):
    """Send a message via WhatsApp Business API"""
    endpoint = f"https://graph.instagram.com/v18.0/YOUR_PHONE_NUMBER_ID/messages"

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": True,
            "body": message
        }
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(endpoint, json=payload, headers=headers)
        return response.json()


async def process_whatsapp_webhook(request: Request) -> dict:
    """
    Process incoming WhatsApp webhook events

    Webhook payloads from WhatsApp Business API look like:
    {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "1234567890",
                        "id": "msg-id",
                        "timestamp": "1234567890",
                        "text": {"body": "User message"},
                        "type": "text"
                    }]
                }
            }]
        }]
    }
    """
    data = await request.json()

    try:
        # Extract message data
        entry = data.get("entry", [{}])[0]
        change = entry.get("changes", [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "ok", "message": "No messages"}

        msg = messages[0]
        phone = msg.get("from")
        text = msg.get("text", {}).get("body", "")
        msg_id = msg.get("id")

        log.info(f"WhatsApp message from {phone}: {text}")

        # Call your Koolbuy chatbot API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{API_BASE_URL}/chat",
                json={
                    "session_id": f"whatsapp-{phone}",
                    "message": text,
                    "user_name": f"WhatsApp-{phone[-4:]}"
                },
                timeout=30.0
            )

            if response.status_code != 200:
                log.error(f"Chat API error: {response.text}")
                return {"status": "error"}

            chat_response = response.json()
            bot_message = chat_response.get(
                "response", "I couldn't process that.")

            # Send response back via WhatsApp
            # Note: You'll need to implement your WhatsApp API integration
            # with proper access token and phone number ID
            log.info(f"Sending to {phone}: {bot_message}")

            return {
                "status": "ok",
                "phone": phone,
                "message_id": msg_id,
                "bot_response": bot_message
            }

    except Exception as e:
        log.error(f"Webhook error: {e}")
        return {"status": "error", "error": str(e)}


async def verify_webhook(challenge: str, verify_token: str) -> str:
    """Verify webhook token with WhatsApp (GET request)"""
    if verify_token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    return challenge


# Add these endpoints to your main FastAPI app:
"""
@app.get("/whatsapp-webhook")
async def verify_whatsapp_webhook(hub_mode: str, hub_challenge: str, hub_verify_token: str):
    return await verify_webhook(hub_challenge, hub_verify_token)

@app.post("/whatsapp-webhook")
async def handle_whatsapp_webhook(request: Request):
    result = await process_whatsapp_webhook(request)
    return result
"""


if __name__ == "__main__":
    # Test webhook signature verification
    print("WhatsApp webhook handler ready")
    print(f"Webhook token: {WEBHOOK_TOKEN}")
    print(f"API base URL: {API_BASE_URL}")
