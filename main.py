from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import threading

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: str

@app.post("/download")
def download(req: VideoRequest):
    try:
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'noplaylist': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            video_url = info.get('url') or info['formats'][-1]['url']
            title = info.get('title', 'video')
            return {"download_url": video_url, "title": title}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.get("/")
def root():
    return {"status": "running"}
