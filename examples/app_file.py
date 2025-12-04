"""
Sample FastAPI application for external file deployment.

This file is deployed by 08_external_file.py
"""
from fastapi import FastAPI
import socket
from datetime import datetime

app = FastAPI(title="External File Demo")


@app.get("/")
def root():
    return {"message": "Hello from external file!"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/info")
def info():
    return {
        "hostname": socket.gethostname(),
        "timestamp": datetime.utcnow().isoformat(),
        "source": "app_file.py",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
