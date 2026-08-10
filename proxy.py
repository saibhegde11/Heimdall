import httpx
from fastapi import FastAPI, Request

app = FastAPI()

SERVERS = [
    "http://127.0.0.1:8001",
    "http://127.0.0.1:8002",
    "http://127.0.0.1:8003"
]

current_server_index = 0

@app.get("/")
async def load_balancer(request: Request) : 
    global current_server_index
    target_server = SERVERS[current_server_index]
    current_server_index = (current_server_index + 1) % len(SERVERS)
    print(f"Intercepted request! Transferring to {target_server}")

    async with httpx.AsyncClient() as client : 
        response = await client.get(target_server)
        return response.json()