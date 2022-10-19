from fastapi import APIRouter
import progress_bars.traces.routes.watch

router = APIRouter()

router.include_router(progress_bars.traces.routes.watch.router)
