from fastapi import FastAPI


app = FastAPI(
    title="SIH26122 API",
    version="0.1.0",
    description="Backend foundation for the SIH 2026 prototype.",
)


@app.get("/health", tags=["system"])
async def health_check() -> dict[str, str]:
    """Return service health for local development and deployment checks."""
    return {"status": "ok"}
