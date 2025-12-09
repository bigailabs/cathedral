from fastapi import FastAPI

app = FastAPI(title="My API")


@app.get("/")
def root():
    return {"message": "Hello from my API!"}


@app.get("/predict")
def predict(x: float):
    return {"input": x, "prediction": x * 2}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
