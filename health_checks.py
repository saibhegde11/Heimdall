import httpx
from config import get_settings

settings = get_settings()
HEALTH_CHECK_TIMEOUT = settings.health_check_timeout

async def is_backend_healthy(
    client : httpx.AsyncClient,
    backend_url : str,
) -> bool : 
    try : 
        response = await client.get(
            f"{backend_url}/health",
            timeout = HEALTH_CHECK_TIMEOUT,
            follow_redirects=False
        )
    except httpx.RequestError : 
        return False;

    return response.status_code == 200

async def get_healthy_backends(
    client : httpx.AsyncClient,
    backends : list[str],
) -> list[str] : 
    healthy_backends = []
    for backend in backends : 
        is_healthy = await is_backend_healthy(client, backend)
        if is_healthy : 
            healthy_backends.append(backend)

    return healthy_backends
