from fastapi import FastAPI

app = FastAPI(title="ezpbars", description="easy progress bars", version="1.0.0+alpha")


@app.get("/")
def root():
    return {"message": "Hello World"}
