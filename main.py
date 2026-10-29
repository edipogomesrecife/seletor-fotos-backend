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
from pydantic import BaseModel
from typing import List
import uuid

app = FastAPI(title="Seletor Inteligente de Fotografias - API")

# Permite chamadas do frontend hospedado na Vercel
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

# Inicialização do Banco de Dados SQLite
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
            selected INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Algoritmos de Análise de Imagem (OpenCV / ImageHash)
def analyze_photo(filepath: str):
    # 1. Análise de Nitidez via Variância do Laplaciano
    img_cv = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
    if img_cv is None:
        return 0.0, "Erro", ""
    
    blur_score = cv2.Laplacian(img_cv, cv2.CV_64F).var()
    
    # 2. Análise de Exposição via Histograma
    hist = cv2.calcHist([img_cv], [0], None, [256], [0, 256])
    total_pixels = img_cv.size
    under_exposed = np.sum(hist[:30]) / total_pixels
    over_exposed = np.sum(hist[225:]) / total_pixels
    
    exposure_status = "Equilibrada"
    if under_exposed > 0.30:
        exposure_status = "Subexposta"
    elif over_exposed > 0.30:
        exposure_status = "Superexposta"

    # 3. Hash Perceptual (pHash) para Imagens Semelhantes
    pil_img = Image.open(filepath)
    phash_val = str(imagehash.phash(pil_img))
    
    return round(blur_score, 2), exposure_status, phash_val

def process_session_photos(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, filepath FROM photos WHERE session_id = ?", (session_id,))
    photos = cursor.fetchall()
    
    for photo_id, filepath in photos:
        blur, exposure, phash = analyze_photo(filepath)
        recommendation = "Recomendada" if blur > 100.0 and exposure == "Equilibrada" else "Revisar"
        
        cursor.execute('''
            UPDATE photos 
            SET blur_score = ?, exposure_status = ?, phash = ?, recommendation = ?
            WHERE id = ?
        ''', (blur, exposure, phash, recommendation, photo_id))
        
        cursor.execute("UPDATE sessions SET processed_photos = processed_photos + 1 WHERE id = ?", (session_id,))
        conn.commit()
        
    cursor.execute("UPDATE sessions SET status = 'Concluída' WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

# Rotas da API
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
                "selected": bool(p[5])
            } for p in photos
        ]
    }
