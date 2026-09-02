FROM python:3.10-slim

# System level packages
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-hin \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user banakar permission set karna
RUN useradd -m botuser
USER botuser
ENV PATH="/home/botuser/.local/bin:$PATH"

WORKDIR /home/botuser/app

COPY --chown=botuser:botuser requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=botuser:botuser . .

CMD ["python", "bot.py"]
