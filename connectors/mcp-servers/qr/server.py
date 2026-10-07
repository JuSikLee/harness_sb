"""QR Code MCP server: encodes text into a QR code PNG file (fully local)."""
import os

import qrcode
from qrcode.exceptions import DataOverflowError
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("qr")


def _error(message: str) -> dict:
    return {"success": False, "error": message}


@mcp.tool()
def make_qr(text: str, output_path: str) -> dict:
    """Encode text (URL, contact, Wi-Fi info, ...) as a QR code and save it as a PNG.

    Args:
        text: Text to encode in the QR code.
        output_path: Absolute path of the PNG file to save. Missing folders are created.

    Returns:
        {"success": True, "path": <absolute path>, "size_bytes": <int>} on success,
        {"success": False, "error": <message>} on failure.
    """
    if not text:
        return _error("text is required")
    if not output_path or not output_path.lower().endswith(".png"):
        return _error("output_path must end with .png")

    path = os.path.abspath(os.path.expanduser(output_path))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img = qrcode.make(text)
        img.save(path, format="PNG")
        size = os.path.getsize(path)
    except (DataOverflowError, ValueError):
        return _error("text is too long to encode in a QR code")
    except OSError as e:
        return _error(f"failed to save file: {e}")

    return {"success": True, "path": path, "size_bytes": size}


if __name__ == "__main__":
    mcp.run()
