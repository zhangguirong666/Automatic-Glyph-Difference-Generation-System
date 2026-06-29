import math
from fontTools.pens.basePen import BasePen
from fontTools.pens.ttGlyphPen import TTGlyphPen


SAMPLE_PER_CURVE = 12
POINTS_PER_CONTOUR = 96


class SamplePen(BasePen):
    def __init__(self, glyph_set):
        super().__init__(glyph_set)
        self.contours = []
        self.current = None

    def _moveTo(self, p0):
        if self.current and len(self.current) >= 3:
            self.contours.append(self.current)
        self.current = [tuple(map(float, p0))]

    def _lineTo(self, p1):
        if self.current is None:
            self.current = []
        self.current.append(tuple(map(float, p1)))

    def _qCurveToOne(self, p1, p2):
        if not self.current:
            return
        p0 = self.current[-1]
        p1 = tuple(map(float, p1))
        p2 = tuple(map(float, p2))

        for i in range(1, SAMPLE_PER_CURVE + 1):
            t = i / SAMPLE_PER_CURVE
            x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
            y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
            self.current.append((x, y))

    def _curveToOne(self, p1, p2, p3):
        if not self.current:
            return
        p0 = self.current[-1]
        p1 = tuple(map(float, p1))
        p2 = tuple(map(float, p2))
        p3 = tuple(map(float, p3))

        for i in range(1, SAMPLE_PER_CURVE + 1):
            t = i / SAMPLE_PER_CURVE
            x = (
                (1 - t) ** 3 * p0[0]
                + 3 * (1 - t) ** 2 * t * p1[0]
                + 3 * (1 - t) * t ** 2 * p2[0]
                + t ** 3 * p3[0]
            )
            y = (
                (1 - t) ** 3 * p0[1]
                + 3 * (1 - t) ** 2 * t * p1[1]
                + 3 * (1 - t) * t ** 2 * p2[1]
                + t ** 3 * p3[1]
            )
            self.current.append((x, y))

    def _closePath(self):
        if self.current and len(self.current) >= 3:
            self.contours.append(self.current)
        self.current = None

    def _endPath(self):
        if self.current and len(self.current) >= 3:
            self.contours.append(self.current)
        self.current = None


def signed_area(points):
    if len(points) < 3:
        return 0.0

    s = 0.0
    n = len(points)

    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1

    return s / 2.0


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def centroid(contours):
    pts = [p for c in contours for p in c]
    if not pts:
        return 0.0, 0.0
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def extract_sampled_contours(font, glyph_name):
    glyph_set = font.getGlyphSet()
    pen = SamplePen(glyph_set)

    try:
        glyph_set[glyph_name].draw(pen)
    except Exception:
        return []

    contours = []
    for c in pen.contours:
        if len(c) >= 3 and abs(signed_area(c)) > 1e-3:
            contours.append(c)

    contours.sort(key=lambda c: abs(signed_area(c)), reverse=True)
    return contours


def resample_closed(points, n=POINTS_PER_CONTOUR):
    pts = list(points)
    if len(pts) < 2:
        return [(0.0, 0.0)] * n

    if dist(pts[0], pts[-1]) < 1e-6:
        pts = pts[:-1]

    if len(pts) < 2:
        return [pts[0]] * n

    segs = []
    total = 0.0
    m = len(pts)

    for i in range(m):
        a = pts[i]
        b = pts[(i + 1) % m]
        d = dist(a, b)
        segs.append((a, b, d))
        total += d

    if total <= 1e-6:
        return [pts[0]] * n

    out = []
    step_len = total / n
    seg_i = 0
    acc = 0.0

    for k in range(n):
        target = k * step_len

        while seg_i < len(segs) - 1 and acc + segs[seg_i][2] < target:
            acc += segs[seg_i][2]
            seg_i += 1

        a, b, d = segs[seg_i]
        u = 0.0 if d <= 1e-6 else (target - acc) / d
        x = a[0] + (b[0] - a[0]) * u
        y = a[1] + (b[1] - a[1]) * u
        out.append((x, y))

    return out


def tiny_contour(cx, cy, size=1.0):
    return resample_closed([
        (cx - size, cy - size),
        (cx + size, cy - size),
        (cx + size, cy + size),
        (cx - size, cy + size),
    ])


def rotate_to_match(a, b):
    if len(a) != len(b):
        return b

    n = len(a)
    best_shift = 0
    best_score = None

    # 96点时可接受。后面如需加速，可改步长搜索。
    for shift in range(n):
        score = 0.0
        for i in range(n):
            ax, ay = a[i]
            bx, by = b[(i + shift) % n]
            score += (ax - bx) ** 2 + (ay - by) ** 2

        if best_score is None or score < best_score:
            best_score = score
            best_shift = shift

    return [b[(i + best_shift) % n] for i in range(n)]


def normalize_contours(contours_a, contours_b):
    ca = [resample_closed(c) for c in contours_a]
    cb = [resample_closed(c) for c in contours_b]

    if not ca or not cb:
        return [], []

    max_n = max(len(ca), len(cb))

    cx, cy = centroid(ca + cb)

    while len(ca) < max_n:
        ca.append(tiny_contour(cx, cy))

    while len(cb) < max_n:
        cb.append(tiny_contour(cx, cy))

    out_a = []
    out_b = []

    for a, b in zip(ca, cb):
        # 方向相反则反转
        if signed_area(a) * signed_area(b) < 0:
            b = list(reversed(b))

        # 起点对齐
        b = rotate_to_match(a, b)

        out_a.append(a)
        out_b.append(b)

    return out_a, out_b


def interpolate_contours(contours_a, contours_b, t):
    result = []

    for a, b in zip(contours_a, contours_b):
        c = []
        for pa, pb in zip(a, b):
            x = round(pa[0] * (1 - t) + pb[0] * t)
            y = round(pa[1] * (1 - t) + pb[1] * t)
            c.append((x, y))
        result.append(c)

    return result


def contours_to_ttglyph(contours):
    pen = TTGlyphPen(None)

    for c in contours:
        if len(c) < 3:
            continue

        pen.moveTo(c[0])

        for p in c[1:]:
            pen.lineTo(p)

        pen.closePath()

    return pen.glyph()


def force_interpolate_glyph(font_a, font_b, glyph_a_name, glyph_b_name, t):
    """
    强制字形插值：
    不要求两个字形 contour 数量一致。
    不要求点数一致。
    不要求原始曲线结构一致。
    """

    contours_a = extract_sampled_contours(font_a, glyph_a_name)
    contours_b = extract_sampled_contours(font_b, glyph_b_name)

    if not contours_a or not contours_b:
        return None

    norm_a, norm_b = normalize_contours(contours_a, contours_b)

    if not norm_a or not norm_b:
        return None

    mixed = interpolate_contours(norm_a, norm_b, t)
    return contours_to_ttglyph(mixed)
