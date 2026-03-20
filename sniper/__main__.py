"""Allow running via `python -m sniper`."""

import asyncio

from sniper.main import main

asyncio.run(main())
