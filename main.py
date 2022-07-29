from fastapi import FastAPI
import multiprocessing
import updater

multiprocessing.Process(target=updater.listen_forever_sync, daemon=True).start()
app = FastAPI(title="ezpbars", description="easy progress bars", version="1.0.0+alpha")


@app.get("/")
def root():
    return {"message": "Hello World"}
