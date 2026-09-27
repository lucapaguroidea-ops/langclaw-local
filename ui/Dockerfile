FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py client.py editor.py ./
COPY templates ./templates

# Railway injects PORT; default for local runs.
ENV PORT=8501
CMD ["sh", "-c", "streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false"]
