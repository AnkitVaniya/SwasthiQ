.PHONY: run test eval
run:
	pip install -q -r backend/requirements.txt && cd frontend && npm ci --no-audit --no-fund && npm run build && cd ../backend && uvicorn app.main:app --port 8000
test:
	cd backend && python -m pytest tests -q
eval:
	python runner.py --repeat 3 && python runner.py --dir adversarial --repeat 3
