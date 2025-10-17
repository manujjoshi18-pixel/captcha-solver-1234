# main.py
import os
import re
import json
import base64
import shutil
import asyncio
import logging
import sys
from typing import List, Optional
from datetime import datetime

import httpx
import git
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# ------------------------- Settings -------------------------
class Settings(BaseSettings):
    GEMINI_API_KEY: str = Field("", env="GEMINI_API_KEY")
    GITHUB_TOKEN: str = Field("", env="GITHUB_TOKEN")
    GITHUB_USERNAME: str = Field("", env="GITHUB_USERNAME")
    STUDENT_SECRET: str = Field("", env="STUDENT_SECRET")
    LOG_FILE_PATH: str = Field("logs/app.log", env="LOG_FILE_PATH")
    MAX_CONCURRENT_TASKS: int = Field(2, env="MAX_CONCURRENT_TASKS")
    KEEP_ALIVE_INTERVAL_SECONDS: int = Field(30, env="KEEP_ALIVE_INTERVAL_SECONDS")
    GITHUB_API_BASE: str = Field("https://api.github.com", env="GITHUB_API_BASE")
    GITHUB_PAGES_BASE: Optional[str] = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
if not settings.GITHUB_PAGES_BASE:
    settings.GITHUB_PAGES_BASE = f"https://{settings.GITHUB_USERNAME}.github.io"
print("Loaded secret:", settings.STUDENT_SECRET)

# ------------------------- Logging -------------------------
os.makedirs(os.path.dirname(settings.LOG_FILE_PATH), exist_ok=True)
logger = logging.getLogger("task_receiver")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.FileHandler(settings.LOG_FILE_PATH, mode="a", encoding="utf-8")
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
console_handler.setFormatter(fmt)
file_handler.setFormatter(fmt)
logger.handlers = []
logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.propagate = False

def flush_logs():
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        for h in logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
    except Exception:
        pass

# ------------------------- Models -------------------------
class Attachment(BaseModel):
    name: str
    url: str  # data URI or http(s) url

class TaskRequest(BaseModel):
    task: str
    email: str
    round: int
    brief: str
    evaluation_url: str
    nonce: str
    secret: str
    attachments: List[Attachment] = []

# ------------------------- App & Globals -------------------------
app = FastAPI(title="Automated Task Receiver & Processor")
background_tasks_list: List[asyncio.Task] = []
task_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_TASKS)
last_received_task: Optional[dict] = None
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent"

# ------------------------- Utility -------------------------
def verify_secret(secret_from_request: str) -> bool:
    return secret_from_request == settings.STUDENT_SECRET

def safe_makedirs(path: str):
    os.makedirs(path, exist_ok=True)

def remove_local_path(path: str):
    if os.path.exists(path):
        shutil.rmtree(path)

# ------------------------- FastAPI Endpoints -------------------------
@app.post("/ready")
async def receive_task(task_data: TaskRequest, request: Request):
    global last_received_task
    if not verify_secret(task_data.secret):
        raise HTTPException(status_code=401, detail="Unauthorized: Secret mismatch")

    last_received_task = {
        "task": task_data.task,
        "email": task_data.email,
        "round": task_data.round,
        "brief": task_data.brief[:250] + ("..." if len(task_data.brief) > 250 else ""),
        "time": datetime.utcnow().isoformat() + "Z"
    }

    # Placeholder for background processing (LLM + GitHub integration)
    async def background_task():
        logger.info(f"Processing task {task_data.task} round {task_data.round}")
        await asyncio.sleep(1)  # simulate work
        logger.info(f"Finished task {task_data.task}")

    bg_task = asyncio.create_task(background_task())
    bg_task.add_done_callback(lambda t: logger.info(f"Background task done: {t}"))
    background_tasks_list.append(bg_task)

    return JSONResponse(status_code=200, content={"status": "ready", "message": f"Task {task_data.task} received."})

@app.get("/")
async def root():
    return {"message": "Task Receiver Service running. POST /ready to submit."}

@app.get("/status")
async def get_status():
    global last_received_task, background_tasks_list
    background_tasks_list[:] = [t for t in background_tasks_list if not t.done()]
    return {
        "last_received_task": last_received_task,
        "running_background_tasks": len(background_tasks_list)
    }

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat() + "Z"}

@app.get("/logs")
async def get_logs(lines: int = 200):
    path = settings.LOG_FILE_PATH
    if not os.path.exists(path):
        return PlainTextResponse("Log file not found.", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        content = f.readlines()
    return PlainTextResponse("".join(content[-lines:]))

# ------------------------- Startup / Shutdown -------------------------
@app.on_event("startup")
async def startup_event():
    async def keep_alive():
        while True:
            logger.info("[KEEPALIVE] Service heartbeat")
            flush_logs()
            await asyncio.sleep(settings.KEEP_ALIVE_INTERVAL_SECONDS)
    asyncio.create_task(keep_alive())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("[SHUTDOWN] Waiting for background tasks to finish...")
    for t in background_tasks_list:
        if not t.done():
            t.cancel()
    await asyncio.sleep(0.5)
    flush_logs()
