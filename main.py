import json
import math
import os
import sys
from datetime import datetime, timezone

RELEASE = datetime(2026, 11, 19, 0, 0, 0, tzinfo=timezone.utc)
ZVALS = (-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722)


def parse_time(text):
    if text is None:
        return None
    s = str(text).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hours_since_release(dt):
    if dt is None:
        return 0.0
    return (dt - RELEASE).total_seconds() / 3600.0


def log_time(dt):
    h = hours_since_release(dt)
    v = 1.0 + h
    if v < 0.05:
        v = 0.05
    return math.log(v)


def clamp_p(p):
    if p < 0.0001:
        return 0.0001
    if p > 0.9999:
        return 0.9999
    return p


def logit_of_percent(rate_pct):
    p = clamp_p(rate_pct / 100.0)
    return math.log(p / (1.0 - p))


def inv_logit_percent(mu):
    if mu >= 0:
        e = math.exp(-mu)
        p = 1.0 / (1.0 + e)
    else:
        e = math.exp(mu)
        p = e / (1.0 + e)
    return 100.0 * p


def ols(xs, ys):
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = 0.0
    sxy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        sxx += dx * dx
        sxy += dx * (y - my)
    if sxx < 1e-12:
        return my, 0.0, 0.0
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = 0.0
    for x, y in zip(xs, ys):
        pred = intercept + slope * x
        d = y - pred
        sse += d * d
    resid = math.sqrt(sse / n)
    return intercept, slope, resid


def distinct_snapshots(history):
    out = []
    prev_key = None
    for row in history:
        rates = row.get("rates") or []
        key = (row.get("players_tracked"), tuple(rates))
        if key == prev_key:
            continue
        prev_key = key
        out.append(row)
    return out


def forecast_case(case):
    trophies = case.get("trophies") or []
    history = case.get("history") or []
    target_dt = parse_time(case.get("target"))
    at_dt = parse_time(case.get("at"))
    horizon_min = case.get("horizon_minutes")
    if horizon_min is not None:
        try:
            horizon_days = float(horizon_min) / 1440.0
        except (TypeError, ValueError):
            horizon_days = 0.0
    elif target_dt is not None and at_dt is not None:
        horizon_days = max(0.0, (target_dt - at_dt).total_seconds() / 86400.0)
    else:
        horizon_days = 0.0
    if horizon_days < 0:
        horizon_days = 0.0
    x_target = log_time(target_dt) if target_dt is not None else 0.0

    snaps = distinct_snapshots(history)

    # Global tracked-player growth on log-time axis.
    px = []
    py = []
    for row in snaps:
        players = row.get("players_tracked")
        if players is None:
            continue
        try:
            pv = float(players)
        except (TypeError, ValueError):
            continue
        if pv <= 0:
            continue
        dt = parse_time(row.get("at") or row.get("slot"))
        px.append(log_time(dt))
        py.append(math.log(pv))
    p_inter, p_slope, p_resid = ols(px, py)
    if len(px) < 2 or abs(p_slope) < 1e-12:
        last_players = py[-1] if py else 0.0
        pred_log_players = last_players
    else:
        if p_slope < 0.0:
            p_slope = 0.0
        if p_slope > 2.5:
            p_slope = 2.5
        p_inter_adj = py[-1] - p_slope * px[-1] if px else p_inter
        pred_log_players = p_inter_adj + p_slope * x_target
        if py and pred_log_players < py[-1]:
            pred_log_players = py[-1]
    pred_players = math.exp(pred_log_players) if pred_log_players < 30 else math.exp(30.0)

    n_troph = len(trophies)
    per = []
    for j in range(n_troph):
        xs = []
        ys_logit = []
        ys_logearn = []
        last_rate = None
        rates_seq = []
        for row in snaps:
            rates = row.get("rates") or []
            if j >= len(rates):
                continue
            r = rates[j]
            if r is None:
                continue
            try:
                rv = float(r)
            except (TypeError, ValueError):
                continue
            rates_seq.append(rv)
            last_rate = rv
            dt = parse_time(row.get("at") or row.get("slot"))
            x = log_time(dt)
            xs.append(x)
            ys_logit.append(logit_of_percent(rv))
            players = row.get("players_tracked")
            try:
                pv = float(players) if players is not None else 0.0
            except (TypeError, ValueError):
                pv = 0.0
            earners = (rv / 100.0) * pv if pv > 0 else 0.0
            if earners < 0.05:
                earners = 0.05
            ys_logearn.append(math.log(earners))
        if last_rate is None:
            # No usable history at all: scan raw history for any last value.
            for row in reversed(history):
                rates = row.get("rates") or []
                if j < len(rates) and rates[j] is not None:
                    try:
                        last_rate = float(rates[j])
                    except (TypeError, ValueError):
                        last_rate = None
                    break
        entry = {
            "xs": xs,
            "ys_logit": ys_logit,
            "ys_logearn": ys_logearn,
            "last_rate": last_rate,
            "n": len(xs),
        }
        if len(xs) >= 2:
            a, b, resid = ols(xs, ys_logit)
            entry["logit_fit"] = (a, b, resid)
            ea, eb, eresid = ols(xs, ys_logearn)
            entry["earn_fit"] = (ea, eb, eresid)
        else:
            entry["logit_fit"] = None
            entry["earn_fit"] = None
        per.append(entry)

    # Tier mean slopes for shrinkage.
    tier_slopes = {}
    tier_earn_slopes = {}
    for idx, tr in enumerate(trophies):
        tier = tr.get("tier") or "bronze"
        e = per[idx]
        if e["logit_fit"] is not None and e["n"] >= 3:
            tier_slopes.setdefault(tier, []).append(e["logit_fit"][1])
            tier_earn_slopes.setdefault(tier, []).append(e["earn_fit"][1])
    tier_mean = {}
    for tier, vals in tier_slopes.items():
        tier_mean[tier] = sum(vals) / len(vals)
    tier_earn_mean = {}
    for tier, vals in tier_earn_slopes.items():
        tier_earn_mean[tier] = sum(vals) / len(vals)
    all_slopes = [v for vals in tier_slopes.values() for v in vals]
    global_mean_slope = sum(all_slopes) / len(all_slopes) if all_slopes else 0.0
    all_earn = [v for vals in tier_earn_slopes.values() for v in vals]
    global_earn_mean = sum(all_earn) / len(all_earn) if all_earn else 0.0

    rw = 0.5 * math.sqrt(horizon_days) if horizon_days > 0 else 0.0
    rows_out = []
    for idx, tr in enumerate(trophies):
        e = per[idx]
        last_rate = e["last_rate"]
        if last_rate is None:
            mu = logit_of_percent(1.0)
            sigma = 1.0
            rows_out.append({"id": tr.get("id"), "q": quantiles(mu, sigma)})
            continue
        if e["n"] < 3 or e["logit_fit"] is None:
            mu = logit_of_percent(last_rate)
            sigma = math.sqrt(rw * rw + 0.30 * 0.30)
            rows_out.append({"id": tr.get("id"), "q": quantiles(mu, sigma)})
            continue
        tier = tr.get("tier") or "bronze"
        tmean = tier_mean.get(tier, global_mean_slope)
        tearn = tier_earn_mean.get(tier, global_earn_mean)
        a, b, resid = e["logit_fit"]
        n = e["n"]
        # Shrinkage weight: more points and lower residual -> trust own slope.
        w = (n / (n + 3.0)) * (1.0 / (1.0 + resid))
        if w < 0.05:
            w = 0.05
        if w > 0.95:
            w = 0.95
        b_shrunk = w * b + (1.0 - w) * tmean
        if b_shrunk > 3.0:
            b_shrunk = 3.0
        if b_shrunk < -3.0:
            b_shrunk = -3.0
        # Anchor the line at the last observed point to respect step level.
        x_last = e["xs"][-1]
        y_last = e["ys_logit"][-1]
        mu_logit = y_last + b_shrunk * (x_target - x_last)

        # Earners / players recombination.
        ea, eb, eresid = e["earn_fit"]
        ew = (n / (n + 3.0)) * (1.0 / (1.0 + eresid))
        if ew < 0.05:
            ew = 0.05
        if ew > 0.95:
            ew = 0.95
        eb_shrunk = ew * eb + (1.0 - ew) * tearn
        if eb_shrunk > 3.0:
            eb_shrunk = 3.0
        if eb_shrunk < -1.0:
            eb_shrunk = -1.0
        log_earn_last = e["ys_logearn"][-1]
        pred_log_earn = log_earn_last + eb_shrunk * (x_target - x_last)
        if pred_log_earn > 30.0:
            pred_log_earn = 30.0
        pred_earn = math.exp(pred_log_earn)
        if pred_players > 0:
            rate_ep = 100.0 * pred_earn / pred_players
        else:
            rate_ep = last_rate
        if rate_ep < 0.0:
            rate_ep = 0.0
        if rate_ep > 100.0:
            rate_ep = 100.0
        mu_ep = logit_of_percent(rate_ep)

        # Blend: logit trend and earners/players agree when dilution is
        # mild; when they disagree the earners view carries the genuine
        # progression signal, so weight it slightly higher. Make the
        # weight adaptive: a dilution dip makes the logit path kinked
        # (large residuals) while log-earners stays smooth and near
        # monotonic, so shift weight toward whichever fit is cleaner.
        w_ep = 0.55
        if eresid < resid - 0.05:
            w_ep = 0.70
        elif eresid > resid + 0.05:
            w_ep = 0.40
        mu = (1.0 - w_ep) * mu_logit + w_ep * mu_ep
        # Never let a tiny-history extrapolation run to the rails purely
        # from slope: pull extreme moves halfway back toward last rate
        # when the horizon is short.
        if horizon_days < 0.25:
            mu_last = logit_of_percent(last_rate)
            mu = 0.5 * mu + 0.5 * mu_last

        resid_term = 0.6 * resid if resid < 1.5 else 0.9
        sigma = math.sqrt(rw * rw + resid_term * resid_term + 0.22 * 0.22)
        if sigma < 0.18:
            sigma = 0.18
        if sigma > 2.5:
            sigma = 2.5
        rows_out.append({"id": tr.get("id"), "q": quantiles(mu, sigma)})
    return {"trophies": rows_out}


def quantiles(mu, sigma):
    vals = []
    for z in ZVALS:
        v = inv_logit_percent(mu + z * sigma)
        if v < 0.0:
            v = 0.0
        if v > 100.0:
            v = 100.0
        vals.append(round(float(v), 4))
    for i in range(1, len(vals)):
        if vals[i] < vals[i - 1]:
            vals[i] = vals[i - 1]
    return vals


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    try:
        names = sorted(os.listdir(in_dir))
    except OSError:
        names = []
    os.makedirs(out_dir, exist_ok=True)
    for name in names:
        if not name.endswith(".in"):
            continue
        path = os.path.join(in_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            case = json.load(fh)
        result = forecast_case(case)
        cid = case.get("id") or name[:-3]
        out_path = os.path.join(out_dir, str(cid) + ".out")
        text = json.dumps(result, separators=(",", ":"), sort_keys=False)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
