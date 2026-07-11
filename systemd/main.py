"""
Velocity Bridge - LAN Continuity Daemon for iOS → Linux

Author: trex099-Arshgour
GitHub: https://github.com/Trex099/Velocity-Bridge
License: GPL-3.0
"""
import base64
import logging
import os
import re
import subprocess
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from version import __version__ as VERSION

# Setup logging
LOG_DIR = Path.home() / ".local" / "share" / "velocity-bridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "velocity.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("velocity")

# Rate limiter - 30 requests per minute per IP
limiter = Limiter(key_func=get_remote_address)

# Filter out /stats from access logs to reduce spam
class EndpointFilter(logging.Filter):
    def filter(self, record):
        return "/stats" not in record.getMessage()

# Apply filter to uvicorn access logger
logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

app = FastAPI(
    title="Velocity Bridge",
    description="LAN-only clipboard sync between iOS and Linux",
    version=VERSION,
)

# Add rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Upload directory
UPLOAD_DIR = Path.home() / "Downloads" / "Velocity"

# Config directory for history
CONFIG_DIR = Path.home() / ".config" / "velocity-bridge"
HISTORY_FILE = CONFIG_DIR / "clipboard_history.json"
SESSION_FILE = CONFIG_DIR / "session_stats.json"

# Session tracking (in-memory, persisted to file)
SESSION_STATS = {
    "request_count": 0,
    "unique_ips": set(),
    "last_request": None,
    "recent_requests": [],  # Last 10 requests for activity feed
}

def load_config() -> dict:
    """Load settings from config file. Generate token if missing."""
    import json
    import secrets
    config_file = CONFIG_DIR / "settings.json"
    config = {}
    
    try:
        if config_file.exists():
            config = json.loads(config_file.read_text())
    except Exception:
        pass
    
    # Generate token if it doesn't exist
    if not config.get("token") and not config.get("security_token"):
        config["token"] = secrets.token_hex(12)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            config_file.write_text(json.dumps(config, indent=2))
        except Exception:
            pass
    
    return config

# Security token from environment or config (AUTH FIX)
# Support both 'token' (from curl/dnf/aur installs) and 'security_token' (legacy)
config = load_config()
SECURITY_TOKEN = os.environ.get("SECURITY_TOKEN") or config.get("token", "") or config.get("security_token", "")

def is_local_ip(ip: str) -> bool:
    """Check if an IP is from local network."""
    if not ip or ip == "unknown":
        return False
    # Localhost
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    # Private networks: 10.x.x.x, 172.16-31.x.x, 192.168.x.x
    parts = ip.split(".")
    if len(parts) == 4:
        try:
            if parts[0] == "10":
                return True
            if parts[0] == "172" and 16 <= int(parts[1]) <= 31:
                return True
            if parts[0] == "192" and parts[1] == "168":
                return True
        except ValueError:
            pass
    return False


def check_ip_whitelist(request: Request) -> None:
    """Check if IP whitelist is enabled and validate the client IP."""
    config = load_config()
    if config.get("ip_whitelist_enabled", False):
        client_ip = request.client.host if request.client else "unknown"
        if not is_local_ip(client_ip):
            logger.warning(f"Connection blocked - non-local IP: {client_ip}")
            raise HTTPException(status_code=403, detail="Access restricted to local network")


def track_request(request: Request, endpoint: str) -> None:
    """Track request for session stats."""
    client_ip = request.client.host if request.client else "unknown"
    SESSION_STATS["request_count"] += 1
    SESSION_STATS["unique_ips"].add(client_ip)
    SESSION_STATS["last_request"] = datetime.now().isoformat()
    
    # Add to recent requests (keep last 10)
    SESSION_STATS["recent_requests"].append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "ip": client_ip,
        "endpoint": endpoint,
    })
    SESSION_STATS["recent_requests"] = SESSION_STATS["recent_requests"][-10:]


def load_history() -> list:
    """Load clipboard history from file."""
    try:
        if HISTORY_FILE.exists():
            import json
            return json.loads(HISTORY_FILE.read_text())
    except Exception:
        pass
    return []


def save_history(history: list) -> None:
    """Save clipboard history to file (keep last 50 items)."""
    try:
        import json
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(history[-50:], indent=2))
    except Exception as e:
        logger.warning(f"Failed to save history: {e}")


def validate_token(token: str, request: Request = None) -> None:
    """Validate the security token. Raises 403 if invalid."""
    # Only validate if a token is actually enforced on the server (AUTH FIX)
    if SECURITY_TOKEN and token != SECURITY_TOKEN:
        # Log failed attempt with client IP
        client_ip = "unknown"
        if request:
            client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Authentication failed from IP: {client_ip}")
        raise HTTPException(status_code=403, detail="Invalid security token")


def detect_display_server() -> Literal["wayland", "x11", "unknown"]:
    """Detect whether we're running on Wayland or X11."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        return "wayland"
    elif session_type == "x11":
        return "x11"
    # Fallback: check for WAYLAND_DISPLAY
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard using appropriate tool."""
    display_server = detect_display_server()
    
    try:
        if display_server == "wayland":
            # Use Popen to avoid blocking - wl-copy stays running to serve clipboard
            proc = subprocess.Popen(
                ["wl-copy", "--"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
            # Don't wait for process - it stays running to serve clipboard
        elif display_server == "x11":
            # Try xsel first (faster, avoids timeout issues), fallback to xclip
            try:
                proc = subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                # Fallback to xclip
                proc = subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
        else:
            print(f"Unknown display server, cannot copy to clipboard")
            return False
        return True
    except subprocess.CalledProcessError as e:
        print(f"Clipboard error: {e}")
        return False
    except FileNotFoundError as e:
        print(f"Clipboard tool not found: {e}")
        return False
    except subprocess.TimeoutExpired as e:
        print(f"Clipboard timeout: {e}")
        return False


def send_notification(title: str, message: str, sound: str = "complete") -> None:
    """Send a desktop notification with sound using notify-send."""
    try:
        subprocess.run(
            ["notify-send", "-a", "Velocity", title, message],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        print("notify-send not found, skipping notification")
    
    # Play sound
    play_sound(sound)


def play_sound(sound_name: str = "complete") -> None:
    """Play a system sound using paplay (PipeWire/PulseAudio)."""
    sound_paths = [
        f"/usr/share/sounds/freedesktop/stereo/{sound_name}.oga",
        f"/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
    ]
    
    for sound_path in sound_paths:
        if os.path.exists(sound_path):
            try:
                subprocess.Popen(
                    ["paplay", sound_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                print("paplay not found, skipping sound")
            break


def is_url(content: str) -> bool:
    """Check if content looks like a URL."""
    url_pattern = re.compile(r"^https?://", re.IGNORECASE)
    return bool(url_pattern.match(content.strip()))


def get_linux_clipboard() -> tuple[str, str]:
    """
    Read current clipboard content from Linux.
    Returns (content_type, content) where content_type is 'text' or 'image'.
    """
    display_server = detect_display_server()
    
    try:
        if display_server == "wayland":
            # Wayland - try text first
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return ("text", result.stdout.decode("utf-8", errors="replace"))
        elif display_server == "x11":
            # X11
            result = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return ("text", result.stdout.decode("utf-8", errors="replace"))
        else:
            logger.error("Clipboard read failed: no graphical session environment is configured")
            return ("error", "No graphical session environment is configured")
    except subprocess.TimeoutExpired:
        return ("error", "Clipboard read timeout")
    except FileNotFoundError:
        return ("error", "Clipboard tool not found")
    except Exception as e:
        return ("error", str(e))

    error = result.stderr.decode("utf-8", errors="replace").strip()
    logger.error("Clipboard read command failed (%s): %s", display_server, error or "unknown error")
    return ("error", error or "Clipboard command failed")

class ClipboardPayload(BaseModel):
    type: Literal["text", "url"]
    content: str
    token: str


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "Velocity Bridge"}


@app.get("/stats")
async def get_stats():
    """
    Get session statistics for GUI.
    No auth required - only returns counts, not sensitive data.
    """
    return {
        "request_count": SESSION_STATS["request_count"],
        "unique_ips": len(SESSION_STATS["unique_ips"]),
        "last_request": SESSION_STATS["last_request"],
        "recent_requests": SESSION_STATS["recent_requests"],
    }


@app.get("/get_clipboard")
@limiter.limit("30/minute")
async def get_clipboard(request: Request, token: str):
    """
    Get current Linux clipboard content.
    Used for bidirectional sync (Linux → iPhone).
    
    Query params:
    - token: Security token
    """
    validate_token(token, request)
    
    content_type, content = get_linux_clipboard()
    
    if content_type == "error":
        raise HTTPException(status_code=500, detail=content)
    
    logger.info(f"Clipboard sent to iPhone: {content_type} ({len(content)} chars)")
    
    return {
        "status": "success",
        "type": content_type,
        "content": content,
    }



class ImagePayload(BaseModel):
    image: str  # Base64-encoded image data
    filename: str = "clipboard_image.png"
    token: str


@app.post("/upload_image")
@limiter.limit("20/minute")
async def upload_image(request: Request, payload: ImagePayload):
    """
    Receive Base64-encoded image from iOS clipboard.
    
    - image: Base64-encoded image data
    - filename: Optional filename
    - token: Security token
    """
    validate_token(payload.token, request)
    logger.info(f"Image upload: {payload.filename}")
    
    # Ensure upload directory exists
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # Decode Base64 image
    try:
        image_data = base64.b64decode(payload.image)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid Base64 data: {e}")
    
    # Determine filename and extension
    filename = payload.filename or "clipboard_image.png"
    if not filename.endswith(('.png', '.jpg', '.jpeg', '.gif', '.heic', '.webp')):
        filename += ".png"
    
    # Handle duplicate filenames
    target_path = UPLOAD_DIR / filename
    if target_path.exists():
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    
    # Save the file
    target_path.write_bytes(image_data)

    # Convert HEIC to PNG if needed (for saved file)
    try:
        is_heic = image_data[:12].find(b'ftyp') != -1
        if is_heic:
            logger.info("Converting HEIC upload to PNG...")
            png_path = str(target_path.with_suffix('.png'))
            # Use temp file for conversion source to avoid overwriting issues
            with tempfile.NamedTemporaryFile(delete=False, suffix='.tmp') as tmp:
                tmp.write(image_data)
                tmp_path = tmp.name
            
            # Convert: heif-convert OR convert (ImageMagick)
            # We overwrite the target_path with the new PNG
            process = subprocess.run(
                f'(heif-convert "{tmp_path}" "{png_path}" 2>/dev/null || convert "{tmp_path}" "{png_path}" 2>/dev/null)',
                shell=True,
                check=False
            )
            
            # Cleanup temp
            Path(tmp_path).unlink(missing_ok=True)
            
            if process.returncode == 0 and Path(png_path).exists():
                # If target was "image.png" but content was HEIC, we just overwrote it with real PNG.
                # If target was "image.heic", we now have "image.png".
                # Update target_path to point to the PNG
                if str(target_path) != png_path:
                    target_path.unlink(missing_ok=True)
                    target_path = Path(png_path)
                logger.info(f"Conversion successful: {target_path}")
            else:
                logger.warning("HEIC conversion failed, keeping original file")

    except Exception as e:
        logger.error(f"Error converting HEIC: {e}")

    
    # Copy image to clipboard using wl-copy (Wayland)
    # Detect format and convert HEIC to PNG for clipboard compatibility
    try:
        # Re-read data in case it was converted
        if target_path.exists():
             final_image_data = target_path.read_bytes()
        else:
             final_image_data = image_data

        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(final_image_data)
            tmp_path = tmp.name
        
        # Copy to clipboard
        subprocess.Popen(
            f'cat "{tmp_path}" | wl-copy --type image/png; rm -f "{tmp_path}"',
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"Image clipboard started ({len(final_image_data)} bytes)")

    except Exception as e:
        print(f"Failed to copy image to clipboard: {e}")
    
    # Get file size for notification
    size_kb = len(image_data) / 1024
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
    
    # Save to clipboard history
    history = load_history()
    history.append({
        "timestamp": datetime.now().isoformat(),
        "type": "image",
        "preview": f"🖼️ {target_path.name} ({size_str})",
        "content": str(target_path),  # Path to image file
    })
    save_history(history)
    
    send_notification("🖼️ Image Received", f"{target_path.name} - Copied to clipboard!", sound="camera-shutter")
    
    return {
        "status": "success",
        "filename": target_path.name,
        "path": str(target_path),
        "size": len(image_data),
        "clipboard": True,
    }


class MultiImagesPayload(BaseModel):
    images: list[str]  # List of Base64-encoded images
    token: str


@app.post("/upload_images")
@limiter.limit("15/minute")
async def upload_images(request: Request, payload: MultiImagesPayload):
    """
    Receive multiple Base64-encoded images from iOS clipboard.
    
    - images: List of Base64-encoded image data
    - token: Security token
    
    Behavior:
    - 1 image: Save + copy to clipboard (same as /upload_image)
    - Multiple images: Save all, NO clipboard, notification with count
    """
    validate_token(payload.token, request)
    
    if not payload.images:
        raise HTTPException(status_code=400, detail="No images provided")
    
    # Ensure upload directory exists
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    saved_files = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    for i, img_b64 in enumerate(payload.images):
        try:
            image_data = base64.b64decode(img_b64)
        except Exception as e:
            logger.warning(f"Skipping invalid image {i}: {e}")
            continue
        
        # Generate filename
        filename = f"image_{timestamp}_{i+1}.png"
        target_path = UPLOAD_DIR / filename
        
        # Handle duplicates
        if target_path.exists():
            counter = 1
            while target_path.exists():
                target_path = UPLOAD_DIR / f"image_{timestamp}_{i+1}_{counter}.png"
                counter += 1
        
        # Save the file
        target_path.write_bytes(image_data)
        saved_files.append({"filename": target_path.name, "size": len(image_data)})
        logger.info(f"Saved image {i+1}/{len(payload.images)}: {target_path.name}")
    
    if not saved_files:
        raise HTTPException(status_code=400, detail="No valid images to save")
    
    # If only 1 image, also copy to clipboard
    if len(saved_files) == 1:
        first_file = UPLOAD_DIR / saved_files[0]["filename"]
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
                tmp.write(first_file.read_bytes())
                tmp_path = tmp.name
            subprocess.Popen(
                f'cat "{tmp_path}" | wl-copy --type image/png; rm -f "{tmp_path}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            logger.warning(f"Failed to copy to clipboard: {e}")
        
        send_notification("🖼️ Image Received", f"{saved_files[0]['filename']} - Copied to clipboard!")
        return {"status": "success", "saved": 1, "clipboard": True, "files": saved_files}
    
    # Multiple images: save only, no clipboard
    send_notification(
        f"🖼️ {len(saved_files)} Images Saved",
        f"Saved to ~/Downloads/Velocity/"
    )
    
    return {
        "status": "success",
        "saved": len(saved_files),
        "clipboard": False,
        "files": saved_files,
    }


def get_local_ip():
    """Get the local IP address of this machine."""
    import socket
    try:
        # Create a dummy socket to connect to an external IP (doesn't actually connect)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.get("/status")
# No rate limit - UI needs to poll frequently
async def get_status(request: Request):
    """Get server status and connection info."""
    # Check IP whitelist but minimal security so UI can see it locally
    check_ip_whitelist(request)
    
    import socket
    hostname = socket.gethostname()

    # Detect installation method
    # If the APPIMAGE environment variable is set, it's an AppImage (or Curl install)
    install_method = "appimage" if os.environ.get("APPIMAGE") else "native"

    return {
        "status": "running",
        "version": VERSION,
        "ip": get_local_ip(),
        "hostname": f"{hostname}.local",  # mDNS format
        "port": 8080,
        "token": SECURITY_TOKEN,  # Send token so UI can display it
        "clients": len(SESSION_STATS["unique_ips"]),
        "requests": SESSION_STATS["request_count"],
        "install_method": install_method,
    }


@app.post("/clipboard")
@limiter.limit("30/minute")
async def receive_clipboard(request: Request, payload: ClipboardPayload):
    """
    Receive clipboard content from iOS.
    
    - type: "text" or "url"
    - content: The actual content
    - token: Security token for validation
    """
    # Security checks
    check_ip_whitelist(request)
    validate_token(payload.token, request)
    
    # Track this request
    track_request(request, "/clipboard")
    
    logger.info(f"Clipboard: {payload.type} ({len(payload.content)} chars)")
    
    content = payload.content.strip()
    
    # Save to clipboard history
    history = load_history()
    history.append({
        "timestamp": datetime.now().isoformat(),
        "type": payload.type,
        "preview": content[:100] + "..." if len(content) > 100 else content,
        "content": content,  # Full content for Copy button in History tab
    })
    save_history(history)
    
    # Handle URLs - copy to clipboard AND open in browser
    if payload.type == "url" or is_url(content):
        copy_to_clipboard(content)
        try:
            webbrowser.open(content)
            send_notification("🌐 URL Received", content[:50] + "..." if len(content) > 50 else content, sound="complete")
            return {"status": "success", "action": "opened_url", "clipboard": True}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to open URL: {e}")
    
    # Handle text - copy to clipboard
    if copy_to_clipboard(content):
        send_notification("📋 Clipboard Updated", content[:50] + "..." if len(content) > 50 else content, sound="message-new-instant")
        return {"status": "success", "action": "copied_to_clipboard"}
    else:
        raise HTTPException(status_code=500, detail="Failed to copy to clipboard")


@app.post("/upload")
@limiter.limit("15/minute")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    token: str = Form(None),
):
    """
    Receive file upload from iOS.
    
    - file: The file to upload (multipart)
    - token: Security token for validation (form field)
    """
    # Token can come from form or be None (we'll still validate)
    if token:
        validate_token(token, request)
    else:
        # Try to get token from query param as fallback
        raise HTTPException(status_code=403, detail="Token required")
    
    # Ensure upload directory exists
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # Sanitize filename
    filename = file.filename or "unnamed_file"
    # Remove any path components
    filename = Path(filename).name
    
    # Handle duplicate filenames
    target_path = UPLOAD_DIR / filename
    if target_path.exists():
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while target_path.exists():
            target_path = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    
    # Save the file
    try:
        content = await file.read()
        target_path.write_bytes(content)
        
        # Get file size for notification
        size_kb = len(content) / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        
        send_notification(
            "📁 File Received",
            f"{target_path.name} ({size_str})"
        )
        
        return {
            "status": "success",
            "filename": target_path.name,
            "path": str(target_path),
            "size": len(content),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")


@app.get("/history")
# No rate limit - UI need to poll frequently for real-time updates
async def get_history(request: Request, token: str, limit: int = 20):
    """
    Get clipboard history.
    
    Query params:
    - token: Security token
    - limit: Number of items to return (default 20, max 50)
    """
    validate_token(token, request)
    
    history = load_history()
    # Return most recent first, limited to requested count
    limit = min(limit, 50)
    return {
        "status": "success",
        "count": len(history),
        "items": history[-limit:][::-1],  # Reverse for most recent first
    }


@app.delete("/history")
async def clear_history(request: Request, token: str):
    """
    Clear all clipboard history.
    
    Query params:
    - token: Security token
    """
    validate_token(token, request)
    
    # Clear the history file
    save_history([])
    
    logger.info("Clipboard history cleared")
    
    return {"status": "success", "message": "History cleared"}


@app.post("/update")
async def update_service(request: Request, token: str):
    """
    Trigger self-update mechanism.
    Pulls latest code and restarts service.
    """
    validate_token(token, request)
    logger.info("Update requested via API")
    
    # Script is in ../../service/update.sh relative to this file
    script_path = Path(__file__).parent.parent / "service" / "update.sh"
    
    if not script_path.exists():
         logger.error(f"Update script not found at {script_path}")
         raise HTTPException(status_code=500, detail="Update script not found")
         
    # Run update script in background (files are updated, then service restarts)
    try:
        subprocess.Popen(
            ["/bin/bash", str(script_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logger.error(f"Failed to start update script: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start update: {e}")
    
    return {"status": "updating", "message": "Service is updating and will restart shortly"}


def check_port(port: int) -> bool:
    """Check if port is already in use"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

if __name__ == "__main__":
    import uvicorn
    import sys
    
    PORT = 8080
    
    if check_port(PORT):
        logger.warning(f"⚠️ Port {PORT} is already in use! Velocity Bridge might already be running (Headless or GUI mode).")
        logger.warning("Attempting to start anyway (it will likely fail to bind)...")
        
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)
