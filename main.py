from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from traffic_api import router as traffic_router
from safety_alert_api import router as safety_router
from violation_store import get_all_violations
from multi_camera_processor import start_all_cameras, start_signal_updater, start_audio_monitor
from stats_manager import stats
from video_stream import router as video_router
import time

import os

app = FastAPI()
app.include_router(video_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

emergency_override = {
    "active": False,
    "cam_id": None,
    "timestamp": 0
}

EMERGENCY_HOLD_TIME = 20  # seconds to hold green for emergency

# Start camera threads + signal logic
camera_sources = {
    "CAM1": "cameras/v.mp4",
    "CAM2": "cameras/cam2.mp4",
    "CAM3": "cameras/cam3.mp4",
    "CAM4": "cameras/cam4.mp4",
}
start_all_cameras(camera_sources)
start_audio_monitor()
start_signal_updater()

# Mount routers
app.include_router(traffic_router)
app.include_router(safety_router)

@app.get("/api/stats")
def get_stats():
    return {
        "vehicles": stats.get_stats()["vehicles"],
        "violations": stats.get_stats()["violations"]
    }

@app.get("/api/challans")
def fetch_all_challans():
    return get_all_violations()

@app.get("/api/challans/download")
def download_challan(pdf_path: str):
    if not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not found")
    return FileResponse(path=pdf_path, filename=pdf_path.split("/")[-1])

@app.get("/api/emergency-status")
def get_emergency_status():
    return {
        "active": emergency_override["active"],
        "cam_id": emergency_override["cam_id"],
        "remaining_time": max(0, EMERGENCY_HOLD_TIME - int(time.time() - emergency_override["timestamp"])) if emergency_override["active"] else 0
    }