FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY conversations conversations
COPY adversarial adversarial
COPY --from=ui /ui/dist frontend/dist
ENV DATA_DIR=/tmp/data PORT=8000
EXPOSE 8000
CMD cd backend && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
