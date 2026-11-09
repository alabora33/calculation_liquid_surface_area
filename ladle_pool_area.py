import argparse
import json
import os
import time
from collections import deque

import cv2
import numpy as np
from scipy.optimize import least_squares

LADLE_DIAMETER_MM = 3200.0
DEFAULT_ELLIPSE = {"cx": 661.7, "cy": 361.3, "a": 568.6, "b": 256.3}

FONT = cv2.FONT_HERSHEY_SIMPLEX
CYAN = (255, 255, 0)
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (0, 0, 255)


def fit_ellipse(points):
    pts = np.asarray(points, dtype=float)

    def residuals(p):
        cx, cy, a, b = p
        return ((pts[:, 0] - cx) / a) ** 2 + ((pts[:, 1] - cy) / b) ** 2 - 1

    x0 = [pts[:, 0].mean(), pts[:, 1].mean() + 50, np.ptp(pts[:, 0]) / 2 + 1, np.ptp(pts[:, 1]) + 1]
    cx, cy, a, b = least_squares(residuals, x0).x
    return {"cx": float(cx), "cy": float(cy), "a": float(abs(a)), "b": float(abs(b))}


def draw_ellipse(img, e, color, thickness=1):
    center = (int(e["cx"]), int(e["cy"]))
    axes = (int(e["a"]), int(e["b"]))
    cv2.ellipse(img, center, axes, 0, 0, 360, color, thickness, cv2.LINE_AA)


def calibrate(frame, path):
    points = []

    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()

    cv2.namedWindow("calibration")
    cv2.setMouseCallback("calibration", on_mouse)

    while True:
        view = frame.copy()
        for p in points:
            cv2.circle(view, p, 4, (255, 0, 0), -1)
        if len(points) >= 5:
            draw_ellipse(view, fit_ellipse(points), GREEN, 2)
        cv2.putText(view, "LMB: add  RMB: undo  ENTER: save  ESC: cancel",
                    (10, 25), FONT, 0.55, WHITE, 2)
        cv2.imshow("calibration", view)

        key = cv2.waitKey(20) & 0xFF
        if key == 13 and len(points) >= 5:
            ellipse = fit_ellipse(points)
            with open(path, "w") as f:
                json.dump(ellipse, f, indent=2)
            cv2.destroyWindow("calibration")
            return ellipse
        if key == 27:
            raise SystemExit("calibration cancelled")


def load_ellipse(args, frame):
    if args.calibrate:
        return calibrate(frame, args.calib)
    if os.path.exists(args.calib):
        with open(args.calib) as f:
            return json.load(f)
    return DEFAULT_ELLIPSE


class PoolDetector:
    def __init__(self, frame_shape, ellipse, threshold=175, crack_kernel=13,
                 min_area_cm2=150, edge_margin=15, arc_ratio=0.03, smoothing=8):
        h, w = frame_shape[:2]
        radius = LADLE_DIAMETER_MM / 2

        self.ellipse = ellipse
        self.mm2_per_px = radius ** 2 / (ellipse["a"] * ellipse["b"])
        self.ladle_area_m2 = np.pi * (radius / 1000) ** 2
        self.threshold = threshold
        self.min_px = min_area_cm2 * 100 / self.mm2_per_px
        self.arc_ratio = arc_ratio

        self.roi = np.zeros((h, w), np.uint8)
        draw_ellipse(self.roi, ellipse, 255, -1)
        if edge_margin > 0:
            self.roi = cv2.erode(self.roi, np.ones((edge_margin, edge_margin), np.uint8))
        self.roi_px = max(int(np.count_nonzero(self.roi)), 1)

        self.open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (crack_kernel, crack_kernel))
        self.close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))

        self.history = deque(maxlen=smoothing)
        self.last = None

    def _is_arc_frame(self, frame, gray):
        inside = self.roi > 0
        saturated = np.count_nonzero((gray >= 250) & inside) / self.roi_px

        b, _, r = cv2.split(frame)
        bluish = (b.astype(np.int16) > r.astype(np.int16) + 15) & (gray > 150) & inside
        blue = np.count_nonzero(bluish) / self.roi_px

        return saturated > self.arc_ratio or blue > 0.01

    def _segment(self, gray):
        blurred = cv2.GaussianBlur(gray, (7, 7), 0)
        mask = np.where(blurred > self.threshold, 255, 0).astype(np.uint8)
        mask = cv2.bitwise_and(mask, self.roi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.close_kernel)
        return cv2.bitwise_and(mask, self.roi)

    def _extract_pools(self, mask):
        n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        pools = []
        clean = np.zeros_like(mask)

        for i in range(1, n):
            x, y, w, h, area_px = stats[i]
            if area_px < self.min_px:
                continue

            blob = (labels[y:y + h, x:x + w] == i).astype(np.uint8)
            contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
                                           offset=(int(x), int(y)))
            if not contours:
                continue

            clean[y:y + h, x:x + w][blob > 0] = 255
            pools.append({
                "contour": max(contours, key=cv2.contourArea),
                "area_m2": area_px * self.mm2_per_px / 1e6,
                "center": (int(centroids[i][0]), int(centroids[i][1])),
            })

        pools.sort(key=lambda p: p["area_m2"], reverse=True)
        return pools, clean

    def process(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self.last is not None and self._is_arc_frame(frame, gray):
            pools, stats, mask = self.last
            return pools, {**stats, "arc": True}, mask

        mask = self._segment(gray)
        pools, mask = self._extract_pools(mask)

        total = sum(p["area_m2"] for p in pools)
        self.history.append(total)
        stats = {
            "count": len(pools),
            "total_m2": total,
            "total_m2_avg": float(np.mean(self.history)),
            "ratio": total / self.ladle_area_m2,
            "arc": False,
        }
        self.last = (pools, stats, mask)
        return pools, stats, mask

    def draw(self, frame, pools, stats, fps=None, mask=None):
        out = frame.copy()
        draw_ellipse(out, self.ellipse, GREEN)

        if mask is not None:
            overlay = np.zeros_like(out)
            overlay[mask > 0] = CYAN
            out = cv2.addWeighted(out, 1.0, overlay, 0.35, 0)

        for idx, pool in enumerate(pools, 1):
            cv2.drawContours(out, [pool["contour"]], -1, BLACK, 4, cv2.LINE_AA)
            cv2.drawContours(out, [pool["contour"]], -1, CYAN, 2, cv2.LINE_AA)
            self._label(out, f"#{idx} {pool['area_m2']:.2f} m2", pool["center"])

        lines = []
        if stats["arc"]:
            lines.append("ARC - holding last value")
        lines += [
            f"Pools       : {stats['count']}",
            f"Liquid area : {stats['total_m2_avg']:.2f} m2",
            f"Coverage    : {100 * stats['total_m2_avg'] / self.ladle_area_m2:.1f}%"
            f"  (ladle {self.ladle_area_m2:.2f} m2)",
        ]
        if fps:
            lines.append(f"{fps:.0f} FPS")

        cv2.rectangle(out, (8, 8), (360, 16 + 20 * len(lines)), BLACK, -1)
        for i, text in enumerate(lines):
            color = RED if text.startswith("ARC") else WHITE
            cv2.putText(out, text, (14, 27 + 20 * i), FONT, 0.5, color, 1, cv2.LINE_AA)
        return out

    @staticmethod
    def _label(img, text, center):
        (tw, th), _ = cv2.getTextSize(text, FONT, 0.5, 1)
        x, y = center
        cv2.rectangle(img, (x - tw // 2 - 3, y - th - 4), (x + tw // 2 + 3, y + 4), BLACK, -1)
        cv2.putText(img, text, (x - tw // 2, y), FONT, 0.5, CYAN, 1, cv2.LINE_AA)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--src", default="0")
    p.add_argument("--out")
    p.add_argument("--csv")
    p.add_argument("--calib", default="calibration.json")
    p.add_argument("--calibrate", action="store_true")
    p.add_argument("--show", action="store_true")
    p.add_argument("--threshold", type=int, default=175)
    p.add_argument("--crack", type=int, default=13)
    p.add_argument("--min-cm2", type=float, default=150)
    return p.parse_args()


def main():
    args = parse_args()
    src = int(args.src) if args.src.isdigit() else args.src

    cap = cv2.VideoCapture(src)
    ok, frame = cap.read()
    if not ok:
        raise SystemExit(f"cannot open source: {args.src}")

    ellipse = load_ellipse(args, frame)
    detector = PoolDetector(frame.shape, ellipse, threshold=args.threshold,
                            crack_kernel=args.crack, min_area_cm2=args.min_cm2)
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 25

    writer = None
    if args.out:
        h, w = frame.shape[:2]
        writer = cv2.VideoWriter(args.out, cv2.VideoWriter_fourcc(*"mp4v"), video_fps, (w, h))

    log = open(args.csv, "w") if args.csv else None
    if log:
        log.write("frame,time_s,pools,area_m2,coverage,arc\n")

    frame_idx = 0
    fps = None
    show_mask = False
    t0 = time.time()

    while ok:
        pools, stats, mask = detector.process(frame)
        view = detector.draw(frame, pools, stats, fps, mask if show_mask else None)

        if writer:
            writer.write(view)
        if log:
            log.write(f"{frame_idx},{frame_idx / video_fps:.2f},{stats['count']},"
                      f"{stats['total_m2']:.4f},{stats['ratio']:.4f},{int(stats['arc'])}\n")
        if args.show:
            cv2.imshow("ladle", view)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
            if key == ord("m"):
                show_mask = not show_mask

        frame_idx += 1
        if frame_idx % 30 == 0:
            fps = 30 / (time.time() - t0)
            t0 = time.time()
        ok, frame = cap.read()

    cap.release()
    if writer:
        writer.release()
    if log:
        log.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()