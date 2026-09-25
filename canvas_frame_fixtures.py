"""Synthetic canvas draw-call records for tests — VENDORED from
florragent's tests/conftest.py (2026-09-01). Deterministic, no browser."""
import json

ZOOM = 0.7558333


def arc_rec(frame, x, y, r, color, anchor=None, scale=ZOOM):
    ax, ay = anchor if anchor else (x, y)
    return {"frame": frame, "op": "fill", "x": x, "y": y, "r": r,
            "bbox": [x - r, y - r, x + r, y + r], "n": 1, "fill": color,
            "stroke": None, "lw": None, "alpha": 1, "m": [scale, 0, 0, scale, ax, ay]}


def minimap_rec(frame, world_x, world_y, color="#FFE763", scale=0.0084):
    ox, oy = 1085.65, 0.74
    return {"frame": frame, "op": "fill", "x": ox + world_x * scale, "y": oy + world_y * scale,
            "r": 2.5, "bbox": None, "n": 1, "fill": color, "stroke": None, "lw": None,
            "alpha": 1, "m": [scale, 0, 0, scale, ox, oy]}


def healthbar_recs(frame, ax, ay, hp=1.0, width=60.0):
    out = []
    for color, lw, w in (("#222222", 10, width), ("#DD3434", 6, width), ("#75DD34", 7, width * hp)):
        out.append({"frame": frame, "op": "stroke", "x": ax, "y": ay, "r": None,
                    "bbox": [ax - width / 2, ay, ax - width / 2 + w, ay], "n": 2,
                    "fill": "#FFFFFF", "stroke": color, "lw": lw, "alpha": 1,
                    "m": [ZOOM, 0, 0, ZOOM, ax, ay]})
    return out


def text_rec(frame, ax, ay, text, color="#FFFFFF", scale=ZOOM):
    return {"frame": frame, "op": "text", "text": text, "x": ax, "y": ay, "fill": color,
            "alpha": 1, "m": [scale, 0, 0, scale, ax, ay]}


def nameplate(frame, ax, ay, name, rarity="Common", rarity_color="#7EEF6D", hp=1.0):
    """One mob's nameplate block in florr.io's draw order: bar, name x2, rarity x2."""
    return (healthbar_recs(frame, ax, ay, hp)
            + [text_rec(frame, ax - 34, ay + 39, name)] * 2
            + [text_rec(frame, ax + 8, ay + 60, rarity, rarity_color)] * 2)


def player_recs(frame, sx=600.0, sy=453.5):
    return [
        arc_rec(frame, sx, sy, 20.0, "#CFBB50", anchor=(sx, sy)),
        arc_rec(frame, sx, sy, 17.8, "#FFE763", anchor=(sx, sy)),
        arc_rec(frame, sx - 5.3, sy - 3.8, 3.4, "#111111", anchor=(sx - 5.3, sy - 3.8)),
        arc_rec(frame, sx + 5.3, sy - 3.8, 3.4, "#111111", anchor=(sx + 5.3, sy - 3.8)),
    ]


def gameplay_frame(frame, player_world=(5640.0, 6911.0), mobs=(), player_screen=(600.0, 453.5)):
    recs = list(player_recs(frame, *player_screen))
    for ax, ay, name, hp in mobs:
        recs += nameplate(frame, ax, ay, name, hp=hp)
        recs += [arc_rec(frame, ax, ay, 11.3, "#8AC255", anchor=(ax, ay))]
    recs += [minimap_rec(frame, *player_world)]
    return recs
