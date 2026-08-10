from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home(request: Request) : 
    # This dynamically grabs the port that this specific server is running on
    server_port = request.url.port
    
    print(f"Request received on Port {server_port}")
    return {
        "message": "Success",
        "handled_by": f"Server running on port {server_port}"
    }