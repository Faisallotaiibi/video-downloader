from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import uuid
import os

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
        filename = f"/tmp/{uuid.uuid4()}.mp4"
        ydl_opts = {
            'outtmpl': filename,
            'format': 'best[ext=mp4]/best',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])
        
        if os.path.exists(filename):
            return {"download_url": f"/file/{os.path.basename(filename)}"}
        return {"error": "فشل التحميل"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
def root():
    return {"status": "running"}
