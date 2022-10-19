from fastapi import APIRouter
import progress_bars.traces.router

router = APIRouter()

router.include_router(progress_bars.traces.router.router)
