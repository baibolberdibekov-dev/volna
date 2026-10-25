import os

import uvicorn

if __name__ == "__main__":
    # A single process is required for the in-memory queue and status store.
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                workers=1, access_log=False)
