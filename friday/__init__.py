import os

# The on-device models must not phone home: Friday's promise is that System 1 is local or explicit.
os.environ.setdefault("CACTUS_TELEMETRY", "0")
