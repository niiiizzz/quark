"""
API process launcher ensuring Windows SelectorEventLoop is active for psycopg async.
"""

import sys
import selectors
import asyncio
import uvicorn
from app.api.main import app
from app.config import settings

if __name__ == "__main__":
    if sys.platform == "win32":
        # Create and set a SelectorEventLoop explicitly
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            loop="asyncio",
            log_level="info",
        )
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(app, host=settings.host, port=settings.port)
