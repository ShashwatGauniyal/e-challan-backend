import cv2
import threading
import time
from collections import defaultdict
from speed_estimator import SpeedEstimator
from stats_manager import stats
from sort import Sort
import numpy as np
import torch
from violation_store import log_violation
from lane_detection import detect_lanes_and_assign_vehicles
from emergency_detector import detect_emergency_vehicle
from traffic_signal_controller import override_signal
import os

os.makedirs("e_challans", exist_ok=True)
os.makedirs("snapshots", exist_ok=True)

# This line is drawn at 80% of the frame height
CROSSING_LINE_Y_RATIO = 0.8
vehicle_last_positions = {}  # vehicle_id → last_y
violated_ids = set()         # avoid duplicate logging

# Load the YOLOv5 model (best.pt = custom model, or use 'yolov5s' for COCO pretrained)
model = torch.hub.load('ultralytics/yolov5', 'yolov5s', pretrained=True)
model.conf = 0.4  # Confidence threshold
model.iou = 0.45  # IOU threshold for NMS

# Shared dictionaries for vehicle counts and traffic signals
vehicle_counts = defaultdict(int)
traffic_lights = {}
fps_lookup = {}
tracker = Sort()

emergency_override = {
    "active": False,
    "cam_id": None,
    "timestamp": 0
}

EMERGENCY_HOLD_TIME = 20  # seconds to hold green for emergency

def override_signal(cam_id):
    for cam in traffic_lights:
        traffic_lights[cam] = "RED"
    traffic_lights[cam_id] = "GREEN"
    emergency_override["active"] = True
    emergency_override["cam_id"] = cam_id
    emergency_override["timestamp"] = time.time()
    print(f"[🚦] Emergency override: GREEN set for {cam_id} at {emergency_override['timestamp']}")



# Dummy object detector and tracker (to be replaced with actual model)
def detect_and_track_objects(frame):
    # Run YOLOv5 detection
    results = model(frame)
    detections = []

    for *box, conf, cls in results.xyxy[0]:  # one detection = [x1, y1, x2, y2, conf, class]
        x1, y1, x2, y2 = map(int, box)
        confidence = float(conf)
        detections.append([x1, y1, x2, y2, confidence])

    if len(detections) == 0:
        return []

    dets = np.array(detections)
    if dets.ndim == 1:
        dets = np.expand_dims(dets, axis=0)

    tracked = tracker.update(dets)

    tracked = tracker.update(np.array(detections))

    result = []
    for trk in tracked:
        x1, y1, x2, y2, track_id = trk
        result.append({
            "id": int(track_id),
            "bbox": (int(x1), int(y1), int(x2), int(y2))
        })

    return result


def process_camera(cam_id, video_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    fps_lookup[cam_id] = fps
    estimator = SpeedEstimator(fps)
    counted_ids = set()

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        # 🚨 Emergency vehicle check
        if detect_emergency_vehicle(frame):
            override_signal(cam_id)

        tracked_objects = detect_and_track_objects(frame)
        frame_height = frame.shape[0]
        crossing_y = int(CROSSING_LINE_Y_RATIO * frame_height)

        for obj in tracked_objects:
            vehicle_id = obj["id"]
            x1, y1, x2, y2 = obj["bbox"]
            current_y = y2

            last_y = vehicle_last_positions.get(vehicle_id, current_y)
            vehicle_last_positions[vehicle_id] = current_y

            if last_y < crossing_y <= current_y:
                if traffic_lights.get(cam_id, "RED") == "RED" and vehicle_id not in violated_ids:
                    speed = int(estimator.get_speed(vehicle_id))
                    pdf_path = f"e_challans/{vehicle_id}_{cam_id}.pdf"
                    snapshot_path = f"snapshots/{vehicle_id}_{cam_id}.jpg"
                    # Highlight violating vehicle
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.imwrite(snapshot_path, frame)
                    log_violation(cam_id, vehicle_id, speed, pdf_path, snapshot_path)
                    violated_ids.add(vehicle_id)
                    print(f"[🚨] Violation detected for {vehicle_id} on {cam_id}")

        vehicle_counts[cam_id] = len(tracked_objects)

        for obj in tracked_objects:
            vehicle_id = obj.get("id")
            if vehicle_id not in counted_ids:
                stats.increment_vehicle()
                counted_ids.add(vehicle_id)

        frame = estimator.update(tracked_objects, frame)
        frame, vehicle_lane_map = detect_lanes_and_assign_vehicles(frame, tracked_objects)

        for obj in tracked_objects:
            x1, y1, x2, y2 = obj["bbox"]
            vehicle_id = obj["id"]
            lane = vehicle_lane_map.get(vehicle_id, -1)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(frame, f'ID: {vehicle_id}', (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
            cv2.putText(frame, f'Lane: {lane}', (x1, y1 - 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 2)

    cap.release()
    vehicle_counts[cam_id] = 0
    
def start_all_cameras(camera_sources):
    for cam_id, path in camera_sources.items():
        thread = threading.Thread(target=process_camera, args=(cam_id, path), daemon=True)
        thread.start()
from women_safety_audio import monitor_audio

def start_audio_monitor():
    threading.Thread(target=monitor_audio, daemon=True).start()

LOW_TRAFFIC_THRESHOLD = 2
lane_cycle = []
current_index = 0
lane_priority = defaultdict(int)
WAIT_LIMIT = 3  # After 3 rounds without green, force green

def update_traffic_signals():
    global current_index, lane_cycle

    while True:
        # 🚨 If emergency override is active
        if emergency_override["active"]:
            elapsed = time.time() - emergency_override["timestamp"]
            if elapsed < EMERGENCY_HOLD_TIME:
                cam = emergency_override["cam_id"]
                for c in traffic_lights:
                    traffic_lights[c] = "RED"
                traffic_lights[cam] = "GREEN"
                print(f"[⏱️] Emergency GREEN still active on {cam} ({int(EMERGENCY_HOLD_TIME - elapsed)}s left)")
                time.sleep(1)
                continue
            else:
                emergency_override["active"] = False
                emergency_override["cam_id"] = None
                print("[✅] Emergency override ended")

        if not vehicle_counts:
            time.sleep(1)
            continue

        lanes = list(vehicle_counts.keys())
        counts = vehicle_counts.copy()

        if not lane_cycle or set(lane_cycle) != set(lanes):
            lane_cycle = lanes
            current_index = 0
            lane_priority.clear()

        if all(count < LOW_TRAFFIC_THRESHOLD for count in counts.values()):
            selected_cam = lane_cycle[current_index % len(lane_cycle)]
            current_index += 1
            lane_priority[selected_cam] = 0
            print(f"[ℹ️] Low traffic: Switching to {selected_cam} (cyclic)")
        else:
            forced_green = None
            for cam in lanes:
                if lane_priority[cam] >= WAIT_LIMIT:
                    forced_green = cam
                    break

            if forced_green:
                selected_cam = forced_green
                print(f"[⚖️] Fairness override: {selected_cam} forced green")
            else:
                selected_cam = max(counts, key=counts.get)
                print(f"[ℹ️] High traffic: Giving green to {selected_cam} (density)")

            for cam in lanes:
                if cam == selected_cam:
                    lane_priority[cam] = 0
                else:
                    lane_priority[cam] += 1

        for cam in lanes:
            traffic_lights[cam] = "GREEN" if cam == selected_cam else "RED"

        time.sleep(10)


def start_signal_updater():
    thread = threading.Thread(target=update_traffic_signals, daemon=True)
    thread.start()
