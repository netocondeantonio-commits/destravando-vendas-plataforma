FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY checkout.html .
COPY afiliados.html .
COPY painel.html .
COPY admin.html .
COPY ferramentas.html .
COPY widget.js .
COPY payout.py .
ENV DB_PATH=/data/ponte.db
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
