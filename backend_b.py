from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

app = FastAPI()


@app.get("/")
def root():
    return {"server": "B"}


@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {
        "server": "B",
        "item_id": item_id,
    }

@app.get("/search")
def search(request: Request):
    return {
        "server": "B",
        "query": request.query_params.multi_items(),
    }

@app.post("/echo", status_code=201)
async def echo(request: Request):
    return {
        "server": "B",
        "method": request.method,
        "content_type": request.headers.get("content-type"),
        "query": request.query_params.multi_items(),
        "received": await request.json(),
    }

@app.get("/go")
def go():
    return RedirectResponse(
        url="/items/42",
        status_code=302,
    )