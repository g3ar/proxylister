"""Isolated decorative project joke; not a functional Proxy Tools module.

Keep this private, dependency-free, and disconnected from normal application
workflows. It is not a reusable rendering API, a supported feature surface, or
a component that should be expanded while implementing unrelated work.
"""

from __future__ import annotations

import math
import random
import shutil
import sys


MESSAGE = "proxy is your hallucination"
SHADES = ".,-~:;=!*#$@"


def render_torus(
    width: int,
    height: int,
    *,
    rotation_x: float | None = None,
    rotation_z: float | None = None,
) -> str:
    """Render one shaded torus frame with the hidden message centered over it."""
    width = max(1, width)
    height = max(1, height)
    pixels = [" "] * (width * height)
    depths = [0.0] * (width * height)

    rotation_x = random.uniform(0, math.tau) if rotation_x is None else rotation_x
    rotation_z = random.uniform(0, math.tau) if rotation_z is None else rotation_z
    cos_x, sin_x = math.cos(rotation_x), math.sin(rotation_x)
    cos_z, sin_z = math.cos(rotation_z), math.sin(rotation_z)
    tube_radius, ring_radius, camera_distance = 1.0, 2.0, 5.0
    scale = min(
        width * camera_distance * 0.45 / (tube_radius + ring_radius),
        height * camera_distance * 0.9 / (tube_radius + ring_radius),
    )

    theta = 0.0
    while theta < math.tau:
        cos_theta, sin_theta = math.cos(theta), math.sin(theta)
        circle_x = ring_radius + tube_radius * cos_theta
        circle_y = tube_radius * sin_theta
        phi = 0.0
        while phi < math.tau:
            cos_phi, sin_phi = math.cos(phi), math.sin(phi)
            depth = camera_distance + cos_x * circle_x * sin_phi + circle_y * sin_x
            inverse_depth = 1.0 / depth
            projected_x = circle_x * (cos_z * cos_phi + sin_x * sin_z * sin_phi)
            projected_x -= circle_y * cos_x * sin_z
            projected_y = circle_x * (sin_z * cos_phi - sin_x * cos_z * sin_phi)
            projected_y += circle_y * cos_x * cos_z
            x = int(width / 2 + scale * inverse_depth * projected_x)
            y = int(height / 2 - scale * 0.5 * inverse_depth * projected_y)

            luminance = (
                cos_phi * cos_theta * sin_z
                - cos_x * cos_theta * sin_phi
                - sin_x * sin_theta
                + cos_z * (cos_x * sin_theta - cos_theta * sin_x * sin_phi)
            )
            if 0 <= x < width and 0 <= y < height and luminance > 0:
                index = x + width * y
                if inverse_depth > depths[index]:
                    depths[index] = inverse_depth
                    shade = min(len(SHADES) - 1, int(luminance * 8))
                    pixels[index] = SHADES[shade]
            phi += 0.025
        theta += 0.07

    visible_message = MESSAGE
    if len(visible_message) > width:
        crop = (len(visible_message) - width) // 2
        visible_message = visible_message[crop:crop + width]
    message_x = (width - len(visible_message)) // 2
    message_y = height // 2
    padding_start = max(0, message_x - 2)
    padding_end = min(width, message_x + len(visible_message) + 2)
    row_start = message_y * width
    pixels[row_start + padding_start:row_start + padding_end] = [" "] * (
        padding_end - padding_start
    )
    start = message_y * width + message_x
    pixels[start:start + len(visible_message)] = visible_message

    return "\n".join(
        "".join(pixels[offset:offset + width])
        for offset in range(0, len(pixels), width)
    )


def show(stream=None) -> None:
    """Display the full-terminal frame, or plain text when output is redirected."""
    stream = stream or sys.stdout
    if not stream.isatty():
        print(MESSAGE, file=stream)
        return

    size = shutil.get_terminal_size(fallback=(80, 24))
    drawable_height = max(1, size.lines - 1)
    frame = render_torus(size.columns, drawable_height)
    stream.write(f"\x1b[2J\x1b[H{frame}\x1b[{size.lines};1H")
    stream.flush()
