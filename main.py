import asyncio, json, os
from enum import Enum
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, conint
from fastapi.middleware.cors import CORSMiddleware

ESP_HOST = os.getenv("ESP_HOST", "192.168.4.1")
ESP_PORT = int(os.getenv("ESP_PORT", "100"))  # set this to the TCP port your ESP32 listens on
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "1.0"))
HEARTBEAT_ENABLE = os.getenv("HEARTBEAT_ENABLE", "1") != "0"

# ---------- Transport (persistent TCP + heartbeat) ----------

class ESPBridge:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._rx_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._counter = 0
        self.last_rx: Optional[str] = None

    def next_H(self) -> str:
        self._counter = (self._counter + 1) % 1_000_000
        return str(self._counter)

    async def connect(self):
        backoff = 0.5
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
                self._connected.set()
                # start tasks
                self._rx_task = asyncio.create_task(self._rx_loop(), name="esp_rx")
                if HEARTBEAT_ENABLE:
                    self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="esp_hb")
                return
            except Exception as e:
                print(f"[bridge] connect failed: {e}; retrying in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 5.0)

    async def _rx_loop(self):
        buf = bytearray()
        try:
            while True:
                ch = await self.reader.read(1)
                if not ch:
                    raise ConnectionError("EOF from ESP32")
                b = ch[0]
                buf.append(b)
                if b == ord('}'):
                    try:
                        frame = buf.decode(errors="ignore")
                        self.last_rx = frame
                        print(f"[ESP<-] {frame.strip()}")
                    finally:
                        buf.clear()
        except Exception as e:
            print(f"[bridge] rx loop ended: {e}")
        finally:
            await self._teardown()

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self.send_raw("{Heartbeat}")
            except Exception as e:
                print(f"[bridge] heartbeat send failed: {e}")

    async def _teardown(self):
        self._connected.clear()
        try:
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
        except:
            pass
        self.reader = self.writer = None
        # cancel tasks (except the one calling _teardown)
        if self._hb_task and not self._hb_task.cancelled():
            self._hb_task.cancel()
        if self._rx_task and not self._rx_task.cancelled():
            self._rx_task.cancel()
        # reconnect
        asyncio.create_task(self.connect())

    async def send_json(self, obj: dict):
        # compact JSON, wrapped in braces (ESP strips spaces anyway)
        frame = json.dumps(obj, separators=(",", ":"))
        await self.send_raw(frame)

    async def send_raw(self, frame: str):
        if not self._connected.is_set():
            raise HTTPException(status_code=503, detail="ESP32 not connected")
        async with self._lock:
            try:
                assert self.writer is not None
                print(f"[ESP->] {frame}")
                self.writer.write(frame.encode("utf-8"))
                await self.writer.drain()
            except Exception as e:
                print(f"[bridge] send failed: {e}")
                await self._teardown()
                raise HTTPException(status_code=503, detail="ESP32 send failed")

bridge = ESPBridge(ESP_HOST, ESP_PORT)

app = FastAPI(title="Elegoo Smart Car API", version="1.0")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(bridge.connect())

@app.on_event("shutdown")
async def _shutdown():
    # best-effort close
    try:
        if bridge.writer:
            bridge.writer.close()
            await bridge.writer.wait_closed()
    except:
        pass

# ---------- Models / Enums ----------

class Dir8(str, Enum):
    forward = "forward"
    back    = "back"
    left    = "left"
    right   = "right"
    lf      = "lf"
    lb      = "lb"
    rf      = "rf"
    rb      = "rb"
    stop    = "stop"

DIR_MAP_N23 = {
    # For N=3 and N=102 / N=2 (rocker-style)
    Dir8.left: 1, Dir8.right: 2, Dir8.forward: 3, Dir8.back: 4,
    Dir8.lf: 5, Dir8.lb: 6, Dir8.rf: 7, Dir8.rb: 8, Dir8.stop: 9
}

def H() -> str:
    return bridge.next_H()

# ---------- Endpoints (one per command category) ----------

class MoveBody(BaseModel):
    direction: Dir8
    speed: conint(ge=0, le=255) = 140

@app.post("/move", summary="N=3 CarControl_NoTimeLimit")
async def move(body: MoveBody):
    msg = {"H": H(), "N": 3, "D1": DIR_MAP_N23[body.direction], "D2": int(body.speed)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class MoveTimedBody(BaseModel):
    direction: Dir8
    speed: conint(ge=0, le=255) = 140
    duration_ms: conint(gt=0) = 800

@app.post("/move_timed", summary="N=2 CarControl_TimeLimited")
async def move_timed(body: MoveTimedBody):
    msg = {"H": H(), "N": 2, "D1": DIR_MAP_N23[body.direction], "D2": int(body.speed), "T": int(body.duration_ms)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class MotorBody(BaseModel):
    motor: conint(ge=0, le=3) = 1
    speed: conint(ge=0, le=255) = 140
    direction: conint(ge=0, le=1) = 0  # 0=fwd, 1=rev (confirm wiring)

@app.post("/motor", summary="N=1 MotorControl single motor")
async def motor(body: MotorBody):
    msg = {"H": H(), "N": 1, "D1": int(body.motor), "D2": int(body.speed), "D3": int(body.direction)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class SpeedBody(BaseModel):
    left: conint(ge=0, le=255)
    right: conint(ge=0, le=255)

@app.post("/speed", summary="N=4 Per-wheel speeds")
async def speed(body: SpeedBody):
    msg = {"H": H(), "N": 4, "D1": int(body.left), "D2": int(body.right)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class ServoBody(BaseModel):
    servo_id: conint(ge=1, le=5) = 1
    angle: conint(ge=0, le=180)

@app.post("/servo", summary="N=5 ServoControl")
async def servo(body: ServoBody):
    msg = {"H": H(), "N": 5, "D1": int(body.servo_id), "D2": int(body.angle)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class LightsTimedBody(BaseModel):
    sequence: int = Field(..., description="bitmask/index for which LEDs")
    r: conint(ge=0, le=255)
    g: conint(ge=0, le=255)
    b: conint(ge=0, le=255)
    duration_ms: conint(gt=0)

@app.post("/lights_timed", summary="N=7 Lighting time-limited")
async def lights_timed(body: LightsTimedBody):
    msg = {"H": H(), "N": 7, "D1": body.sequence, "D2": body.r, "D3": body.g, "D4": body.b, "T": body.duration_ms}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class LightsBody(BaseModel):
    sequence: int
    r: conint(ge=0, le=255)
    g: conint(ge=0, le=255)
    b: conint(ge=0, le=255)

@app.post("/lights", summary="N=8 Lighting no-time-limit")
async def lights(body: LightsBody):
    msg = {"H": H(), "N": 8, "D1": body.sequence, "D2": body.r, "D3": body.g, "D4": body.b}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class UltrasonicBody(BaseModel):
    flag: int = 1

@app.post("/ultrasonic", summary="N=21 Ultrasonic query/enable")
async def ultrasonic(body: UltrasonicBody):
    msg = {"H": H(), "N": 21, "D1": int(body.flag)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class LineTrackBody(BaseModel):
    flag: int = 1

@app.post("/line", summary="N=22 IR line-tracking query/enable")
async def line(body: LineTrackBody):
    msg = {"H": H(), "N": 22, "D1": int(body.flag)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

@app.post("/liftcheck", summary="N=23 Leave-the-ground check")
async def liftcheck():
    msg = {"H": H(), "N": 23}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

@app.post("/standby", summary="N=100 Clear all / standby")
async def standby():
    msg = {"H": H(), "N": 100}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class ModeBody(BaseModel):
    mode: conint(ge=1, le=3) = Field(..., description="1=LineFollow, 2=ObstacleAvoid, 3=Follow")

@app.post("/mode", summary="N=101 Switch mode")
async def mode(body: ModeBody):
    msg = {"H": H(), "N": 101, "D1": int(body.mode)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class RockerBody(BaseModel):
    direction: Dir8
    speed: conint(ge=0, le=255) = 140

@app.post("/rocker", summary="N=102 Rocker control (dir+speed)")
async def rocker(body: RockerBody):
    msg = {"H": H(), "N": 102, "D1": DIR_MAP_N23[body.direction], "D2": int(body.speed)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class LedBrightnessBody(BaseModel):
    step: conint(ge=1, le=2) = Field(..., description="1=+5, 2=-5")

@app.post("/led_brightness", summary="N=105 Adjust FastLED brightness")
async def led_brightness(body: LedBrightnessBody):
    msg = {"H": H(), "N": 105, "D1": int(body.step)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

class ServoPresetBody(BaseModel):
    servo_id: conint(ge=1, le=5)

@app.post("/servo_preset", summary="N=106 Apply servo preset routine")
async def servo_preset(body: ServoPresetBody):
    msg = {"H": H(), "N": 106, "D1": int(body.servo_id)}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

@app.post("/programming_mode", summary="N=110 Clear all / programming mode")
async def programming_mode():
    msg = {"H": H(), "N": 110}
    await bridge.send_json(msg)
    return {"sent": msg, "last_rx": bridge.last_rx}

# --- Utility endpoints ---

@app.post("/stop", summary="Hard stop")
async def stop():
    # per-wheel zero
    msg1 = {"H": H(), "N": 4, "D1": 0, "D2": 0}
    await bridge.send_json(msg1)
    # standby/clear-all (vendor code sets Standby_mode here)
    msg2 = {"H": H(), "N": 100}
    await bridge.send_json(msg2)
    return {"sent": [msg1, msg2], "last_rx": bridge.last_rx}


@app.post("/heartbeat", summary="Send a single {Heartbeat} frame")
async def heartbeat():
    await bridge.send_raw("{Heartbeat}")
    return {"sent": "{Heartbeat}", "last_rx": bridge.last_rx}

class RawBody(BaseModel):
    payload: dict

@app.post("/raw", summary="Send arbitrary raw JSON to ESP32")
async def raw(body: RawBody):
    # Caller supplies full JSON (no spaces recommended)
    # We'll auto-insert H if missing
    obj = dict(body.payload)
    obj.setdefault("H", H())
    await bridge.send_json(obj)
    return {"sent": obj, "last_rx": bridge.last_rx}

@app.get("/healthz")
async def healthz():
    return {"esp_host": ESP_HOST, "esp_port": ESP_PORT, "connected": bridge._connected.is_set(), "last_rx": bridge.last_rx}

# DEV: allow everything (no cookies/credentials)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # or list specific origins below
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,    # must be False if allow_origins=["*"]
)
