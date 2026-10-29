FROM python:3.10-slim

WORKDIR /app

# Instala bibliotecas de sistema necessárias para o OpenCV rodar
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala as dependências do Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o código da aplicação
COPY . .

# Expõe a porta onde a API vai rodar
EXPOSE 8000

# Comando para iniciar o servidor FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
