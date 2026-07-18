import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chatbot import database
from chatbot.core import redis_client
from chatbot.routers import (
    admin_auth,
    ai_settings,
    analytics,
    chat,
    conversations,
    followup,
    leads,
    misc,
    products,
    templates,
    webhook,
)
from chatbot.workers.follow_up import follow_up_worker
from chatbot.workers.reengagement import reengagement_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_database()
    await redis_client.connect()

    fu_task = asyncio.create_task(follow_up_worker())
    re_task = asyncio.create_task(reengagement_worker())

    yield

    for task in (fu_task, re_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await redis_client.disconnect()
    database.dispose()


app = FastAPI(title="Koolbuy Chatbot API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(misc.router)
app.include_router(chat.router)
app.include_router(webhook.router)
app.include_router(admin_auth.router)
app.include_router(conversations.router)
app.include_router(leads.router)
app.include_router(products.router)
app.include_router(templates.router)
app.include_router(analytics.router)
app.include_router(followup.router)
app.include_router(ai_settings.router)
