FROM python:3.10-slim

# Working directory set karein
WORKDIR /app

# Dependencies install karein
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Bot code copy karein
COPY . .

# Bot run karein
CMD ["python", "bot.py"]
