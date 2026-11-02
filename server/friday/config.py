import os
from types import SimpleNamespace


def load():
    """All settings come from env vars so the home box is configured in one .env file."""
    e = os.environ.get
    model = e("FRIDAY_LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct")
    return SimpleNamespace(
        token=e("FRIDAY_TOKEN", ""),
        owner=e("FRIDAY_OWNER_NAME", "the owner"),
        db=e("FRIDAY_DB", "friday.db"),
        # Any OpenAI-compatible server: tt-inference-server (vLLM) on the Blackhole box by default.
        llm_base=e("FRIDAY_LLM_BASE_URL", "http://localhost:8000/v1"),
        llm_key=e("FRIDAY_LLM_API_KEY", "none"),
        llm_model=model,
        fast_model=e("FRIDAY_FAST_MODEL", model),
        vision_model=e("FRIDAY_VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct"),
        retain_days=int(e("FRIDAY_RETAIN_DAYS", "30")),
        keep_importance=float(e("FRIDAY_KEEP_IMPORTANCE", "0.5")),
        record_others=e("FRIDAY_RECORD_OTHERS", "0") == "1",
        github_token=e("GITHUB_TOKEN", ""),
        # Phone control: ESP32 BLE-HID bridge + AirPlay frame dumped to disk.
        hid_url=e("FRIDAY_HID_URL", "http://friday-hid.local"),
        hid_token=e("FRIDAY_HID_TOKEN", ""),
        screen_frame=e("FRIDAY_SCREEN_FRAME", "/tmp/iphone.jpg"),
        screen_w=int(e("FRIDAY_SCREEN_W", "393")),
        screen_h=int(e("FRIDAY_SCREEN_H", "852")),
        mouse_scale=float(e("FRIDAY_MOUSE_SCALE", "1.0")),
        twilio_sid=e("TWILIO_ACCOUNT_SID", ""),
        twilio_token=e("TWILIO_AUTH_TOKEN", ""),
        twilio_from=e("TWILIO_FROM_NUMBER", ""),
        public_url=e("FRIDAY_PUBLIC_URL", ""),
        motion_threshold=float(e("FRIDAY_MOTION_THRESHOLD", "2.0")),
        home_lat=float(e("FRIDAY_HOME_LAT", "0")),
        home_lon=float(e("FRIDAY_HOME_LON", "0")),
    )
