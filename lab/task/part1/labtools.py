"""
labtools: drawing and plotting helpers for this course. Nothing more.

This file keeps matplotlib and ffmpeg code out of the notebooks. It makes no
VSS API calls. The VSS requests live in `course_setup.py` next to this file,
and the notebooks write the important ones out in full where they teach them.
Read `course_setup.py` when you want to see a request.

If you want to reuse something from this course elsewhere, reuse the requests
in `course_setup.py`, not this file.
"""

import base64
import io
import subprocess

CLASS_COLOURS = {
    "car": "#4cc9f0",
    "person": "#f72585",
    "bicycle": "#b5e48c",
    "road_sign": "#ffd166",
}


def frame_from_mp4(video_bytes, width=None):
    """Decode one frame out of an mp4 already in memory.

    Returned at native resolution unless you ask otherwise. That matters:
    detection boxes are recorded in the video's own pixel coordinates, so
    resizing the frame before drawing puts the boxes in the wrong place.
    """
    from PIL import Image

    cmd = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1"]
    if width:
        cmd += ["-vf", "scale=" + str(width) + ":-1"]
    cmd += ["-f", "image2", "-vcodec", "mjpeg", "pipe:1"]

    proc = subprocess.run(cmd, input=video_bytes, capture_output=True, timeout=120)
    if not proc.stdout:
        raise RuntimeError("ffmpeg could not decode a frame: "
                           + proc.stderr.decode()[:200])
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def draw_boxes(img, objects):
    """Draw RT-CV detections on a frame.

    `objects` is the `objects` list straight out of an Elasticsearch document.
    """
    from PIL import ImageDraw

    img = img.copy()
    draw = ImageDraw.Draw(img)
    for o in objects:
        b = o.get("bbox", {}) or {}
        box = [b.get("leftX", 0), b.get("topY", 0),
               b.get("rightX", 0), b.get("bottomY", 0)]
        colour = CLASS_COLOURS.get(o.get("type"), "#ffffff")
        draw.rectangle(box, outline=colour, width=2)
        draw.text((box[0] + 3, max(box[1] - 11, 0)),
                  str(o.get("type")) + " #" + str(o.get("id")), fill=colour)
    return img


def plot_paths(frame, rows, min_points=5, moved_px=40):
    """Draw each tracked object's path over a frame.

    `rows` is a DataFrame with columns: tracklet, left, top, right, bottom.
    """
    import matplotlib.pyplot as plt

    df = rows.dropna(subset=["left", "top", "right", "bottom"]).copy()
    # Follow the middle of the bottom edge: roughly where the object meets the
    # ground, and steadier than the box centre as the box grows with distance.
    df["x"] = (df["left"] + df["right"]) / 2
    df["y"] = df["bottom"]

    fig, ax = plt.subplots(figsize=(11, 11 * frame.height / frame.width))
    ax.imshow(frame, extent=[0, frame.width, frame.height, 0], alpha=.55)

    drawn = 0
    for tracklet, grp in df.groupby("tracklet"):
        if len(grp) < min_points:
            continue
        # Static things (signs, traffic lights) are detected every frame and
        # would bury the vehicles under labels.
        if (grp["x"].max() - grp["x"].min()) < moved_px:
            continue
        ax.plot(grp["x"], grp["y"], linewidth=2, alpha=.9)
        ax.scatter(grp["x"].iloc[-1], grp["y"].iloc[-1], s=18, zorder=3)
        ax.annotate(str(tracklet), (grp["x"].iloc[-1], grp["y"].iloc[-1]),
                    fontsize=8, color="white",
                    bbox=dict(boxstyle="round,pad=0.15", fc="black", alpha=.6))
        drawn += 1

    ax.set_xlim(0, frame.width)
    ax.set_ylim(frame.height, 0)
    ax.set_title(str(drawn) + " moving objects, and the path each took")
    ax.axis("off")
    plt.tight_layout()
    plt.show()


def plot_counts(labels, values, title="", xlabel=""):
    """A horizontal bar chart."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 0.5 * len(labels) + 1.6))
    ax.barh(list(labels), list(values), color="#4cc9f0")
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    plt.tight_layout()
    plt.show()


def plot_scores(series, title="Query vs control"):
    """Compare similarity scores. `series` is {label: [scores]}."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 3.4))
    for label, scores in series.items():
        ax.plot(range(1, len(scores) + 1), scores, marker="o", label=label)
    ax.set_xticks(range(1, max((len(s) for s in series.values()), default=0) + 1))
    ax.axhline(0.5, color="#888", linestyle="--", linewidth=1)
    ax.annotate("Reference score: 0.5", (1, 0.505), color="#888", fontsize=8)
    ax.set_xlabel("result rank")
    ax.set_ylabel("similarity")
    ax.set_title(title)
    ax.legend(fontsize=8)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    plt.tight_layout()
    plt.show()


def show_video(video_bytes, width=640):
    """Play an mp4 that is already in memory, inline in the notebook."""
    from IPython.display import HTML, display

    b64 = base64.b64encode(video_bytes).decode()
    return display(HTML(
        '<video width="' + str(width) + '" controls '
        'src="data:video/mp4;base64,' + b64 + '"></video>'
    ))


def objects_table(hits):
    """Flatten Elasticsearch detection hits into one row per detected object."""
    import pandas as pd

    rows = []
    for h in hits:
        src = h["_source"]
        when = src.get("timestamp", "")
        for o in src.get("objects", []) or []:
            b = o.get("bbox", {}) or {}
            rows.append({
                "time": when,
                "tracklet": o.get("id"),
                "type": o.get("type"),
                "confidence": o.get("confidence"),
                "left": b.get("leftX"), "top": b.get("topY"),
                "right": b.get("rightX"), "bottom": b.get("bottomY"),
            })
    return pd.DataFrame(rows)
