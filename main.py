import os
import shutil
import zipfile
import sqlite3
import imagehash
import cv2
import numpy as np
from PIL import Image
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import uuid

app = FastAPI(title="Seletor Inteligente de Fotografias - API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "./storage/uploads"
EXPORT_DIR = "./storage/exports"
DB_PATH = "./storage/database.db"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

class PhotoSelectPayload(BaseModel):
    photo_id: str
    selected: bool

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            total_photos INTEGER DEFAULT 0,
            processed_photos INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS photos (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            filename TEXT,
            filepath TEXT,
            blur_score REAL,
            exposure_status TEXT,
            phash TEXT,
            recommendation TEXT,
            selected INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def analyze_photo(filepath: str):
    img_cv = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
    if img_cv is None:
        return 0.0, "Erro", ""
    
    h, w = img_cv.shape[:2]
    max_dim = 1024
    if h > max_dim or w > max_dim:
        scale = max_dim / float(max(h, w))
        img_cv = cv2.resize(img_cv, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    blur_score = cv2.Laplacian(img_cv, cv2.CV_64F).var()
    
    hist = cv2.calcHist([img_cv], [0], None, [256], [0, 256])
    total_pixels = img_cv.size
    under_exposed = np.sum(hist[:30]) / total_pixels
    over_exposed = np.sum(hist[225:]) / total_pixels
    
    exposure_status = "Equilibrada"
    if under_exposed > 0.30:
        exposure_status = "Subexposta"
    elif over_exposed > 0.30:
        exposure_status = "Superexposta"

    with Image.open(filepath) as pil_img:
        pil_img.thumbnail((512, 512))
        phash_val = str(imagehash.phash(pil_img))
    
    return round(blur_score, 2), exposure_status, phash_val

def process_session_photos(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, filepath FROM photos WHERE session_id = ?", (session_id,))
    photos = cursor.fetchall()
    
    for photo_id, filepath in photos:
        blur, exposure, phash = analyze_photo(filepath)
        recommendation = "Recomendada" if blur > 60.0 and exposure == "Equilibrada" else "Revisar"
        # Fotos recomendadas iniciam pré-selecionadas (1), foto para revisar iniciam desmarcadas (0)
        selected = 1 if recommendation == "Recomendada" else 0
        
        cursor.execute('''
            UPDATE photos 
            SET blur_score = ?, exposure_status = ?, phash = ?, recommendation = ?, selected = ?
            WHERE id = ?
        ''', (blur, exposure, phash, recommendation, selected, photo_id))
        
        cursor.execute("UPDATE sessions SET processed_photos = processed_photos + 1 WHERE id = ?", (session_id,))
        conn.commit()
        
    cursor.execute("UPDATE sessions SET status = 'Concluída' WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

@app.post("/api/sessions")
def create_session(title: str = "Nova Análise"):
    session_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO sessions (id, title, status) VALUES (?, ?, ?)", (session_id, title, "Criada"))
    conn.commit()
    conn.close()
    return {"session_id": session_id, "title": title}

@app.post("/api/sessions/{session_id}/upload")
def upload_photos(session_id: str, background_tasks: BackgroundTasks, files: List[UploadFile] = File(...)):
    session_dir = os.path.join(UPLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for file in files:
        photo_id = str(uuid.uuid4())
        filepath = os.path.join(session_dir, file.filename)
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        cursor.execute("INSERT INTO photos (id, session_id, filename, filepath) VALUES (?, ?, ?, ?)",
                       (photo_id, session_id, file.filename, filepath))
        
    cursor.execute("UPDATE sessions SET total_photos = total_photos + ?, status = 'Processando' WHERE id = ?", 
                   (len(files), session_id))
    conn.commit()
    conn.close()
    
    background_tasks.add_task(process_session_photos, session_id)
    return {"message": f"{len(files)} fotos enviadas com sucesso e em processamento."}

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, status, total_photos, processed_photos FROM sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
        
    cursor.execute("SELECT id, filename, blur_score, exposure_status, recommendation, selected FROM photos WHERE session_id = ?", (session_id,))
    photos = cursor.fetchall()
    conn.close()
    
    return {
        "id": session[0],
        "title": session[1],
        "status": session[2],
        "total_photos": session[3],
        "processed_photos": session[4],
        "photos": [
            {
                "id": p[0],
                "filename": p[1],
                "blur_score": p[2],
                "exposure_status": p[3],
                "recommendation": p[4],
                "selected": bool(p[5]),
                "url": f"/uploads/{session_id}/{p[1]}"
            } for p in photos
        ]
    }

# Rota para atualizar a seleção/aprovação individual da foto
@app.post("/api/photos/select")
def select_photo(payload: PhotoSelectPayload):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE photos SET selected = ? WHERE id = ?", (1 if payload.selected else 0, payload.photo_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "photo_id": payload.photo_id, "selected": payload.selected}

# Exporta estritamente apenas as fotos com selected = 1 (Aprovadas)
@app.get("/api/sessions/{session_id}/export")
def export_selected_photos(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT filename, filepath FROM photos WHERE session_id = ? AND selected = 1", (session_id,))
    photos = cursor.fetchall()
    conn.close()

    if not photos:
        raise HTTPException(status_code=400, detail="Nenhuma foto aprovada/selecionada para exportação.")

    zip_filename = f"selecao_{session_id[:8]}.zip"
    zip_path = os.path.join(EXPORT_DIR, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for filename, filepath in photos:
            if os.path.exists(filepath):
                zipf.write(filepath, arcname=filename)

    return FileResponse(zip_path, filename=zip_filename, media_type="application/zip")
