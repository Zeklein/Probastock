from fastapi import FastAPI

app = FastAPI(title="Probastock", version="0.1.0")


@app.get("/")
def root():
    return {"status": "ok", "project": "Probastock"}
