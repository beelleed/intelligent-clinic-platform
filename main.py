import os

import uvicorn


if __name__ == "__main__":
    reload_enabled = (
        os.getenv("APP_RELOAD", "true").strip().lower() == "true"
    )
    uvicorn.run(
        "app.api:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=reload_enabled,
    )
